"""SteamSpy discovery requests and response parsing."""

from collections.abc import Mapping
from datetime import timedelta
import json
import re
from typing import Any

from steam_catalog.http import CachedHttpClient
from steam_catalog.models import DiscoveryItem


STEAMSPY_API_URL = "https://steamspy.com/api.php"
CATALOG_CACHE_TTL = timedelta(days=7)
_BASE_10_INTEGER = re.compile(r"^[0-9]+$")


class SteamSpyClient:
    """Fetch SteamSpy catalog pages through the shared cached HTTP client."""

    def __init__(self, http: CachedHttpClient) -> None:
        self.http = http

    def fetch_page(self, page: int) -> list[DiscoveryItem]:
        """Fetch and parse a SteamSpy ``request=all`` page."""
        payload = self.http.get_json(
            STEAMSPY_API_URL,
            params={"request": "all", "page": str(page)},
            cache_ttl=CATALOG_CACHE_TTL,
            rate_bucket="steamspy_all",
        )
        return parse_catalog_page(payload)


def parse_catalog_page(payload: Mapping[str, Any]) -> list[DiscoveryItem]:
    """Parse valid SteamSpy records into provisional discovery candidates."""
    items: list[DiscoveryItem] = []
    for source_item in payload.values():
        if not isinstance(source_item, Mapping):
            continue
        appid = _positive_appid(source_item.get("appid"))
        if appid is None:
            continue
        items.append(
            DiscoveryItem(
                appid=appid,
                name=_string_or_empty(source_item.get("name")),
                provisional_reviews=_review_count(source_item.get("positive"))
                + _review_count(source_item.get("negative")),
                payload_json=_compact_json(source_item),
            )
        )
    return items


def _positive_appid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _BASE_10_INTEGER.fullmatch(value):
        appid = int(value)
        return appid if appid > 0 else None
    return None


def _review_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and _BASE_10_INTEGER.fullmatch(value):
        return int(value)
    return 0


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
