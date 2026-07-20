"""Steam Store and review requests with pure response parsers."""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
import re
from typing import Any

from steam_catalog.http import CachedHttpClient
from steam_catalog.models import DiscoveryItem, GameDetails, Review, ReviewPage
from steam_catalog.sources.steamspy import _compact_json, _positive_appid


APP_LIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
METADATA_CACHE_TTL = timedelta(days=7)
REVIEWS_CACHE_TTL = timedelta(days=1)
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
    """Fetch Steam app metadata and user reviews through cached HTTP."""

    def __init__(self, http: CachedHttpClient) -> None:
        self.http = http

    def fetch_app_list(self) -> Iterator[DiscoveryItem]:
        """Yield valid entries from the Steam app-list endpoint."""
        payload = self.http.get_json(
            APP_LIST_URL, params={}, cache_ttl=METADATA_CACHE_TTL, rate_bucket="steam"
        )
        yield from parse_app_list(payload)

    def fetch_app_details(self, appid: int) -> GameDetails | None:
        """Fetch details, returning ``None`` for an unavailable application."""
        payload = self.http.get_json(
            APP_DETAILS_URL,
            params={"appids": str(appid)},
            cache_ttl=METADATA_CACHE_TTL,
            rate_bucket="steam",
        )
        return parse_app_details(appid, payload)

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
            cache_ttl=REVIEWS_CACHE_TTL,
            rate_bucket="steam",
        )
        return parse_review_page(appid, payload)


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


def parse_app_list(payload: Mapping[str, Any]) -> list[DiscoveryItem]:
    """Parse valid Steam app-list entries into discovery items."""
    app_list = payload.get("applist")
    if not isinstance(app_list, Mapping) or not isinstance(app_list.get("apps"), list):
        raise ValueError("Steam app list response is malformed")
    items: list[DiscoveryItem] = []
    for source_item in app_list["apps"]:
        if not isinstance(source_item, Mapping):
            continue
        appid = _positive_appid(source_item.get("appid"))
        if appid is not None:
            items.append(
                DiscoveryItem(
                    appid=appid,
                    name=_optional_string(source_item.get("name")) or "",
                    payload_json=_compact_json(source_item),
                )
            )
    return items


def parse_app_details(appid: int, payload: Mapping[str, Any]) -> GameDetails | None:
    """Parse one Steam Store response and preserve fields approved for storage."""
    response = payload.get(str(appid))
    if not isinstance(response, Mapping):
        raise ValueError("Steam app details response is malformed")
    if response.get("success") is False:
        return None
    if response.get("success") is not True or not isinstance(response.get("data"), Mapping):
        raise ValueError("Steam app details response is malformed")
    data = response["data"]
    name = data.get("name")
    if not isinstance(name, str):
        raise ValueError("Steam app details response is malformed")
    release_data = data.get("release_date")
    if release_data is not None and not isinstance(release_data, Mapping):
        raise ValueError("Steam app details response is malformed")
    coming_soon = _optional_bool(release_data.get("coming_soon")) if release_data else None
    release_date = _optional_string(release_data.get("date")) if release_data else None
    short_html = _optional_string(data.get("short_description"))
    detailed_html = _optional_string(data.get("detailed_description"))
    about_html = _optional_string(data.get("about_the_game"))
    return GameDetails(
        appid=appid,
        name=name,
        type=_optional_string(data.get("type")),
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
        header_image_url=_optional_string(data.get("header_image")),
        screenshots_json=_json_or_none(data.get("screenshots")),
        price_json=_json_or_none(data.get("price_overview")),
    )


def parse_review_page(appid: int, payload: Mapping[str, Any]) -> ReviewPage:
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
        raise ValueError("Steam reviews response is malformed")
    reviews = tuple(
        parsed
        for source_review in source_reviews
        if isinstance(source_review, Mapping)
        if (parsed := _parse_review(appid, source_review)) is not None
    )
    return ReviewPage(
        total_reviews=_required_nonnegative_integer(summary.get("total_reviews")),
        total_positive=_required_nonnegative_integer(summary.get("total_positive")),
        total_negative=_required_nonnegative_integer(summary.get("total_negative")),
        review_score=_optional_integer(summary.get("review_score")),
        review_score_desc=_optional_string(summary.get("review_score_desc")),
        reviews=reviews,
        cursor=_optional_string(payload.get("cursor")),
    )


def _parse_review(appid: int, source: Mapping[str, Any]) -> Review | None:
    recommendation_id = source.get("recommendationid")
    review_text = _optional_string(source.get("review"))
    voted_up = _optional_bool(source.get("voted_up"))
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
        language=_optional_string(source.get("language")),
        votes_up=_optional_integer(source.get("votes_up")),
        votes_funny=_optional_integer(source.get("votes_funny")),
        weighted_vote_score=_optional_float(source.get("weighted_vote_score")),
        comment_count=_optional_integer(source.get("comment_count")),
        steam_purchase=_optional_bool(source.get("steam_purchase")),
        received_for_free=_optional_bool(source.get("received_for_free")),
        written_during_early_access=_optional_bool(source.get("written_during_early_access")),
        author_steamid=_optional_string(author.get("steamid")),
        author_playtime_forever=_optional_integer(author.get("playtime_forever")),
        author_playtime_at_review=_optional_integer(author.get("playtime_at_review")),
        created_at=_timestamp_to_iso(source.get("timestamp_created")),
        updated_at=_timestamp_to_iso(source.get("timestamp_updated")),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _required_nonnegative_integer(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("Steam reviews response is malformed")


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _timestamp_to_iso(value: object) -> str | None:
    timestamp = _optional_integer(value)
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp is not None else None


def _normalized_or_none(value: str | None) -> str | None:
    return normalize_html(value) if value is not None else None


def _json_or_none(value: object) -> str | None:
    return _compact_json(value) if value is not None else None
