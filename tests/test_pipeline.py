from dataclasses import replace

import pytest

from steam_catalog.db import Database
from steam_catalog.models import DiscoveryItem, GameDetails, Review, ReviewPage
from steam_catalog.pipeline import CatalogPipeline, RunStats


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
        app_available: bool,
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
        self.app_available = app_available
        self.calls: list[tuple[object, ...]] = []

    def fetch_app_list(self) -> list[DiscoveryItem]:
        self.calls.append(("app_list",))
        return self.app_list

    def fetch_reviews(self, appid: int, filter: str, review_type: str, limit: int) -> ReviewPage:
        self.calls.append(("reviews", appid, filter, review_type, limit))
        return self.review_page

    def fetch_app_details(self, appid: int) -> GameDetails | None:
        self.calls.append(("details", appid))
        if not self.app_available:
            return None
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
        app_available: bool = True,
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
                app_available=app_available,
                app_list=app_list,
            ),
            steamspy=FakeSteamSpyClient(steamspy_pages or []),
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


def test_claim_specific_job_claims_only_requested_due_job(initialized_db):
    initialized_db.enqueue_job("collect", "10")
    initialized_db.enqueue_job("collect", "11")

    claimed = initialized_db.claim_specific_job(
        "collect",
        "11",
        lease_token="specific-lease",
        now="2026-07-20T00:00:00+00:00",
    )

    assert claimed is not None
    assert (claimed["item_key"], claimed["status"], claimed["lease_token"]) == (
        "11",
        "running",
        "specific-lease",
    )
    assert initialized_db.job_status("collect", "10") == "pending"


@pytest.mark.parametrize(
    "initial_status",
    [None, "pending", "failed", "complete", "permanent_failure"],
)
def test_requeue_and_claim_specific_job_forces_only_requested_non_running_job(
    initialized_db, initial_status
):
    initialized_db.enqueue_job("collect", "99")
    if initial_status is not None:
        initialized_db.enqueue_job("collect", "10")
        initialized_db.connection.execute(
            """
            UPDATE sync_jobs
            SET status = ?, attempt_count = 2, next_attempt_at = '2999-01-01T00:00:00Z',
                last_error = 'old failure'
            WHERE item_key = '10'
            """,
            (initial_status,),
        )

    claimed = initialized_db.requeue_and_claim_specific_job(
        "collect", "10", lease_token="opaque-target-lease"
    )

    assert claimed is not None
    assert (
        claimed["status"],
        claimed["attempt_count"],
        claimed["lease_token"],
        claimed["next_attempt_at"],
        claimed["last_error"],
    ) == ("running", 1, "opaque-target-lease", None, None)
    assert initialized_db.job_status("collect", "99") == "pending"


def test_requeue_and_claim_specific_job_preserves_valid_running_lease(initialized_db):
    initialized_db.enqueue_job("collect", "10")
    running = initialized_db.claim_job("collect", lease_token="current-owner")

    claimed = initialized_db.requeue_and_claim_specific_job(
        "collect", "10", lease_token="would-be-thief"
    )

    assert claimed is None
    row = initialized_db.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("running", running["attempt_count"], "current-owner")


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


def test_bootstrap_provisional_undercount_preserves_authoritative_game(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 19)]])
    db.upsert_game(
        appid=10,
        name="Collected name",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00+00:00",
        detailed_description_text="Keep this searchable description.",
        steam_total_reviews=42,
        review_score=8,
        metadata_fetched_at="2026-07-20T00:00:00+00:00",
        reviews_fetched_at="2026-07-20T00:00:00+00:00",
    )
    _store_sample(db)
    db.enqueue_job("collect", "10")
    lease = db.claim_job("collect")
    db.complete_job("collect", "10", expected_lease_token=lease["lease_token"])

    pipeline.bootstrap("steamspy", page_limit=1)

    row = db.connection.execute(
        """
        SELECT name, detailed_description_text, steam_total_reviews, review_score,
               eligibility_status, metadata_fetched_at, reviews_fetched_at
        FROM games WHERE appid = 10
        """
    ).fetchone()
    assert tuple(row) == (
        "Collected name",
        "Keep this searchable description.",
        42,
        8,
        "eligible",
        "2026-07-20T00:00:00+00:00",
        "2026-07-20T00:00:00+00:00",
    )
    assert db.job_status("collect", "10") == "complete"
    assert db.review_count(10) == 1
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


def test_provisional_undercount_preserves_running_unverified_collection(tmp_path):
    path = tmp_path / "running-undercount.sqlite3"
    owner = Database(path)
    bootstrap_database = Database(path)
    owner.initialize()
    bootstrap_database.initialize()
    try:
        owner.upsert_game(
            appid=10,
            name="Collection in progress",
            eligibility_status="pending",
            detailed_description_text="Keep this searchable state.",
            updated_at="2026-07-20T00:00:00Z",
        )
        _store_sample(owner)
        owner.enqueue_job("collect", "10")
        lease = owner.claim_job("collect", lease_token="original-opaque-lease")
        assert lease is not None

        bootstrap_pipeline = CatalogPipeline(
            bootstrap_database,
            steam=FakeSteamClient(
                review_total=20,
                app_type="game",
                coming_soon=False,
                app_available=True,
            ),
            steamspy=FakeSteamSpyClient([[item(10, 19)]]),
        )
        stats = bootstrap_pipeline.bootstrap("steamspy", page_limit=1)

        game = owner.connection.execute(
            """
            SELECT name, eligibility_status, detailed_description_text,
                   steamspy_positive, steamspy_negative
            FROM games WHERE appid = 10
            """
        ).fetchone()
        job = owner.connection.execute(
            """
            SELECT status, attempt_count, lease_token
            FROM sync_jobs WHERE job_kind = 'collect' AND item_key = '10'
            """
        ).fetchone()
        assert stats == RunStats(discovered=1, queued=0)
        assert tuple(game) == (
            "Collection in progress",
            "pending",
            "Keep this searchable state.",
            None,
            None,
        )
        assert owner.connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0
        assert owner.review_count(10) == 1
        assert [row["recommendation_id"] for row in owner.search_reviews("retained", 10)] == ["old"]
        assert (
            owner.connection.execute(
                "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'searchable'"
            ).fetchone()[0]
            == 1
        )
        assert tuple(job) == ("running", lease["attempt_count"], "original-opaque-lease")

        owner_pipeline = CatalogPipeline(
            owner,
            steam=FakeSteamClient(
                review_total=20,
                app_type="game",
                coming_soon=False,
                app_available=True,
            ),
            steamspy=FakeSteamSpyClient([]),
        )
        result = owner_pipeline.collect_one(10, lease_token="original-opaque-lease")

        assert result.status == "eligible"
        assert owner.job_status("collect", "10") == "complete"
    finally:
        bootstrap_database.close()
        owner.close()


def _store_old_sample(db: Database) -> None:
    db.upsert_game(
        appid=10,
        name="Previously eligible",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00Z",
        steam_total_reviews=30,
        metadata_fetched_at="2026-07-20T00:00:00Z",
        reviews_fetched_at="2026-07-20T00:00:00Z",
    )
    _store_sample(db)


def _store_sample(db: Database) -> None:
    db.replace_review_samples(
        10,
        {
            "helpful_positive": [
                Review(
                    recommendation_id="old",
                    appid=10,
                    review_text="old retained sample",
                    voted_up=True,
                )
            ]
        },
        fetched_at="2026-07-20T00:00:01Z",
    )


@pytest.mark.parametrize(
    ("pipeline_options", "expected_status"),
    [
        ({"review_total": 19}, "below_verified_threshold"),
        ({"app_type": "dlc"}, "ineligible"),
        ({"coming_soon": True}, "ineligible"),
        ({"app_available": False}, "ineligible"),
    ],
)
def test_eligible_refresh_to_ineligible_clears_review_samples(
    pipeline_factory, pipeline_options, expected_status
):
    pipeline, db = pipeline_factory(**pipeline_options)
    _store_old_sample(db)

    result = pipeline.collect_one(10)

    assert result.status == expected_status
    assert db.review_count(10) == 0
    assert db.search_reviews("retained", 10) == []


def test_provisional_refresh_preserves_authoritative_review_samples(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 19)]])
    _store_old_sample(db)

    pipeline.bootstrap("steamspy", resume=False, page_limit=1)

    assert db.game_status(10) == "eligible"
    assert db.review_count(10) == 1
    assert [row["recommendation_id"] for row in db.search_reviews("retained", 10)] == ["old"]


def test_provisional_undercount_preserves_below_verified_authority(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 5)]])
    db.record_game_status(
        10,
        "below_verified_threshold",
        skip_reason="below_verified_threshold",
        steam_total_positive=12,
        steam_total_negative=7,
        steam_total_reviews=19,
        review_score=5,
        review_score_desc="Mixed",
        reviews_fetched_at="2026-06-01T00:00:00Z",
        replace_steam_aggregate=True,
        updated_at="2026-06-01T00:00:00Z",
    )

    pipeline.bootstrap("steamspy", resume=False, page_limit=1)

    row = db.connection.execute(
        """
        SELECT eligibility_status, skip_reason, steam_total_positive, steam_total_negative,
               steam_total_reviews, review_score, review_score_desc, reviews_fetched_at
        FROM games WHERE appid = 10
        """
    ).fetchone()
    assert tuple(row) == (
        "below_verified_threshold",
        "below_verified_threshold",
        12,
        7,
        19,
        5,
        "Mixed",
        "2026-06-01T00:00:00Z",
    )
    assert db.job_status("collect", "10") == "pending"


def test_legacy_canonical_aggregate_is_protected_but_stale(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 5)]])
    db.upsert_game(
        appid=10,
        name="Legacy verified",
        eligibility_status="eligible",
        steam_total_reviews=25,
        updated_at="2026-07-20T00:00:00Z",
    )

    pipeline.bootstrap("steamspy", page_limit=1)
    scheduled = db.schedule_refresh("2026-01-01T00:00:00Z")

    assert db.game_status(10) == "eligible"
    assert scheduled == 1
    assert db.job_status("collect", "10") == "pending"


def test_rediscovery_does_not_make_stale_authoritative_collection_fresh(pipeline_factory):
    pipeline, db = pipeline_factory(steamspy_pages=[[item(10, 30)]])
    db.upsert_game(
        appid=10,
        name="Stale collected game",
        eligibility_status="eligible",
        steam_total_reviews=30,
        metadata_fetched_at="2000-01-01T00:00:00Z",
        reviews_fetched_at="2000-01-01T00:00:00Z",
        updated_at="2000-01-01T00:00:00Z",
    )

    pipeline.bootstrap("steamspy", page_limit=1)
    scheduled = db.schedule_refresh("2026-01-01T00:00:00Z")

    assert scheduled == 1
    assert db.job_status("collect", "10") == "pending"
    assert (
        db.connection.execute("SELECT updated_at FROM games WHERE appid = 10").fetchone()[0]
        == "2000-01-01T00:00:00Z"
    )


def test_stale_lease_rollback_preserves_previous_review_samples(pipeline_factory):
    pipeline, db = pipeline_factory(review_total=19)
    _store_old_sample(db)
    db.enqueue_job("collect", "10")
    stale_lease = db.claim_job("collect")
    db.connection.execute("UPDATE sync_jobs SET status = 'pending' WHERE item_key = '10'")
    current_lease = db.claim_job("collect")

    with pytest.raises(RuntimeError, match="lease"):
        pipeline.collect_one(10, lease_token=stale_lease["lease_token"])

    samples = db.connection.execute(
        "SELECT recommendation_id, sample_kind FROM review_samples"
    ).fetchall()
    assert [tuple(row) for row in samples] == [("old", "helpful_positive")]
    assert [row["recommendation_id"] for row in db.search_reviews("retained", 10)] == ["old"]
    assert db.game_status(10) == "eligible"
    job = db.connection.execute(
        "SELECT status, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(job) == ("running", current_lease["lease_token"])


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
