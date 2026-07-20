import sqlite3

from steam_catalog.db import Database
from steam_catalog.models import EligibilityStatus, JobStatus


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
        assert "content=''" in db.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'game_search'"
        ).fetchone()[0]
    finally:
        db.close()


def test_initialize_is_idempotent(tmp_path):
    db = Database(tmp_path / "catalog.sqlite3")
    try:
        db.initialize()
        db.initialize()
        assert db.connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
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

    assert initialized_db.connection.execute("SELECT name FROM games WHERE appid = 1").fetchone()[0] == "x"


def test_upsert_game_updates_fts_row_in_the_same_transaction(initialized_db):
    initialized_db.upsert_game(
        appid=70,
        name="Half-Life",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        updated_at="2026-07-20T00:00:00Z",
        developers="Valve",
    )
    initialized_db.upsert_game(
        appid=70,
        name="Half-Life: Source",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        updated_at="2026-07-20T00:01:00Z",
        developers="Valve",
    )

    assert initialized_db.connection.execute("SELECT name FROM games WHERE appid = 70").fetchone()[0] == (
        "Half-Life: Source"
    )
    assert initialized_db.connection.execute(
        "SELECT COUNT(*) FROM game_search WHERE game_search MATCH 'source'"
    ).fetchone()[0] == 1
    assert initialized_db.connection.execute(
        "SELECT COUNT(*) FROM game_search WHERE game_search MATCH '\"half-life\"'"
    ).fetchone()[0] == 1


def test_database_uses_row_objects_and_enables_foreign_keys(initialized_db):
    row = initialized_db.connection.execute("SELECT 1 AS value").fetchone()

    assert isinstance(row, sqlite3.Row)
    assert initialized_db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
