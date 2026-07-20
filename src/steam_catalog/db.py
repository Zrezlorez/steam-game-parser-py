"""SQLite persistence and schema migration support."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from steam_catalog.models import EligibilityStatus


SCHEMA_VERSION = 1

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS games (
    appid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    is_free INTEGER,
    short_description TEXT,
    detailed_description TEXT,
    release_date TEXT,
    required_age INTEGER,
    developers TEXT,
    publishers TEXT,
    genres TEXT,
    tags TEXT,
    positive_reviews INTEGER,
    negative_reviews INTEGER,
    total_reviews INTEGER,
    eligibility_status TEXT NOT NULL DEFAULT 'pending',
    eligibility_reason TEXT,
    store_data_json TEXT,
    steamspy_data_json TEXT,
    created_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_library (
    appid INTEGER PRIMARY KEY REFERENCES games(appid) ON DELETE CASCADE,
    playtime_forever INTEGER NOT NULL DEFAULT 0,
    playtime_2weeks INTEGER,
    last_played_at TEXT,
    added_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id INTEGER PRIMARY KEY,
    appid INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    author_steamid TEXT,
    language TEXT,
    voted_up INTEGER,
    votes_up INTEGER,
    votes_funny INTEGER,
    weighted_vote_score REAL,
    playtime_forever_at_review INTEGER,
    written_during_early_access INTEGER,
    received_for_free INTEGER,
    steam_purchase INTEGER,
    comment_count INTEGER,
    review_text TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_samples (
    id INTEGER PRIMARY KEY,
    appid INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    review_id INTEGER REFERENCES reviews(review_id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    sample_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    appid INTEGER REFERENCES games(appid) ON DELETE SET NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    processed_at TEXT,
    error TEXT,
    UNIQUE(source, source_identifier)
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    finished_at TEXT,
    details_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS game_search USING fts5(
    name,
    developers,
    publishers,
    genres,
    tags,
    content=''
);

CREATE VIRTUAL TABLE IF NOT EXISTS review_search USING fts5(
    author_steamid,
    language,
    review_text,
    content=''
);

CREATE INDEX IF NOT EXISTS games_eligibility_status_updated_at_idx
    ON games(eligibility_status, updated_at);
CREATE INDEX IF NOT EXISTS user_library_last_played_at_idx ON user_library(last_played_at);
CREATE INDEX IF NOT EXISTS reviews_appid_created_at_idx ON reviews(appid, created_at);
CREATE INDEX IF NOT EXISTS review_samples_appid_idx ON review_samples(appid);
CREATE INDEX IF NOT EXISTS source_items_source_processed_at_idx ON source_items(source, processed_at);
CREATE INDEX IF NOT EXISTS sync_jobs_status_created_at_idx ON sync_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS http_cache_expires_at_idx ON http_cache(expires_at);
"""


class Database:
    """Connection wrapper that owns schema initialization and atomic writes."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        """Create schema version one, or validate the existing schema version."""
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        version_row = self.connection.execute("SELECT version FROM schema_version").fetchone()
        if version_row is None:
            try:
                self.connection.executescript(MIGRATION_1)
            except sqlite3.OperationalError as error:
                if "fts5" in str(error).lower():
                    raise RuntimeError("SQLite was built without required FTS5 support") from error
                raise
            self.connection.execute("INSERT INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,))
            return

        version = version_row["version"]
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported SQLite schema version: {version}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a group of writes atomically using SQLite's immediate lock mode."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def upsert_game(
        self,
        *,
        appid: int,
        name: str,
        eligibility_status: EligibilityStatus | str,
        updated_at: str,
        developers: str = "",
        publishers: str = "",
        genres: str = "",
        tags: str = "",
    ) -> None:
        """Persist a game and replace its contentless full-text-search entry."""
        with self.transaction():
            self._delete_game_search_row(appid)
            self.connection.execute(
                """
                INSERT INTO games(
                    appid, name, developers, publishers, genres, tags, eligibility_status, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appid) DO UPDATE SET
                    name = excluded.name,
                    developers = excluded.developers,
                    publishers = excluded.publishers,
                    genres = excluded.genres,
                    tags = excluded.tags,
                    eligibility_status = excluded.eligibility_status,
                    updated_at = excluded.updated_at
                """,
                (
                    appid,
                    name,
                    developers,
                    publishers,
                    genres,
                    tags,
                    eligibility_status.value
                    if isinstance(eligibility_status, EligibilityStatus)
                    else eligibility_status,
                    updated_at,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO game_search(rowid, name, developers, publishers, genres, tags)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (appid, name, developers, publishers, genres, tags),
            )

    def upsert_review(
        self,
        *,
        review_id: int,
        appid: int,
        author_steamid: str,
        language: str,
        review_text: str,
        updated_at: str,
    ) -> None:
        """Persist a review and replace its contentless full-text-search entry."""
        with self.transaction():
            self._delete_review_search_row(review_id)
            self.connection.execute(
                """
                INSERT INTO reviews(review_id, appid, author_steamid, language, review_text, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    appid = excluded.appid,
                    author_steamid = excluded.author_steamid,
                    language = excluded.language,
                    review_text = excluded.review_text,
                    updated_at = excluded.updated_at
                """,
                (review_id, appid, author_steamid, language, review_text, updated_at),
            )
            self.connection.execute(
                """
                INSERT INTO review_search(rowid, author_steamid, language, review_text)
                VALUES(?, ?, ?, ?)
                """,
                (review_id, author_steamid, language, review_text),
            )

    def _delete_game_search_row(self, appid: int) -> None:
        self.connection.execute(
            """
            INSERT INTO game_search(game_search, rowid, name, developers, publishers, genres, tags)
            SELECT 'delete', appid, name, COALESCE(developers, ''), COALESCE(publishers, ''),
                   COALESCE(genres, ''), COALESCE(tags, '')
            FROM games
            WHERE appid = ?
            """,
            (appid,),
        )

    def _delete_review_search_row(self, review_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO review_search(review_search, rowid, author_steamid, language, review_text)
            SELECT 'delete', review_id, COALESCE(author_steamid, ''), COALESCE(language, ''), review_text
            FROM reviews
            WHERE review_id = ?
            """,
            (review_id,),
        )
