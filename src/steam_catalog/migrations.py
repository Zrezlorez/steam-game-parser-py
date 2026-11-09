"""SQLite schema definition and forward-only migrations."""

import sqlite3

from steam_catalog.errors import SchemaError

SCHEMA_VERSION = 3

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS games (
    appid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    is_released INTEGER,
    release_date TEXT,
    coming_soon INTEGER,
    short_description_html TEXT,
    short_description_text TEXT,
    detailed_description_html TEXT,
    detailed_description_text TEXT,
    about_the_game_html TEXT,
    about_the_game_text TEXT,
    developers_json TEXT,
    publishers_json TEXT,
    languages_json TEXT,
    platforms_json TEXT,
    categories_json TEXT,
    genres_json TEXT,
    header_image_url TEXT,
    screenshots_json TEXT,
    prices_json TEXT,
    regional_prices_fetched_at TEXT,
    steamspy_positive INTEGER,
    steamspy_negative INTEGER,
    steam_total_positive INTEGER,
    steam_total_negative INTEGER,
    steam_total_reviews INTEGER,
    review_score INTEGER,
    review_score_desc TEXT,
    eligibility_status TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT,
    metadata_fetched_at TEXT,
    reviews_fetched_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_library (
    appid INTEGER PRIMARY KEY REFERENCES games(appid) ON DELETE CASCADE,
    name_at_import TEXT NOT NULL,
    playtime_forever_minutes INTEGER NOT NULL,
    playtime_2weeks_minutes INTEGER,
    last_played_at TEXT,
    platform_playtime_json TEXT,
    source_payload_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    recommendation_id TEXT PRIMARY KEY,
    appid INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    language TEXT,
    review_text TEXT NOT NULL,
    voted_up INTEGER NOT NULL,
    votes_up INTEGER,
    votes_funny INTEGER,
    weighted_vote_score REAL,
    comment_count INTEGER,
    steam_purchase INTEGER,
    received_for_free INTEGER,
    written_during_early_access INTEGER,
    author_steamid TEXT,
    author_playtime_forever INTEGER,
    author_playtime_at_review INTEGER,
    created_at TEXT,
    updated_at TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_samples (
    appid INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    recommendation_id TEXT NOT NULL REFERENCES reviews(recommendation_id) ON DELETE CASCADE,
    sample_kind TEXT NOT NULL CHECK(sample_kind IN (
        'helpful_positive', 'helpful_negative', 'recent_positive', 'recent_negative'
    )),
    sample_rank INTEGER NOT NULL,
    PRIMARY KEY(appid, recommendation_id, sample_kind)
);

CREATE TABLE IF NOT EXISTS source_items (
    source TEXT NOT NULL,
    appid INTEGER NOT NULL,
    source_page INTEGER,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(source, appid)
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    job_kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    lease_token TEXT,
    PRIMARY KEY(job_kind, item_key)
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS game_search USING fts5(
    name,
    short_description_text,
    detailed_description_text,
    about_the_game_text,
    developers,
    publishers,
    genres,
    categories,
    content=''
);

CREATE VIRTUAL TABLE IF NOT EXISTS review_search USING fts5(
    appid UNINDEXED,
    recommendation_id UNINDEXED,
    review_text,
    content=''
);

CREATE INDEX IF NOT EXISTS games_eligibility_status_updated_at_idx
    ON games(eligibility_status, updated_at);
CREATE INDEX IF NOT EXISTS user_library_last_played_at_idx ON user_library(last_played_at);
CREATE INDEX IF NOT EXISTS reviews_appid_created_at_idx ON reviews(appid, created_at);
CREATE INDEX IF NOT EXISTS review_samples_appid_sample_kind_rank_idx
    ON review_samples(appid, sample_kind, sample_rank);
CREATE INDEX IF NOT EXISTS source_items_source_page_idx ON source_items(source, source_page);
CREATE INDEX IF NOT EXISTS sync_jobs_status_next_attempt_at_idx
    ON sync_jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS http_cache_expires_at_idx ON http_cache(expires_at);
"""

_MIGRATION_2 = """
ALTER TABLE games DROP COLUMN price_json;
ALTER TABLE games ADD COLUMN prices_json TEXT;
UPDATE schema_version SET version = 2;
"""

_MIGRATION_3 = """
ALTER TABLE games ADD COLUMN regional_prices_fetched_at TEXT;
UPDATE schema_version SET version = 3;
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create schema version one or migrate an existing database in place."""
    connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    version_row = connection.execute("SELECT version FROM schema_version").fetchone()
    if version_row is None:
        _create_initial_schema(connection)
        return

    version = version_row[0]
    if version == 1:
        connection.executescript(_MIGRATION_2)
        version = 2
    if version == 2:
        connection.executescript(_MIGRATION_3)
        version = 3
    if version != SCHEMA_VERSION:
        raise SchemaError(f"Unsupported SQLite schema version: {version}")


def _create_initial_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(_MIGRATION_1)
    except sqlite3.OperationalError as error:
        if "fts5" in str(error).lower():
            raise SchemaError("SQLite was built without required FTS5 support") from error
        raise
    connection.execute("INSERT INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,))
