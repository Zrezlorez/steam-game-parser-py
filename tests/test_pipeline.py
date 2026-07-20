from dataclasses import replace

import pytest

from steam_catalog.db import Database
from steam_catalog.models import DiscoveryItem, GameDetails, ReviewPage
from steam_catalog.pipeline import CatalogPipeline


def item(appid: int, reviews: int) -> DiscoveryItem:
    return DiscoveryItem(
        appid=appid,
        name=f"Game {appid}",
        steamspy_positive=reviews,
        steamspy_negative=0,
        provisional_reviews=reviews,
        payload_json=f'{{"appid":{appid}}}',
    )


class FakeSteamSpyClient:
    def __init__(self, pages: list[list[DiscoveryItem]]) -> None:
        self.pages = pages
        self.requested_pages: list[int] = []

    def fetch_page(self, page: int) -> list[DiscoveryItem]:
        self.requested_pages.append(page)
        if page >= len(self.pages):
            return []
        return self.pages[page]


class FakeSteamClient:
    def __init__(
        self,
        *,
        review_total: int,
        app_type: str,
        coming_soon: bool,
        app_list: list[DiscoveryItem] | None = None,
    ) -> None:
        self.review_page = ReviewPage(
            total_reviews=review_total,
            total_positive=review_total,
        )
        self.details = replace(
            GameDetails(appid=10, name="Game 10"),
            type=app_type,
            is_released=not coming_soon,
            coming_soon=coming_soon,
        )
        self.app_list = app_list or []
        self.calls: list[tuple[object, ...]] = []

    def fetch_app_list(self) -> list[DiscoveryItem]:
        self.calls.append(("app_list",))
        return self.app_list

    def fetch_reviews(self, appid: int, filter: str, review_type: str, limit: int) -> ReviewPage:
        self.calls.append(("reviews", appid, filter, review_type, limit))
        return self.review_page

    def fetch_app_details(self, appid: int) -> GameDetails:
        self.calls.append(("details", appid))
        return replace(self.details, appid=appid)


@pytest.fixture
def pipeline_factory(tmp_path):
    databases: list[Database] = []

    def factory(
        *,
        steamspy_pages: list[list[DiscoveryItem]] | None = None,
        review_total: int = 20,
        app_type: str = "game",
        coming_soon: bool = False,
        app_list: list[DiscoveryItem] | None = None,
    ) -> tuple[CatalogPipeline, Database]:
        database = Database(tmp_path / f"catalog-{len(databases)}.sqlite3")
        database.initialize()
        databases.append(database)
        pipeline = CatalogPipeline(
            database,
            steam=FakeSteamClient(
                review_total=review_total,
                app_type=app_type,
                coming_soon=coming_soon,
                app_list=app_list,
            ),
            steamspy=FakeSteamSpyClient(steamspy_pages or []),
            min_reviews=20,
        )
        return pipeline, database

    yield factory

    for database in databases:
        database.close()


def test_bootstrap_queues_only_provisional_candidates(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20), item(11, 19)]])

    stats = pipeline.bootstrap("steamspy", page_limit=1)

    assert stats.discovered == 2
    assert stats.queued == 1
    assert db.job_status("collect", "10") == "pending"
    assert db.game_status(11) == "below_provisional_threshold"


def test_steam_count_is_authoritative(pipeline_factory):
    pipeline, db = pipeline_factory(review_total=19)

    result = pipeline.collect_one(10)

    assert result.status == "below_verified_threshold"
    assert db.review_count(10) == 0


def test_non_game_and_unreleased_apps_get_explicit_skip_reasons(pipeline_factory):
    non_game, _db1 = pipeline_factory(review_total=20, app_type="dlc")
    upcoming, _db2 = pipeline_factory(review_total=20, coming_soon=True)

    assert non_game.collect_one(10).skip_reason == "not_a_game"
    assert upcoming.collect_one(10).skip_reason == "unreleased"


def test_claim_job_is_exclusive_due_and_increments_attempts(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = Database(path)
    first.initialize()
    second = Database(path)
    second.initialize()
    try:
        first.enqueue_job("collect", "10", updated_at="2026-07-20T00:00:00+00:00")
        first.enqueue_job("collect", "11", updated_at="2026-07-20T00:00:01+00:00")
        first.connection.execute(
            "UPDATE sync_jobs SET status = 'failed', next_attempt_at = ? WHERE item_key = '10'",
            ("2026-07-20T01:00:00+00:00",),
        )

        assert first.claim_job("collect", now="2026-07-20T00:30:00+00:00")["item_key"] == "11"
        assert second.claim_job("collect", now="2026-07-20T00:30:00+00:00") is None

        row = first.connection.execute(
            "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '11'"
        ).fetchone()
        assert tuple(row[:2]) == ("running", 1)
        assert row["lease_token"]
    finally:
        second.close()
        first.close()


def test_failed_jobs_retry_when_due_then_become_permanent(initialized_db):
    initialized_db.enqueue_job("collect", "10", updated_at="2026-07-20T00:00:00+00:00")
    first_lease = initialized_db.claim_job("collect", now="2026-07-20T00:00:01+00:00")

    initialized_db.fail_job(
        "collect",
        "10",
        error="x" * 2000,
        expected_lease_token=first_lease["lease_token"],
        next_attempt_at="2026-07-20T00:05:00+00:00",
        max_attempts=2,
        updated_at="2026-07-20T00:00:02+00:00",
    )

    row = initialized_db.connection.execute(
        "SELECT status, length(last_error), lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("failed", 1000, None)
    assert initialized_db.claim_job("collect", now="2026-07-20T00:04:59+00:00") is None
    second_lease = initialized_db.claim_job("collect", now="2026-07-20T00:05:00+00:00")
    assert second_lease is not None

    initialized_db.fail_job(
        "collect",
        "10",
        error="still failing",
        expected_lease_token=second_lease["lease_token"],
        next_attempt_at="2026-07-20T00:10:00+00:00",
        max_attempts=2,
        updated_at="2026-07-20T00:05:01+00:00",
    )

    assert initialized_db.job_status("collect", "10") == "permanent_failure"
    assert (
        initialized_db.connection.execute(
            "SELECT lease_token FROM sync_jobs WHERE item_key = '10'"
        ).fetchone()[0]
        is None
    )
    assert initialized_db.claim_job("collect", now="2026-07-21T00:00:00+00:00") is None


def test_complete_job_records_terminal_state(initialized_db):
    initialized_db.enqueue_job("collect", "10")
    lease = initialized_db.claim_job("collect")

    initialized_db.complete_job("collect", "10", expected_lease_token=lease["lease_token"])

    assert initialized_db.job_status("collect", "10") == "complete"
    assert (
        initialized_db.connection.execute(
            "SELECT lease_token FROM sync_jobs WHERE item_key = '10'"
        ).fetchone()[0]
        is None
    )


def test_initialize_recovers_running_jobs_stale_for_one_hour(tmp_path):
    path = tmp_path / "stale.sqlite3"
    database = Database(path)
    database.initialize()
    database.enqueue_job("collect", "10")
    database.connection.execute(
        "UPDATE sync_jobs SET status = 'running', updated_at = '2000-01-01T00:00:00+00:00'"
    )
    database.close()

    reopened = Database(path)
    try:
        reopened.initialize()
        assert reopened.job_status("collect", "10") == "pending"
        assert (
            reopened.connection.execute(
                "SELECT lease_token FROM sync_jobs WHERE item_key = '10'"
            ).fetchone()[0]
            is None
        )
    finally:
        reopened.close()


def test_bootstrap_resumes_at_next_page_and_restart_requeues_work(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)], [item(11, 20)]])

    first = pipeline.bootstrap("steamspy", page_limit=1)
    lease = db.claim_job("collect")
    db.complete_job("collect", "10", expected_lease_token=lease["lease_token"])
    resumed = pipeline.bootstrap("steamspy", page_limit=1)
    restarted = pipeline.bootstrap("steamspy", resume=False, page_limit=1)

    assert (first.discovered, first.queued) == (1, 1)
    assert (resumed.discovered, resumed.queued) == (1, 1)
    assert (restarted.discovered, restarted.queued) == (1, 1)
    assert pipeline.steamspy.requested_pages == [0, 1, 0]
    assert db.job_status("collect", "10") == "pending"


def test_bootstrap_provisional_skip_preserves_existing_metadata(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 19)]])
    db.upsert_game(
        appid=10,
        name="Collected name",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00+00:00",
        detailed_description_text="Keep this searchable description.",
        steam_total_reviews=42,
        review_score=8,
    )

    pipeline.bootstrap("steamspy", page_limit=1)

    row = db.connection.execute(
        """
        SELECT name, detailed_description_text, steam_total_reviews, review_score,
               eligibility_status
        FROM games WHERE appid = 10
        """
    ).fetchone()
    assert tuple(row) == (
        "Collected name",
        "Keep this searchable description.",
        42,
        8,
        "below_provisional_threshold",
    )
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'searchable'"
        ).fetchone()[0]
        == 1
    )


def test_steam_fallback_queues_candidates_without_provisional_counts(pipeline_factory):
    pipeline, db = pipeline_factory(
        app_list=[DiscoveryItem(appid=10, name="Fallback", payload_json='{"appid":10}')]
    )

    stats = pipeline.bootstrap("steam")

    assert (stats.discovered, stats.queued) == (1, 1)
    assert db.job_status("collect", "10") == "pending"


def test_repeated_steam_fallback_resume_preserves_terminal_game_and_job_states(
    pipeline_factory,
):
    app_list = [
        DiscoveryItem(appid=10, name="Complete", payload_json='{"appid":10}'),
        DiscoveryItem(appid=11, name="Permanent", payload_json='{"appid":11}'),
    ]
    pipeline, db = pipeline_factory(app_list=app_list)
    pipeline.bootstrap("steam")
    complete_lease = db.claim_job("collect")
    db.record_game_status(10, "eligible")
    db.complete_job("collect", "10", expected_lease_token=complete_lease["lease_token"])
    permanent_lease = db.claim_job("collect")
    db.record_game_status(11, "ineligible", skip_reason="unavailable")
    db.fail_job(
        "collect",
        "11",
        "not available",
        expected_lease_token=permanent_lease["lease_token"],
        next_attempt_at="2026-07-21T00:00:00+00:00",
        max_attempts=1,
    )

    stats = pipeline.bootstrap("steam")

    assert (stats.discovered, stats.queued) == (2, 0)
    assert db.job_status("collect", "10") == "complete"
    assert db.game_status(10) == "eligible"
    assert db.job_status("collect", "11") == "permanent_failure"
    assert db.game_status(11) == "ineligible"


def test_verified_threshold_stops_before_store_details(pipeline_factory):
    pipeline, _db = pipeline_factory(review_total=19)

    pipeline.collect_one(10)

    assert pipeline.steam.calls == [("reviews", 10, "recent", "all", 1)]


def test_skipped_app_persists_store_details_and_steam_aggregate(pipeline_factory):
    pipeline, db = pipeline_factory(review_total=20, app_type="dlc")

    result = pipeline.collect_one(10)

    row = db.connection.execute(
        """
        SELECT type, coming_soon, steam_total_reviews, eligibility_status, skip_reason
        FROM games WHERE appid = 10
        """
    ).fetchone()
    assert result.skip_reason == "not_a_game"
    assert tuple(row) == ("dlc", 0, 20, "ineligible", "not_a_game")


def test_collect_one_completes_queued_job_with_game(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)]])
    pipeline.bootstrap("steamspy", page_limit=1)
    lease = db.claim_job("collect")

    result = pipeline.collect_one(10, lease_token=lease["lease_token"])

    assert result.status == "eligible"
    assert db.game_status(10) == "eligible"
    assert db.job_status("collect", "10") == "complete"


def test_game_and_job_completion_roll_back_together(pipeline_factory, monkeypatch):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)]])
    pipeline.bootstrap("steamspy", page_limit=1)
    lease = db.claim_job("collect")
    complete_job = db.complete_job

    def abort_after_completion(job_kind, item_key, *, expected_lease_token, updated_at=None):
        complete_job(
            job_kind,
            item_key,
            expected_lease_token=expected_lease_token,
            updated_at=updated_at,
        )
        raise RuntimeError("abort game transaction")

    monkeypatch.setattr(db, "complete_job", abort_after_completion)

    with pytest.raises(RuntimeError, match="abort game transaction"):
        pipeline.collect_one(10, lease_token=lease["lease_token"])

    assert db.game_status(10) == "pending"
    assert db.job_status("collect", "10") == "running"


def test_bootstrap_page_rolls_back_as_one_resumable_unit(pipeline_factory, monkeypatch):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20), item(11, 20)]])
    enqueue_job = db.enqueue_job

    def fail_second_job(job_kind, item_key, **kwargs):
        if item_key == "11":
            raise RuntimeError("interrupt page")
        return enqueue_job(job_kind, item_key, **kwargs)

    monkeypatch.setattr(db, "enqueue_job", fail_second_job)

    with pytest.raises(RuntimeError, match="interrupt page"):
        pipeline.bootstrap("steamspy", page_limit=1)

    assert db.connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0] == 0


def test_provisional_refresh_removes_stale_collect_job(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)]])
    pipeline.bootstrap("steamspy", page_limit=1)
    pipeline.steamspy.pages = [[item(10, 19)]]

    pipeline.bootstrap("steamspy", resume=False, page_limit=1)

    assert db.game_status(10) == "below_provisional_threshold"
    assert db.job_status("collect", "10") is None


def test_claim_job_compares_equivalent_utc_timestamp_formats(initialized_db):
    initialized_db.enqueue_job("collect", "10")
    initialized_db.connection.execute(
        """
        UPDATE sync_jobs
        SET status = 'failed', next_attempt_at = '2026-07-20T00:00:00Z'
        WHERE item_key = '10'
        """
    )

    claimed = initialized_db.claim_job("collect", now="2026-07-20T00:00:00+00:00")

    assert claimed is not None


def test_stale_worker_cannot_complete_or_fail_a_reclaimed_lease(initialized_db):
    initialized_db.record_game_status(10, "pending")
    initialized_db.enqueue_job("collect", "10")
    stale_lease = initialized_db.claim_job("collect")
    initialized_db.connection.execute(
        "UPDATE sync_jobs SET status = 'pending' WHERE item_key = '10'"
    )
    current_lease = initialized_db.claim_job("collect")

    with pytest.raises(RuntimeError, match="lease"):
        initialized_db.complete_job(
            "collect", "10", expected_lease_token=stale_lease["lease_token"]
        )
    with pytest.raises(RuntimeError, match="lease"):
        with initialized_db.transaction():
            initialized_db.record_game_status(10, "eligible")
            initialized_db.fail_job(
                "collect",
                "10",
                "stale worker",
                expected_lease_token=stale_lease["lease_token"],
                next_attempt_at="2026-07-21T00:00:00+00:00",
                max_attempts=3,
            )

    row = initialized_db.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == (
        "running",
        current_lease["attempt_count"],
        current_lease["lease_token"],
    )
    assert initialized_db.game_status(10) == "pending"


def test_stale_pipeline_lease_rolls_back_game_writes(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)]])
    pipeline.bootstrap("steamspy", page_limit=1)
    stale_lease = db.claim_job("collect")
    db.connection.execute("UPDATE sync_jobs SET status = 'pending' WHERE item_key = '10'")
    current_lease = db.claim_job("collect")

    with pytest.raises(RuntimeError, match="lease"):
        pipeline.collect_one(10, lease_token=stale_lease["lease_token"])

    assert db.game_status(10) == "pending"
    row = db.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == (
        "running",
        current_lease["attempt_count"],
        current_lease["lease_token"],
    )


def test_stale_pipeline_lease_is_rejected_after_explicit_requeue(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)]])
    pipeline.bootstrap("steamspy", page_limit=1)
    stale_lease = db.claim_job("collect")

    pipeline.bootstrap("steamspy", resume=False, page_limit=1)
    requeued = db.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(requeued) == ("pending", 0, None)
    current_lease = db.claim_job("collect")

    assert stale_lease["attempt_count"] == current_lease["attempt_count"] == 1
    assert stale_lease["lease_token"] != current_lease["lease_token"]
    with pytest.raises(RuntimeError, match="lease"):
        pipeline.collect_one(10, lease_token=stale_lease["lease_token"])

    assert db.game_status(10) == "pending"
    row = db.connection.execute(
        "SELECT status, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("running", current_lease["lease_token"])


def test_stale_pipeline_lease_is_rejected_after_discard_and_recreate(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 20)]])
    pipeline.bootstrap("steamspy", page_limit=1)
    stale_lease = db.claim_job("collect")

    db.discard_job("collect", "10")
    db.enqueue_job("collect", "10")
    current_lease = db.claim_job("collect")

    assert stale_lease["attempt_count"] == current_lease["attempt_count"] == 1
    assert stale_lease["lease_token"] != current_lease["lease_token"]
    with pytest.raises(RuntimeError, match="lease"):
        pipeline.collect_one(10, lease_token=stale_lease["lease_token"])

    assert db.game_status(10) == "pending"
    row = db.connection.execute(
        "SELECT status, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("running", current_lease["lease_token"])


def test_repeated_stale_recovery_exhausts_crashed_job(tmp_path):
    path = tmp_path / "crashes.sqlite3"
    database = Database(path, max_attempts=2)
    database.initialize()
    database.enqueue_job("collect", "10")
    first_lease = database.claim_job("collect")
    assert first_lease["attempt_count"] == 1
    database.connection.execute("UPDATE sync_jobs SET updated_at = '2000-01-01T00:00:00+00:00'")
    database.close()

    recovered = Database(path, max_attempts=2)
    recovered.initialize()
    second_lease = recovered.claim_job("collect")
    assert second_lease["attempt_count"] == 2
    recovered.connection.execute("UPDATE sync_jobs SET updated_at = '2000-01-01T00:00:00+00:00'")
    recovered.close()

    exhausted = Database(path, max_attempts=2)
    try:
        exhausted.initialize()
        assert exhausted.job_status("collect", "10") == "permanent_failure"
        assert exhausted.claim_job("collect") is None
    finally:
        exhausted.close()


def test_claim_marks_exhausted_pending_job_permanent(initialized_db):
    initialized_db.enqueue_job("collect", "10")
    initialized_db.connection.execute(
        "UPDATE sync_jobs SET attempt_count = 3, lease_token = 'orphaned' WHERE item_key = '10'"
    )

    assert initialized_db.claim_job("collect") is None
    assert initialized_db.job_status("collect", "10") == "permanent_failure"
    assert (
        initialized_db.connection.execute(
            "SELECT lease_token FROM sync_jobs WHERE item_key = '10'"
        ).fetchone()[0]
        is None
    )


def test_discovery_details_refresh_round_trip_preserves_field_ownership(pipeline_factory):
    discovered = DiscoveryItem(
        appid=10,
        name="Discovery name",
        steamspy_positive=30,
        steamspy_negative=5,
        provisional_reviews=35,
        payload_json='{"appid":10,"positive":30,"negative":5}',
    )
    pipeline, db = pipeline_factory(steamspy_pages=[[discovered]], review_total=40)
    pipeline.steam.details = replace(
        pipeline.steam.details,
        name="Store name",
        detailed_description_text="Store-owned description.",
    )
    pipeline.bootstrap("steamspy", page_limit=1)
    lease = db.claim_job("collect")
    pipeline.collect_one(10, lease_token=lease["lease_token"])

    collected = db.connection.execute(
        "SELECT name, steamspy_positive, steamspy_negative FROM games WHERE appid = 10"
    ).fetchone()
    assert tuple(collected) == ("Store name", 30, 5)

    pipeline.steamspy.pages = [
        [
            replace(
                discovered,
                name="New discovery name",
                steamspy_positive=45,
                steamspy_negative=6,
                provisional_reviews=51,
            )
        ]
    ]
    pipeline.bootstrap("steamspy", resume=False, page_limit=1)

    refreshed = db.connection.execute(
        """
        SELECT name, detailed_description_text, steamspy_positive, steamspy_negative,
               steam_total_reviews
        FROM games WHERE appid = 10
        """
    ).fetchone()
    assert tuple(refreshed) == ("Store name", "Store-owned description.", 45, 6, 40)


def test_claim_prioritizes_review_count_then_timezone_safe_age_and_item_key(initialized_db):
    jobs = [
        ("10", 100, None, None, "2026-07-20T03:00:00Z"),
        ("20", None, 70, 10, "2026-07-20T02:00:00Z"),
        ("30", None, 45, 5, "2026-07-20T02:00:00+02:00"),
        ("40", None, 45, 5, "2026-07-20T00:30:00Z"),
        ("50", None, 45, 5, "2026-07-20T00:30:00+00:00"),
    ]
    for item_key, steam_total, spy_positive, spy_negative, updated_at in jobs:
        initialized_db.upsert_game(
            appid=int(item_key),
            name=item_key,
            eligibility_status="pending",
            updated_at=updated_at,
            steam_total_reviews=steam_total,
            steamspy_positive=spy_positive,
            steamspy_negative=spy_negative,
        )
        initialized_db.enqueue_job("collect", item_key, updated_at=updated_at)

    claimed_keys = []
    for _ in jobs:
        lease = initialized_db.claim_job("collect", now="2026-07-21T00:00:00Z")
        claimed_keys.append(lease["item_key"])
        initialized_db.complete_job(
            "collect",
            lease["item_key"],
            expected_lease_token=lease["lease_token"],
        )

    assert claimed_keys == ["10", "20", "30", "40", "50"]
