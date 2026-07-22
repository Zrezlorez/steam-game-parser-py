"""Cached, retrying HTTP access for Steam catalog sources."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import math
import random
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from steam_catalog import __version__
from steam_catalog.config import Settings
from steam_catalog.db import Database


Clock = Callable[[], datetime]
Sleep = Callable[[float], None]


class CachedHttpClient:
    """Fetch JSON responses with retries and rate buckets."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.next_allowed_at: dict[str, datetime] = {}
        self.client = httpx.Client(
            transport=transport,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": f"steam-catalog/{__version__} local catalog collector"},
        )

    def close(self) -> None:
        """Release the underlying HTTP client's resources."""
        self.client.close()

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        cache_ttl: timedelta | None = None,
        rate_bucket: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch and decode one JSON object without persisting the raw response."""
        normalized_url, _cache_key = self._normalized_url_and_key(url, params)
        response = self._request_with_retries(normalized_url, {}, rate_bucket)

        response.raise_for_status()
        body = response.content.decode("utf-8")
        return self._decode_json(body)

    def _request_with_retries(
        self, url: str, headers: dict[str, str], rate_bucket: str | None
    ) -> httpx.Response:
        for attempt in range(1, self.settings.max_attempts + 1):
            self._wait_for_bucket(rate_bucket)
            try:
                response = self.client.get(url, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self.settings.max_attempts:
                    raise
                self.sleep(self._backoff(attempt))
                continue

            if response.status_code == httpx.codes.NOT_MODIFIED:
                return response
            if (
                not self._is_retryable(response.status_code)
                or attempt == self.settings.max_attempts
            ):
                return response

            self.sleep(self._retry_delay(response, attempt))

        raise RuntimeError("request retry loop exited unexpectedly")

    def _wait_for_bucket(self, rate_bucket: str | None) -> None:
        if rate_bucket is None:
            return

        now = self._now()
        next_allowed_at = self.next_allowed_at.get(rate_bucket, now)
        wait_seconds = max(0.0, (next_allowed_at - now).total_seconds())
        if wait_seconds:
            self.sleep(wait_seconds)
        self.next_allowed_at[rate_bucket] = max(now, next_allowed_at) + timedelta(
            seconds=self._bucket_interval(rate_bucket)
        )

    def _bucket_interval(self, rate_bucket: str) -> float:
        if rate_bucket == "steam":
            return self.settings.steam_interval_seconds
        if rate_bucket == "steamspy":
            return self.settings.steamspy_interval_seconds
        if rate_bucket == "steamspy_all":
            return self.settings.steamspy_all_interval_seconds
        return 0.0

    @staticmethod
    def _normalized_url_and_key(url: str, params: Mapping[str, str] | None) -> tuple[str, str]:
        parsed = urlsplit(url)
        query = list(httpx.QueryParams(parsed.query).multi_items())
        if params is not None:
            query.extend(httpx.QueryParams(params).multi_items())
        canonical_query = urlencode(sorted(query), doseq=True)
        normalized_url = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, canonical_query, "")
        )
        cache_key = f"GET {parsed.netloc.lower()}{parsed.path}?{canonical_query}"
        return normalized_url, cache_key

    @staticmethod
    def _is_retryable(status_code: int) -> bool:
        return status_code == httpx.codes.TOO_MANY_REQUESTS or 500 <= status_code < 600

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if math.isfinite(delay):
                    return max(0.0, delay)
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - self._now()).total_seconds())
        return self._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(60, 2 ** (attempt - 1)) + random.uniform(0, 0.25)

    def _now(self) -> datetime:
        now = self.clock()
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    @staticmethod
    def _decode_json(body: str) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise ValueError("Response body is not valid JSON") from error
        if not isinstance(value, dict):
            raise ValueError("Response JSON must be an object")
        return value
