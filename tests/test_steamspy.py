from datetime import timedelta

from steam_catalog.sources.steamspy import SteamSpyClient, parse_catalog_page


def test_parse_catalog_page_calculates_provisional_review_count(load_fixture):
    items = parse_catalog_page(load_fixture("steamspy_page.json"))

    assert [(item.appid, item.provisional_reviews) for item in items] == [(10, 20), (11, 19)]


def test_parse_catalog_page_rejects_invalid_ids_and_preserves_compact_payloads():
    items = parse_catalog_page(
        {
            "valid": {"appid": "12", "name": "Valid", "positive": "3", "negative": 4},
            "boolean": {"appid": True, "name": "Invalid"},
            "missing": {"name": "Invalid"},
            "zero": {"appid": 0, "name": "Invalid"},
            "negative": {"appid": -1, "name": "Invalid"},
            "decimal": {"appid": "1.2", "name": "Invalid"},
        }
    )

    assert [(item.appid, item.provisional_reviews, item.payload_json) for item in items] == [
        (12, 7, '{"appid":"12","name":"Valid","negative":4,"positive":"3"}')
    ]


def test_fetch_page_uses_steamspy_all_bucket_and_seven_day_cache(load_fixture):
    class RecordingHttpClient:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, str], timedelta, str]] = []

        def get_json(self, url, *, params, cache_ttl, rate_bucket):
            self.calls.append((url, dict(params), cache_ttl, rate_bucket))
            return load_fixture("steamspy_page.json")

    http = RecordingHttpClient()

    assert [item.appid for item in SteamSpyClient(http).fetch_page(4)] == [10, 11]
    assert http.calls == [
        (
            "https://steamspy.com/api.php",
            {"request": "all", "page": "4"},
            timedelta(days=7),
            "steamspy_all",
        )
    ]
