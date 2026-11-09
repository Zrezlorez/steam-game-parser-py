"""SteamSpy discovery requests and response parsing."""

from collections.abc import Mapping
from typing import Any

from steam_catalog.http import HttpClient
from steam_catalog.models import DiscoveryItem
from steam_catalog.sources.common import optional_integer, positive_appid
from steam_catalog.utils import compact_json

STEAMSPY_API_URL = "https://steamspy.com/api.php"


class SteamSpyClient:
    """Fetch SteamSpy catalog pages through the shared HTTP client."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch_page(self, page: int) -> list[DiscoveryItem]:
        """Fetch and parse a SteamSpy ``request=all`` page."""
        payload = self.http.get_json(
            STEAMSPY_API_URL,
            params={"request": "all", "page": str(page)},
            rate_bucket="steamspy_all",
        )
        return parse_catalog_page(payload)


def parse_catalog_page(payload: Mapping[str, Any]) -> list[DiscoveryItem]:
    """Parse valid SteamSpy records into provisional discovery candidates."""
    items: list[DiscoveryItem] = []
    for source_item in payload.values():
        if not isinstance(source_item, Mapping):
            continue
        appid = positive_appid(source_item.get("appid"))
        if appid is None:
            continue
        positive = _review_count(source_item.get("positive"))
        negative = _review_count(source_item.get("negative"))
        items.append(
            DiscoveryItem(
                appid=appid,
                name=_string_or_empty(source_item.get("name")),
                steamspy_positive=positive,
                steamspy_negative=negative,
                provisional_reviews=positive + negative
                if positive is not None and negative is not None
                else None,
                payload_json=compact_json(source_item),
            )
        )
    return items


def _review_count(value: object) -> int | None:
    review_count = optional_integer(value)
    return review_count if review_count is not None and review_count >= 0 else None


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""
