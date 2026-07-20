"""Resumable discovery and verified eligibility collection."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol

from steam_catalog.db import Database
from steam_catalog.models import DiscoveryItem, GameDetails, ReviewPage


class SteamProvider(Protocol):
    def fetch_app_list(self) -> list[DiscoveryItem]: ...

    def fetch_app_details(self, appid: int) -> GameDetails | None: ...

    def fetch_reviews(
        self, appid: int, filter: str, review_type: str, limit: int
    ) -> ReviewPage: ...


class SteamSpyProvider(Protocol):
    def fetch_page(self, page: int) -> list[DiscoveryItem]: ...


@dataclass(frozen=True)
class RunStats:
    discovered: int = 0
    queued: int = 0


@dataclass(frozen=True)
class CollectionResult:
    appid: int
    status: str
    skip_reason: str | None = None


class CatalogPipeline:
    """Coordinate offline-testable providers with durable catalog state."""

    def __init__(
        self,
        database: Database,
        *,
        steam: SteamProvider,
        steamspy: SteamSpyProvider,
        min_reviews: int = 20,
    ) -> None:
        self.database = database
        self.steam = steam
        self.steamspy = steamspy
        self.min_reviews = min_reviews

    def bootstrap(
        self,
        source: str,
        *,
        resume: bool = True,
        page_limit: int | None = None,
    ) -> RunStats:
        """Discover candidates and queue only those passing provisional checks."""
        if page_limit is not None and page_limit < 0:
            raise ValueError("page_limit must not be negative")

        discovered = 0
        queued = 0
        for source_page, items in self._discovery_pages(source, resume, page_limit):
            timestamp = _utc_now()
            with self.database.transaction():
                for candidate in items:
                    discovered += 1
                    self.database.upsert_source_item(
                        source,
                        candidate,
                        source_page=source_page,
                        fetched_at=timestamp,
                    )
                    if (
                        candidate.provisional_reviews is not None
                        and candidate.provisional_reviews < self.min_reviews
                    ):
                        self.database.record_game_status(
                            candidate.appid,
                            "below_provisional_threshold",
                            skip_reason="below_provisional_threshold",
                            updated_at=timestamp,
                        )
                        self.database.discard_job("collect", str(candidate.appid))
                        continue

                    enqueued = self.database.enqueue_job(
                        "collect", str(candidate.appid), resume=resume, updated_at=timestamp
                    )
                    if enqueued:
                        self.database.record_game_status(
                            candidate.appid,
                            "pending",
                            updated_at=timestamp,
                        )
                        queued += 1

        return RunStats(discovered=discovered, queued=queued)

    def collect_one(
        self, appid: int, *, lease_attempt_count: int | None = None
    ) -> CollectionResult:
        """Verify eligibility first, then persist one app's store metadata."""
        if lease_attempt_count is None and self.database.job_status("collect", str(appid)):
            raise ValueError("lease_attempt_count is required for a queued collect job")
        summary = self.steam.fetch_reviews(appid, "recent", "all", 1)
        timestamp = _utc_now()
        aggregate = {
            "steam_total_positive": summary.total_positive,
            "steam_total_negative": summary.total_negative,
            "steam_total_reviews": summary.total_reviews,
            "review_score": summary.review_score,
            "review_score_desc": summary.review_score_desc,
            "reviews_fetched_at": timestamp,
        }
        if summary.total_reviews < self.min_reviews:
            with self.database.transaction():
                self.database.record_game_status(
                    appid,
                    "below_verified_threshold",
                    skip_reason="below_verified_threshold",
                    updated_at=timestamp,
                    replace_steam_aggregate=True,
                    **aggregate,
                )
                self._complete_job(appid, lease_attempt_count, timestamp)
            return CollectionResult(appid, "below_verified_threshold", "below_verified_threshold")

        details = self.steam.fetch_app_details(appid)
        if details is None:
            return self._record_skip(
                appid, None, "unavailable", timestamp, aggregate, lease_attempt_count
            )
        if details.type != "game":
            return self._record_skip(
                appid, details, "not_a_game", timestamp, aggregate, lease_attempt_count
            )
        if details.coming_soon is True or details.is_released is False:
            return self._record_skip(
                appid, details, "unreleased", timestamp, aggregate, lease_attempt_count
            )

        with self.database.transaction():
            self._store_details(details, "eligible", None, timestamp, aggregate)
            self._complete_job(appid, lease_attempt_count, timestamp)
        return CollectionResult(appid, "eligible")

    def _record_skip(
        self,
        appid: int,
        details: GameDetails | None,
        reason: str,
        timestamp: str,
        aggregate: dict[str, int | str | None],
        lease_attempt_count: int | None,
    ) -> CollectionResult:
        with self.database.transaction():
            if details is None:
                self.database.record_game_status(
                    appid,
                    "ineligible",
                    skip_reason=reason,
                    updated_at=timestamp,
                    replace_steam_aggregate=True,
                    **aggregate,
                )
            else:
                self._store_details(details, "ineligible", reason, timestamp, aggregate)
            self._complete_job(appid, lease_attempt_count, timestamp)
        return CollectionResult(appid, "ineligible", reason)

    def _complete_job(self, appid: int, lease_attempt_count: int | None, timestamp: str) -> None:
        if lease_attempt_count is not None:
            self.database.complete_job(
                "collect",
                str(appid),
                expected_attempt_count=lease_attempt_count,
                updated_at=timestamp,
            )

    def _store_details(
        self,
        details: GameDetails,
        status: str,
        skip_reason: str | None,
        timestamp: str,
        aggregate: dict[str, int | str | None],
    ) -> None:
        values = asdict(details)
        appid = values.pop("appid")
        self.database.upsert_game(
            appid=appid,
            eligibility_status=status,
            skip_reason=skip_reason,
            metadata_fetched_at=timestamp,
            updated_at=timestamp,
            **aggregate,
            **values,
        )

    def _discovery_pages(
        self,
        source: str,
        resume: bool,
        page_limit: int | None,
    ):
        if source == "steam":
            if page_limit != 0:
                yield None, self.steam.fetch_app_list()
            return
        if source != "steamspy":
            raise ValueError(f"Unsupported discovery source: {source}")

        page = self.database.next_source_page(source) if resume else 0
        fetched = 0
        while page_limit is None or fetched < page_limit:
            items = self.steamspy.fetch_page(page)
            fetched += 1
            if not items:
                return
            yield page, items
            page += 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
