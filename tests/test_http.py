from datetime import UTC, datetime, timedelta

import httpx
import pytest


def test_second_identical_request_uses_sqlite_cache(http_client_factory):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    client = http_client_factory(handler)

    assert client.get_json("https://example.test/data") == {"ok": True}
    assert client.get_json("https://example.test/data") == {"ok": True}
    assert calls == 1


def test_cache_key_normalizes_host_and_sorted_query_parameters(http_client_factory):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    client = http_client_factory(handler)

    assert client.get_json("https://EXAMPLE.test/data?b=two", params={"a": "one"}) == {"ok": True}
    assert client.get_json("https://example.test/data?a=one&b=two") == {"ok": True}
    assert calls == 1


def test_expired_entry_uses_conditional_headers_before_revalidation(http_client_factory):
    now = datetime(2026, 7, 20, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"ETag": '"version-1"', "Last-Modified": "Sun, 20 Jul 2025 00:00:00 GMT"},
            )
        return httpx.Response(304)

    client = http_client_factory(handler, clock=lambda: now)

    assert client.get_json("https://example.test/data", cache_ttl=timedelta(seconds=1)) == {
        "ok": True
    }
    now += timedelta(seconds=2)
    assert client.get_json("https://example.test/data", cache_ttl=timedelta(seconds=1)) == {
        "ok": True
    }
    assert requests[1].headers["if-none-match"] == '"version-1"'
    assert requests[1].headers["if-modified-since"] == "Sun, 20 Jul 2025 00:00:00 GMT"


def test_429_uses_retry_after_before_retrying(http_client_factory):
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps: list[float] = []
    client = http_client_factory(lambda request: next(responses), sleep=sleeps.append)

    assert client.get_json("https://example.test/data") == {"ok": True}
    assert sleeps == [3.0]


def test_server_errors_retry_with_exponential_backoff(http_client_factory):
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps: list[float] = []
    client = http_client_factory(
        lambda request: next(responses),
        sleep=sleeps.append,
        max_attempts=2,
    )

    assert client.get_json("https://example.test/data") == {"ok": True}
    assert sleeps[0] >= 1.0
    assert sleeps[0] <= 1.25


def test_rate_bucket_waits_before_sending(http_client_factory):
    now = datetime(2026, 7, 20, tzinfo=UTC)
    sleeps: list[float] = []
    client = http_client_factory(
        lambda request: httpx.Response(200, json={"ok": True}),
        clock=lambda: now,
        sleep=sleeps.append,
        steam_interval_seconds=3.0,
    )

    assert client.get_json("https://example.test/one", rate_bucket="steam") == {"ok": True}
    assert client.get_json("https://example.test/two", rate_bucket="steam") == {"ok": True}
    assert sleeps == [3.0]


def test_malformed_json_is_not_cached(http_client_factory):
    client = http_client_factory(lambda request: httpx.Response(200, text="not-json"))

    with pytest.raises(ValueError, match="JSON"):
        client.get_json("https://example.test/data")
