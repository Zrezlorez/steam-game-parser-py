import sqlite3

import pytest

from steam_catalog.db import Database
from steam_catalog.models import EligibilityStatus, JobStatus


EXPECTED_COLUMNS = {
    "games": [
        "appid",
        "name",
        "type",
        "is_released",
        "release_date",
        "coming_soon",
        "short_description_html",
        "short_description_text",
        "detailed_description_html",
        "detailed_description_text",
        "about_the_game_html",
        "about_the_game_text",
        "developers_json",
        "publishers_json",
        "languages_json",
        "platforms_json",
        "categories_json",
        "genres_json",
        "header_image_url",
        "screenshots_json",
        "prices_json",
        "regional_prices_fetched_at",
        "steamspy_positive",
        "steamspy_negative",
        "steam_total_positive",
        "steam_total_negative",
        "steam_total_reviews",
        "review_score",
        "review_score_desc",
        "eligibility_status",
        "skip_reason",
        "metadata_fetched_at",
        "reviews_fetched_at",
        "updated_at",
    ],
    "user_library": [
        "appid",
        "name_at_import",
        "playtime_forever_minutes",
        "playtime_2weeks_minutes",
        "last_played_at",
        "platform_playtime_json",
        "source_payload_json",
        "imported_at",
    ],
    "reviews": [
        "recommendation_id",
        "appid",
        "language",
        "review_text",
        "voted_up",
        "votes_up",
        "votes_funny",
        "weighted_vote_score",
        "comment_count",
        "steam_purchase",
        "received_for_free",
        "written_during_early_access",
        "author_steamid",
        "author_playtime_forever",
        "author_playtime_at_review",
        "created_at",
        "updated_at",
        "fetched_at",
    ],
    "review_samples": ["appid", "recommendation_id", "sample_kind", "sample_rank"],
    "source_items": ["source", "appid", "source_page", "payload_json", "fetched_at"],
    "sync_jobs": [
        "job_kind",
        "item_key",
        "status",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "updated_at",
        "lease_token",
    ],
    "http_cache": [
        "cache_key",
        "url",
        "status_code",
        "response_body",
        "etag",
        "last_modified",
        "fetched_at",
        "expires_at",
    ],
}

EXPECTED_TABLE_INFO = {
    "games": [
        ("appid", "INTEGER", 0, None, 1),
        ("name", "TEXT", 1, None, 0),
        ("type", "TEXT", 0, None, 0),
        ("is_released", "INTEGER", 0, None, 0),
        ("release_date", "TEXT", 0, None, 0),
        ("coming_soon", "INTEGER", 0, None, 0),
        ("short_description_html", "TEXT", 0, None, 0),
        ("short_description_text", "TEXT", 0, None, 0),
        ("detailed_description_html", "TEXT", 0, None, 0),
        ("detailed_description_text", "TEXT", 0, None, 0),
        ("about_the_game_html", "TEXT", 0, None, 0),
        ("about_the_game_text", "TEXT", 0, None, 0),
        ("developers_json", "TEXT", 0, None, 0),
        ("publishers_json", "TEXT", 0, None, 0),
        ("languages_json", "TEXT", 0, None, 0),
        ("platforms_json", "TEXT", 0, None, 0),
        ("categories_json", "TEXT", 0, None, 0),
        ("genres_json", "TEXT", 0, None, 0),
        ("header_image_url", "TEXT", 0, None, 0),
        ("screenshots_json", "TEXT", 0, None, 0),
        ("prices_json", "TEXT", 0, None, 0),
        ("regional_prices_fetched_at", "TEXT", 0, None, 0),
        ("steamspy_positive", "INTEGER", 0, None, 0),
        ("steamspy_negative", "INTEGER", 0, None, 0),
        ("steam_total_positive", "INTEGER", 0, None, 0),
        ("steam_total_negative", "INTEGER", 0, None, 0),
        ("steam_total_reviews", "INTEGER", 0, None, 0),
        ("review_score", "INTEGER", 0, None, 0),
        ("review_score_desc", "TEXT", 0, None, 0),
        ("eligibility_status", "TEXT", 1, "'pending'", 0),
        ("skip_reason", "TEXT", 0, None, 0),
        ("metadata_fetched_at", "TEXT", 0, None, 0),
        ("reviews_fetched_at", "TEXT", 0, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
    "user_library": [
        ("appid", "INTEGER", 0, None, 1),
        ("name_at_import", "TEXT", 1, None, 0),
        ("playtime_forever_minutes", "INTEGER", 1, None, 0),
        ("playtime_2weeks_minutes", "INTEGER", 0, None, 0),
        ("last_played_at", "TEXT", 0, None, 0),
        ("platform_playtime_json", "TEXT", 0, None, 0),
        ("source_payload_json", "TEXT", 1, None, 0),
        ("imported_at", "TEXT", 1, None, 0),
    ],
    "reviews": [
        ("recommendation_id", "TEXT", 0, None, 1),
        ("appid", "INTEGER", 1, None, 0),
        ("language", "TEXT", 0, None, 0),
        ("review_text", "TEXT", 1, None, 0),
        ("voted_up", "INTEGER", 1, None, 0),
        ("votes_up", "INTEGER", 0, None, 0),
        ("votes_funny", "INTEGER", 0, None, 0),
        ("weighted_vote_score", "REAL", 0, None, 0),
        ("comment_count", "INTEGER", 0, None, 0),
        ("steam_purchase", "INTEGER", 0, None, 0),
        ("received_for_free", "INTEGER", 0, None, 0),
        ("written_during_early_access", "INTEGER", 0, None, 0),
        ("author_steamid", "TEXT", 0, None, 0),
        ("author_playtime_forever", "INTEGER", 0, None, 0),
        ("author_playtime_at_review", "INTEGER", 0, None, 0),
        ("created_at", "TEXT", 0, None, 0),
        ("updated_at", "TEXT", 0, None, 0),
        ("fetched_at", "TEXT", 1, None, 0),
    ],
    "review_samples": [
        ("appid", "INTEGER", 1, None, 1),
        ("recommendation_id", "TEXT", 1, None, 2),
        ("sample_kind", "TEXT", 1, None, 3),
        ("sample_rank", "INTEGER", 1, None, 0),
    ],
    "source_items": [
        ("source", "TEXT", 1, None, 1),
        ("appid", "INTEGER", 1, None, 2),
        ("source_page", "INTEGER", 0, None, 0),
        ("payload_json", "TEXT", 1, None, 0),
        ("fetched_at", "TEXT", 1, None, 0),
    ],
    "sync_jobs": [
        ("job_kind", "TEXT", 1, None, 1),
        ("item_key", "TEXT", 1, None, 2),
        ("status", "TEXT", 1, None, 0),
        ("attempt_count", "INTEGER", 1, "0", 0),
        ("next_attempt_at", "TEXT", 0, None, 0),
        ("last_error", "TEXT", 0, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("lease_token", "TEXT", 0, None, 0),
    ],
    "http_cache": [
        ("cache_key", "TEXT", 0, None, 1),
        ("url", "TEXT", 1, None, 0),
        ("status_code", "INTEGER", 1, None, 0),
        ("response_body", "TEXT", 1, None, 0),
        ("etag", "TEXT", 0, None, 0),
        ("last_modified", "TEXT", 0, None, 0),
        ("fetched_at", "TEXT", 1, None, 0),
        ("expires_at", "TEXT", 1, None, 0),
    ],
    "schema_version": [("version", "INTEGER", 1, None, 0)],
    "game_search": [
        ("name", "", 0, None, 0),
        ("short_description_text", "", 0, None, 0),
        ("detailed_description_text", "", 0, None, 0),
        ("about_the_game_text", "", 0, None, 0),
        ("developers", "", 0, None, 0),
        ("publishers", "", 0, None, 0),
        ("genres", "", 0, None, 0),
        ("categories", "", 0, None, 0),
    ],
    "review_search": [
        ("appid", "", 0, None, 0),
        ("recommendation_id", "", 0, None, 0),
        ("review_text", "", 0, None, 0),
    ],
}


def _column_names(db: Database, table: str) -> list[str]:
    return [row["name"] for row in db.connection.execute(f"PRAGMA table_info({table})")]


def _table_info(db: Database, table: str) -> list[tuple[object, ...]]:
    return [
        (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in db.connection.execute(f"PRAGMA table_info({table})")
    ]


def _primary_key_columns(db: Database, table: str) -> list[str]:
    rows = db.connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in sorted(rows, key=lambda row: row["pk"]) if row["pk"]]


def _index_columns(db: Database, index: str) -> list[str]:
    return [row["name"] for row in db.connection.execute(f"PRAGMA index_info({index})")]


def test_status_enums_use_persistable_string_values():
    assert EligibilityStatus.PENDING == "pending"
    assert EligibilityStatus.ELIGIBLE == "eligible"
    assert EligibilityStatus.INELIGIBLE == "ineligible"
    assert JobStatus.PENDING == "pending"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.COMPLETED == "completed"
    assert JobStatus.FAILED == "failed"


def test_initialize_creates_schema_and_fts(tmp_path):
    db = Database(tmp_path / "catalog.sqlite3")
    try:
        db.initialize()
        names = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert {
            "games",
            "user_library",
            "reviews",
            "review_samples",
            "source_items",
            "sync_jobs",
            "http_cache",
            "game_search",
            "review_search",
            "schema_version",
        } <= names
        assert (
            "content=''"
            in db.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'game_search'"
            ).fetchone()[0]
        )
    finally:
        db.close()


def test_schema_uses_exact_approved_columns(initialized_db):
    for table, expected in EXPECTED_COLUMNS.items():
        assert _column_names(initialized_db, table) == expected

    assert _column_names(initialized_db, "game_search") == [
        "name",
        "short_description_text",
        "detailed_description_text",
        "about_the_game_text",
        "developers",
        "publishers",
        "genres",
        "categories",
    ]
    assert _column_names(initialized_db, "review_search") == [
        "appid",
        "recommendation_id",
        "review_text",
    ]


def test_schema_declares_exact_column_metadata(initialized_db):
    for table, expected in EXPECTED_TABLE_INFO.items():
        assert _table_info(initialized_db, table) == expected


def test_schema_uses_approved_keys_and_foreign_keys(initialized_db):
    assert _primary_key_columns(initialized_db, "review_samples") == [
        "appid",
        "recommendation_id",
        "sample_kind",
    ]
    assert _primary_key_columns(initialized_db, "source_items") == ["source", "appid"]
    assert _primary_key_columns(initialized_db, "sync_jobs") == ["job_kind", "item_key"]

    foreign_keys = {
        table: {
            (row["from"], row["table"], row["to"])
            for row in initialized_db.connection.execute(f"PRAGMA foreign_key_list({table})")
        }
        for table in ("user_library", "reviews", "review_samples")
    }
    assert foreign_keys == {
        "user_library": {("appid", "games", "appid")},
        "reviews": {("appid", "games", "appid")},
        "review_samples": {
            ("appid", "games", "appid"),
            ("recommendation_id", "reviews", "recommendation_id"),
        },
    }


def test_schema_creates_operational_indexes(initialized_db):
    expected_indexes = {
        "games_eligibility_status_updated_at_idx": ["eligibility_status", "updated_at"],
        "reviews_appid_created_at_idx": ["appid", "created_at"],
        "review_samples_appid_sample_kind_rank_idx": ["appid", "sample_kind", "sample_rank"],
        "source_items_source_page_idx": ["source", "source_page"],
        "sync_jobs_status_next_attempt_at_idx": ["status", "next_attempt_at"],
        "http_cache_expires_at_idx": ["expires_at"],
    }

    for name, columns in expected_indexes.items():
        assert _index_columns(initialized_db, name) == columns


def test_initialize_is_idempotent(tmp_path):
    db = Database(tmp_path / "catalog.sqlite3")
    try:
        db.initialize()
        db.initialize()
        assert db.connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    finally:
        db.close()


def test_initialize_adds_lease_token_to_existing_version_one_schema(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES(1);
        CREATE TABLE games (
            appid INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            price_json TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO games VALUES(10, 'Legacy', '{"currency":"RUB","final":100}', 'now');
        CREATE TABLE sync_jobs (
            job_kind TEXT NOT NULL,
            item_key TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(job_kind, item_key)
        );
        INSERT INTO sync_jobs VALUES('collect', '10', 'pending', 0, NULL, NULL, 'now');
        """
    )
    connection.close()

    db = Database(path)
    try:
        db.initialize()

        assert db.connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert "price_json" not in _column_names(db, "games")
        assert "prices_json" in _column_names(db, "games")
        assert "regional_prices_fetched_at" in _column_names(db, "games")
        assert db.connection.execute("SELECT prices_json FROM games").fetchone()[0] is None
        assert _column_names(db, "sync_jobs") == EXPECTED_COLUMNS["sync_jobs"]
        assert (
            db.connection.execute(
                "SELECT lease_token FROM sync_jobs WHERE item_key = '10'"
            ).fetchone()[0]
            is None
        )
    finally:
        db.close()


def test_transaction_rolls_back_all_writes(tmp_path):
    db = Database(tmp_path / "catalog.sqlite3")
    try:
        db.initialize()
        try:
            with db.transaction():
                db.connection.execute(
                    "INSERT INTO games(appid, name, eligibility_status, updated_at) "
                    "VALUES(1, 'x', 'pending', 'now')"
                )
                raise RuntimeError("stop")
        except RuntimeError:
            pass
        assert db.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    finally:
        db.close()


def test_transaction_commits_writes(initialized_db):
    with initialized_db.transaction():
        initialized_db.connection.execute(
            "INSERT INTO games(appid, name, eligibility_status, updated_at) "
            "VALUES(1, 'x', 'pending', 'now')"
        )

    assert (
        initialized_db.connection.execute("SELECT name FROM games WHERE appid = 1").fetchone()[0]
        == "x"
    )


def test_upsert_game_updates_fts_row_in_the_same_transaction(initialized_db):
    initialized_db.upsert_game(
        appid=70,
        name="Half-Life",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        updated_at="2026-07-20T00:00:00Z",
        developers_json='["Valve"]',
        short_description_text="A science experiment goes wrong.",
        detailed_description_text="Fight through Black Mesa.",
        about_the_game_text="A landmark first-person adventure.",
        publishers_json='["Valve"]',
        genres_json='["Action"]',
        categories_json='["Single-player"]',
    )
    initialized_db.upsert_game(
        appid=70,
        name="Half-Life: Source",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        updated_at="2026-07-20T00:01:00Z",
        developers_json='["Valve"]',
        short_description_text="The experiment returns in the Source engine.",
        detailed_description_text="Revisit Black Mesa with updated technology.",
        about_the_game_text="The classic adventure remastered.",
        publishers_json='["Valve"]',
        genres_json='["Action"]',
        categories_json='["Single-player"]',
    )

    assert initialized_db.connection.execute("SELECT name FROM games WHERE appid = 70").fetchone()[
        0
    ] == ("Half-Life: Source")
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'source'"
        ).fetchone()[0]
        == 1
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH '\"half-life\"'"
        ).fetchone()[0]
        == 1
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'remastered'"
        ).fetchone()[0]
        == 1
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'experiment'"
        ).fetchone()[0]
        == 1
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search "
            "WHERE game_search MATCH 'detailed_description_text:technology'"
        ).fetchone()[0]
        == 1
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'name:technology'"
        ).fetchone()[0]
        == 0
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search "
            "WHERE game_search MATCH 'short_description_text:technology'"
        ).fetchone()[0]
        == 0
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search "
            "WHERE game_search MATCH 'about_the_game_text:technology'"
        ).fetchone()[0]
        == 0
    )


def test_upsert_review_uses_text_identity_and_search_linkage(initialized_db):
    initialized_db.upsert_game(
        appid=70,
        name="Half-Life",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        updated_at="2026-07-20T00:00:00Z",
    )

    initialized_db.upsert_review(
        recommendation_id="12345678901234567",
        appid=70,
        language="english",
        review_text="Responsive controls and excellent pacing.",
        voted_up=True,
        fetched_at="2026-07-20T00:01:00Z",
    )

    row = initialized_db.connection.execute(
        """
        SELECT reviews.recommendation_id, reviews.appid
        FROM review_search
        JOIN reviews ON reviews.rowid = review_search.rowid
        WHERE review_search MATCH 'pacing'
        """
    ).fetchone()
    assert tuple(row) == ("12345678901234567", 70)


def test_review_may_belong_to_multiple_samples_without_duplicate_membership(initialized_db):
    initialized_db.connection.execute(
        "INSERT INTO games(appid, name, eligibility_status, updated_at) "
        "VALUES(70, 'Half-Life', 'eligible', '2026-07-20T00:00:00Z')"
    )
    initialized_db.connection.execute(
        """
        INSERT INTO reviews(
            recommendation_id, appid, review_text, voted_up, fetched_at
        ) VALUES('123', 70, 'Recommended.', 1, '2026-07-20T00:01:00Z')
        """
    )
    initialized_db.connection.execute(
        "INSERT INTO review_samples VALUES(70, '123', 'helpful_positive', 1)"
    )
    initialized_db.connection.execute(
        "INSERT INTO review_samples VALUES(70, '123', 'recent_positive', 1)"
    )

    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM review_samples WHERE recommendation_id = '123'"
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(sqlite3.IntegrityError):
        initialized_db.connection.execute(
            "INSERT INTO review_samples VALUES(70, '123', 'helpful_positive', 2)"
        )


def test_repository_upserts_join_outer_transaction_and_roll_back_all_rows(initialized_db):
    with pytest.raises(RuntimeError, match="abort game"):
        with initialized_db.transaction():
            initialized_db.upsert_game(
                appid=70,
                name="Half-Life",
                eligibility_status=EligibilityStatus.ELIGIBLE,
                updated_at="2026-07-20T00:00:00Z",
                short_description_text="A resonance cascade changes everything.",
            )
            initialized_db.upsert_review(
                recommendation_id="123",
                appid=70,
                review_text="Unforgettable pacing.",
                voted_up=True,
                fetched_at="2026-07-20T00:01:00Z",
            )
            raise RuntimeError("abort game")

    assert initialized_db.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    assert initialized_db.connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'cascade'"
        ).fetchone()[0]
        == 0
    )
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM review_search WHERE review_search MATCH 'unforgettable'"
        ).fetchone()[0]
        == 0
    )


def test_database_uses_row_objects_and_enables_foreign_keys(initialized_db):
    row = initialized_db.connection.execute("SELECT 1 AS value").fetchone()

    assert isinstance(row, sqlite3.Row)
    assert initialized_db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_collection_queue_size_counts_pending_and_retry_backlog(initialized_db):
    for appid in (10, 20, 30, 40):
        initialized_db.enqueue_job("collect", str(appid))
    initialized_db.connection.execute(
        "UPDATE sync_jobs SET status = 'failed', next_attempt_at = '2999-01-01T00:00:00Z' "
        "WHERE item_key = '20'"
    )
    initialized_db.connection.execute(
        "UPDATE sync_jobs SET status = 'permanent_failure' WHERE item_key = '30'"
    )
    initialized_db.connection.execute(
        "UPDATE sync_jobs SET status = 'complete' WHERE item_key = '40'"
    )

    assert initialized_db.collection_queue_size() == 2


def test_game_progress_details_returns_total_reviews_and_stored_sample_count(initialized_db):
    initialized_db.upsert_game(
        appid=10,
        name="Test Game",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00+00:00",
        steam_total_reviews=124176,
    )
    initialized_db.upsert_review(
        recommendation_id="sample-1",
        appid=10,
        review_text="Useful",
        voted_up=True,
        fetched_at="2026-07-20T00:00:00+00:00",
    )

    assert initialized_db.game_progress_details(10) == ("Test Game", 124176, 1)
    assert initialized_db.game_progress_details(999) == (None, 0, 0)
