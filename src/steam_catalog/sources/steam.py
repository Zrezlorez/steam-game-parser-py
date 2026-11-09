"""Steam Store and review requests with pure response parsers."""

import re
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from steam_catalog.errors import SourceRequestError, SourceResponseError
from steam_catalog.http import HttpClient
from steam_catalog.models import DiscoveryItem, GameDetails, Review, ReviewPage
from steam_catalog.sources.common import (
    optional_bool,
    optional_float,
    optional_integer,
    optional_string,
    positive_appid,
    required_nonnegative_integer,
)
from steam_catalog.utils import compact_json

APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
APP_LIST_MAX_RESULTS = 50_000
APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STORE_REGIONS = (("KZ", "KZT"), ("US", "USD"), ("UA", "UAH"))
_WHITESPACE = re.compile(r"\s+")
_STRUCTURAL_BREAK = object()
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class SteamClient:
    """Fetch Steam app metadata and user reviews through the shared HTTP client."""

    def __init__(self, http: HttpClient, *, api_key: str | None = None) -> None:
        self.http = http
        self.api_key = api_key

    def fetch_app_pages(self) -> Iterator[tuple[list[DiscoveryItem], bool]]:
        """Yield official Steam game-list pages using a validated advancing cursor."""
        if self.api_key is None:
            raise ValueError("STEAM_CATALOG_STEAM_WEB_API_KEY is required for --source steam")
        cursor: int | None = None
        while True:
            params = {
                "key": self.api_key,
                "include_games": "true",
                "max_results": str(APP_LIST_MAX_RESULTS),
            }
            if cursor is not None:
                params["last_appid"] = str(cursor)
            try:
                payload = self.http.get_json(
                    APP_LIST_URL,
                    params=params,
                    rate_bucket="steam",
                )
            except httpx.HTTPStatusError as error:
                raise SourceRequestError(
                    f"Steam app list request failed with HTTP {error.response.status_code}"
                ) from error
            except httpx.HTTPError as error:
                raise SourceRequestError("Steam app list request failed") from error
            items, have_more, next_cursor = parse_app_list_page(payload)
            yield items, not have_more
            if not have_more:
                return
            if next_cursor is None or (cursor is not None and next_cursor <= cursor):
                raise SourceResponseError("Steam app list cursor did not advance")
            cursor = next_cursor

    def fetch_app_details(self, appid: int) -> GameDetails | None:
        """Fetch KZ metadata and its storefront price."""
        kz_payload = self.http.get_json(
            APP_DETAILS_URL,
            params={"appids": str(appid), "cc": "KZ"},
            rate_bucket="steam",
        )
        details = parse_app_details(appid, kz_payload)
        if details is None:
            return None
        prices = {"KZT": _parse_store_price(appid, kz_payload, "KZT")}
        return replace(details, prices_json=compact_json(prices))

    def fetch_regional_prices(self, appid: int) -> str:
        """Fetch the optional US and UA storefront prices."""
        prices: dict[str, object | None] = {}
        for country, currency in STORE_REGIONS[1:]:
            payload = self.http.get_json(
                APP_DETAILS_URL,
                params={"appids": str(appid), "cc": country},
                rate_bucket="steam",
            )
            prices[currency] = _parse_store_price(appid, payload, currency)
        return compact_json(prices)

    def fetch_reviews(self, appid: int, filter: str, review_type: str, limit: int) -> ReviewPage:
        """Fetch one aggregate and sample review page."""
        payload = self.http.get_json(
            REVIEWS_URL.format(appid=appid),
            params={
                "filter": filter,
                "review_type": review_type,
                "num_per_page": str(limit),
                "language": "all",
                "json": "1",
            },
            rate_bucket="steam",
        )
        return parse_review_page(appid, payload, require_aggregate=review_type == "all")


class _TextExtractor(HTMLParser):
    """Collect visible HTML text while preserving structural boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str | object] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            if not self.hidden_depth:
                self._space()
            self.hidden_depth += 1
            return
        if tag in _BLOCK_TAGS and not self.hidden_depth:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
            if not self.hidden_depth:
                self._space()
            return
        if tag in _BLOCK_TAGS and not self.hidden_depth:
            self._newline()

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and self.parts[-1] is not _STRUCTURAL_BREAK:
            self.parts.append(_STRUCTURAL_BREAK)

    def _space(self) -> None:
        if self.parts and isinstance(self.parts[-1], str) and not self.parts[-1].isspace():
            self.parts.append(" ")


def normalize_html(value: str) -> str:
    """Return visible source HTML as whitespace-normalized plain text."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    rendered = "".join(
        "\n" if part is _STRUCTURAL_BREAK else _WHITESPACE.sub(" ", part) for part in parser.parts
    )
    lines = [line.strip() for line in rendered.split("\n")]
    return "\n".join(line for line in lines if line)


def parse_app_list_page(
    payload: Mapping[str, Any],
) -> tuple[list[DiscoveryItem], bool, int | None]:
    """Parse one official Steam app-list page and its continuation cursor."""
    response = payload.get("response")
    if not isinstance(response, Mapping) or not isinstance(response.get("apps"), list):
        raise SourceResponseError("Steam app list response is malformed")
    have_more = response.get("have_more_results")
    if not isinstance(have_more, bool):
        raise SourceResponseError("Steam app list response is malformed")
    raw_last_appid = response.get("last_appid")
    last_appid = positive_appid(raw_last_appid) if raw_last_appid is not None else None
    if raw_last_appid is not None and last_appid is None:
        raise SourceResponseError("Steam app list response is malformed")
    if have_more and last_appid is None:
        raise SourceResponseError("Steam app list response is malformed")
    items: list[DiscoveryItem] = []
    for source_item in response["apps"]:
        if not isinstance(source_item, Mapping):
            continue
        appid = positive_appid(source_item.get("appid"))
        if appid is not None:
            items.append(
                DiscoveryItem(
                    appid=appid,
                    name=optional_string(source_item.get("name")) or "",
                    payload_json=compact_json(source_item),
                )
            )
    return items, have_more, last_appid


def parse_app_details(appid: int, payload: Mapping[str, Any]) -> GameDetails | None:
    """Parse one Steam Store response and preserve fields approved for storage."""
    data = _successful_app_data(appid, payload)
    if data is None:
        return None
    name = data.get("name")
    if not isinstance(name, str):
        raise SourceResponseError("Steam app details response is malformed")
    release_data = data.get("release_date")
    if release_data is not None and not isinstance(release_data, Mapping):
        raise SourceResponseError("Steam app details response is malformed")
    coming_soon = optional_bool(release_data.get("coming_soon")) if release_data else None
    release_date = optional_string(release_data.get("date")) if release_data else None
    short_html = optional_string(data.get("short_description"))
    detailed_html = optional_string(data.get("detailed_description"))
    about_html = optional_string(data.get("about_the_game"))
    return GameDetails(
        appid=appid,
        name=name,
        type=optional_string(data.get("type")),
        is_released=not coming_soon if coming_soon is not None else None,
        release_date=release_date,
        coming_soon=coming_soon,
        short_description_html=short_html,
        short_description_text=_normalized_or_none(short_html),
        detailed_description_html=detailed_html,
        detailed_description_text=_normalized_or_none(detailed_html),
        about_the_game_html=about_html,
        about_the_game_text=_normalized_or_none(about_html),
        developers_json=_json_or_none(data.get("developers")),
        publishers_json=_json_or_none(data.get("publishers")),
        languages_json=_json_or_none(data.get("supported_languages")),
        platforms_json=_json_or_none(data.get("platforms")),
        categories_json=_json_or_none(data.get("categories")),
        genres_json=_json_or_none(data.get("genres")),
        header_image_url=optional_string(data.get("header_image")),
        screenshots_json=_json_or_none(data.get("screenshots")),
    )


def _successful_app_data(appid: int, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    response = payload.get(str(appid))
    if not isinstance(response, Mapping):
        raise SourceResponseError("Steam app details response is malformed")
    if response.get("success") is False:
        return None
    data = response.get("data")
    if response.get("success") is not True or not isinstance(data, Mapping):
        raise SourceResponseError("Steam app details response is malformed")
    return data


def _parse_store_price(
    appid: int, payload: Mapping[str, Any], expected_currency: str
) -> dict[str, Any] | None:
    data = _successful_app_data(appid, payload)
    if data is None:
        return None
    price = data.get("price_overview")
    if price is None:
        if data.get("is_free") is True:
            return {
                "currency": expected_currency,
                "initial": 0,
                "final": 0,
                "discount_percent": 0,
            }
        return None
    if not isinstance(price, Mapping) or price.get("currency") != expected_currency:
        raise SourceResponseError("Steam app details response has unexpected price currency")
    return dict(price)


def parse_review_page(
    appid: int,
    payload: Mapping[str, Any],
    *,
    require_aggregate: bool = True,
) -> ReviewPage:
    """Parse Steam aggregate review data and valid review records."""
    summary = payload.get("query_summary")
    source_reviews = payload.get("reviews")
    success = payload.get("success")
    if (
        type(success) is not int
        or success != 1
        or not isinstance(summary, Mapping)
        or not isinstance(source_reviews, list)
    ):
        raise SourceResponseError("Steam reviews response is malformed")
    reviews = tuple(
        parsed
        for source_review in source_reviews
        if isinstance(source_review, Mapping)
        if (parsed := _parse_review(appid, source_review)) is not None
    )
    aggregate_fields = ("total_reviews", "total_positive", "total_negative")
    if require_aggregate or all(field in summary for field in aggregate_fields):
        total_reviews = _review_count(summary.get("total_reviews"))
        total_positive = _review_count(summary.get("total_positive"))
        total_negative = _review_count(summary.get("total_negative"))
    else:
        total_reviews = _review_count(summary.get("num_reviews"))
        total_positive = sum(review.voted_up for review in reviews)
        total_negative = sum(not review.voted_up for review in reviews)

    return ReviewPage(
        total_reviews=total_reviews,
        total_positive=total_positive,
        total_negative=total_negative,
        review_score=optional_integer(summary.get("review_score")),
        review_score_desc=optional_string(summary.get("review_score_desc")),
        reviews=reviews,
        cursor=optional_string(payload.get("cursor")),
    )


def _review_count(value: object) -> int:
    return required_nonnegative_integer(value, error_message="Steam reviews response is malformed")


def _parse_review(appid: int, source: Mapping[str, Any]) -> Review | None:
    recommendation_id = source.get("recommendationid")
    review_text = optional_string(source.get("review"))
    voted_up = optional_bool(source.get("voted_up"))
    if isinstance(recommendation_id, bool) or recommendation_id is None or review_text is None:
        return None
    if voted_up is None:
        return None
    author = source.get("author")
    if author is not None and not isinstance(author, Mapping):
        return None
    author = author or {}
    return Review(
        recommendation_id=str(recommendation_id),
        appid=appid,
        review_text=review_text,
        voted_up=voted_up,
        language=optional_string(source.get("language")),
        votes_up=optional_integer(source.get("votes_up")),
        votes_funny=optional_integer(source.get("votes_funny")),
        weighted_vote_score=optional_float(source.get("weighted_vote_score")),
        comment_count=optional_integer(source.get("comment_count")),
        steam_purchase=optional_bool(source.get("steam_purchase")),
        received_for_free=optional_bool(source.get("received_for_free")),
        written_during_early_access=optional_bool(source.get("written_during_early_access")),
        author_steamid=optional_string(author.get("steamid")),
        author_playtime_forever=optional_integer(author.get("playtime_forever")),
        author_playtime_at_review=optional_integer(author.get("playtime_at_review")),
        created_at=_timestamp_to_iso(source.get("timestamp_created")),
        updated_at=_timestamp_to_iso(source.get("timestamp_updated")),
    )


def _timestamp_to_iso(value: object) -> str | None:
    timestamp = optional_integer(value)
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp is not None else None


def _normalized_or_none(value: str | None) -> str | None:
    return normalize_html(value) if value is not None else None


def _json_or_none(value: object) -> str | None:
    return compact_json(value) if value is not None else None
