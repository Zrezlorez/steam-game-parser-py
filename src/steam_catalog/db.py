"""SQLite persistence and schema migration support."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sqlite3
import unicodedata

from steam_catalog.models import DiscoveryItem, EligibilityStatus, Review


SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3
_SNIPPET_CONTEXT = 80


@dataclass(frozen=True)
class _SearchToken:
    folded: str
    start: int
    end: int


MIGRATION_1 = """
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
    price_json TEXT,
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


class Database:
    """Connection wrapper that owns schema initialization and atomic writes."""

    def __init__(self, path: Path, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.path = path.resolve()
        self.max_attempts = max_attempts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self._savepoint_counter = 0
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
            self.connection.execute(
                "INSERT INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,)
            )
        else:
            version = version_row["version"]
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported SQLite schema version: {version}")

        self._ensure_sync_job_lease_token()
        self._recover_stale_jobs()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a group of writes atomically using SQLite's immediate lock mode."""
        if self.connection.in_transaction:
            self._savepoint_counter += 1
            savepoint = f"steam_catalog_{self._savepoint_counter}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return

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
        type: str | None = None,
        is_released: bool | None = None,
        release_date: str | None = None,
        coming_soon: bool | None = None,
        short_description_html: str | None = None,
        short_description_text: str | None = None,
        detailed_description_html: str | None = None,
        detailed_description_text: str | None = None,
        about_the_game_html: str | None = None,
        about_the_game_text: str | None = None,
        developers_json: str | None = None,
        publishers_json: str | None = None,
        languages_json: str | None = None,
        platforms_json: str | None = None,
        categories_json: str | None = None,
        genres_json: str | None = None,
        header_image_url: str | None = None,
        screenshots_json: str | None = None,
        price_json: str | None = None,
        steamspy_positive: int | None = None,
        steamspy_negative: int | None = None,
        steam_total_positive: int | None = None,
        steam_total_negative: int | None = None,
        steam_total_reviews: int | None = None,
        review_score: int | None = None,
        review_score_desc: str | None = None,
        skip_reason: str | None = None,
        metadata_fetched_at: str | None = None,
        reviews_fetched_at: str | None = None,
    ) -> None:
        """Persist approved game metadata and replace its full-text-search entry."""
        status = (
            eligibility_status.value
            if isinstance(eligibility_status, EligibilityStatus)
            else eligibility_status
        )
        values = (
            appid,
            name,
            type,
            is_released,
            release_date,
            coming_soon,
            short_description_html,
            short_description_text,
            detailed_description_html,
            detailed_description_text,
            about_the_game_html,
            about_the_game_text,
            developers_json,
            publishers_json,
            languages_json,
            platforms_json,
            categories_json,
            genres_json,
            header_image_url,
            screenshots_json,
            price_json,
            steamspy_positive,
            steamspy_negative,
            steam_total_positive,
            steam_total_negative,
            steam_total_reviews,
            review_score,
            review_score_desc,
            status,
            skip_reason,
            metadata_fetched_at,
            reviews_fetched_at,
            updated_at,
        )
        with self.transaction():
            self._delete_game_search_row(appid)
            self.connection.execute(
                """
                INSERT INTO games VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(appid) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    is_released = excluded.is_released,
                    release_date = excluded.release_date,
                    coming_soon = excluded.coming_soon,
                    short_description_html = excluded.short_description_html,
                    short_description_text = excluded.short_description_text,
                    detailed_description_html = excluded.detailed_description_html,
                    detailed_description_text = excluded.detailed_description_text,
                    about_the_game_html = excluded.about_the_game_html,
                    about_the_game_text = excluded.about_the_game_text,
                    developers_json = excluded.developers_json,
                    publishers_json = excluded.publishers_json,
                    languages_json = excluded.languages_json,
                    platforms_json = excluded.platforms_json,
                    categories_json = excluded.categories_json,
                    genres_json = excluded.genres_json,
                    header_image_url = excluded.header_image_url,
                    screenshots_json = excluded.screenshots_json,
                    price_json = excluded.price_json,
                    steamspy_positive = COALESCE(
                        excluded.steamspy_positive, games.steamspy_positive
                    ),
                    steamspy_negative = COALESCE(
                        excluded.steamspy_negative, games.steamspy_negative
                    ),
                    steam_total_positive = excluded.steam_total_positive,
                    steam_total_negative = excluded.steam_total_negative,
                    steam_total_reviews = excluded.steam_total_reviews,
                    review_score = excluded.review_score,
                    review_score_desc = excluded.review_score_desc,
                    eligibility_status = excluded.eligibility_status,
                    skip_reason = excluded.skip_reason,
                    metadata_fetched_at = excluded.metadata_fetched_at,
                    reviews_fetched_at = excluded.reviews_fetched_at,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            self.connection.execute(
                """
                INSERT INTO game_search(
                    rowid, name, short_description_text, detailed_description_text,
                    about_the_game_text, developers, publishers, genres, categories
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    appid,
                    name,
                    short_description_text or "",
                    detailed_description_text or "",
                    about_the_game_text or "",
                    developers_json or "",
                    publishers_json or "",
                    genres_json or "",
                    categories_json or "",
                ),
            )

    def upsert_review(
        self,
        *,
        recommendation_id: str,
        appid: int,
        review_text: str,
        voted_up: bool,
        fetched_at: str,
        language: str | None = None,
        votes_up: int | None = None,
        votes_funny: int | None = None,
        weighted_vote_score: float | None = None,
        comment_count: int | None = None,
        steam_purchase: bool | None = None,
        received_for_free: bool | None = None,
        written_during_early_access: bool | None = None,
        author_steamid: str | None = None,
        author_playtime_forever: int | None = None,
        author_playtime_at_review: int | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        """Persist approved review metadata and replace its full-text-search entry."""
        values = (
            recommendation_id,
            appid,
            language,
            review_text,
            int(voted_up),
            votes_up,
            votes_funny,
            weighted_vote_score,
            comment_count,
            steam_purchase,
            received_for_free,
            written_during_early_access,
            author_steamid,
            author_playtime_forever,
            author_playtime_at_review,
            created_at,
            updated_at,
            fetched_at,
        )
        with self.transaction():
            self._delete_review_search_row(recommendation_id)
            self.connection.execute(
                """
                INSERT INTO reviews VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(recommendation_id) DO UPDATE SET
                    appid = excluded.appid,
                    language = excluded.language,
                    review_text = excluded.review_text,
                    voted_up = excluded.voted_up,
                    votes_up = excluded.votes_up,
                    votes_funny = excluded.votes_funny,
                    weighted_vote_score = excluded.weighted_vote_score,
                    comment_count = excluded.comment_count,
                    steam_purchase = excluded.steam_purchase,
                    received_for_free = excluded.received_for_free,
                    written_during_early_access = excluded.written_during_early_access,
                    author_steamid = excluded.author_steamid,
                    author_playtime_forever = excluded.author_playtime_forever,
                    author_playtime_at_review = excluded.author_playtime_at_review,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    fetched_at = excluded.fetched_at
                """,
                values,
            )
            rowid = self.connection.execute(
                "SELECT rowid FROM reviews WHERE recommendation_id = ?", (recommendation_id,)
            ).fetchone()[0]
            self.connection.execute(
                """
                INSERT INTO review_search(rowid, appid, recommendation_id, review_text)
                VALUES(?, ?, ?, ?)
                """,
                (rowid, appid, recommendation_id, review_text),
            )

    def upsert_library_entry(
        self,
        *,
        appid: int,
        name_at_import: str,
        playtime_forever_minutes: int,
        playtime_2weeks_minutes: int | None,
        last_played_at: str | None,
        platform_playtime_json: str | None,
        source_payload_json: str,
        imported_at: str,
    ) -> bool:
        """Upsert one personal library row without replacing catalog-owned game metadata."""
        with self.transaction():
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO games(appid, name, eligibility_status, updated_at)
                VALUES(?, ?, 'pending', ?)
                """,
                (appid, name_at_import, imported_at),
            )
            if inserted.rowcount > 0:
                self._insert_game_search_row(appid)
            self.connection.execute(
                """
                INSERT INTO user_library(
                    appid, name_at_import, playtime_forever_minutes, playtime_2weeks_minutes,
                    last_played_at, platform_playtime_json, source_payload_json, imported_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appid) DO UPDATE SET
                    name_at_import = excluded.name_at_import,
                    playtime_forever_minutes = excluded.playtime_forever_minutes,
                    playtime_2weeks_minutes = excluded.playtime_2weeks_minutes,
                    last_played_at = excluded.last_played_at,
                    platform_playtime_json = excluded.platform_playtime_json,
                    source_payload_json = excluded.source_payload_json,
                    imported_at = excluded.imported_at
                """,
                (
                    appid,
                    name_at_import,
                    playtime_forever_minutes,
                    playtime_2weeks_minutes,
                    last_played_at,
                    platform_playtime_json,
                    source_payload_json,
                    imported_at,
                ),
            )
        return inserted.rowcount > 0

    def replace_review_samples(
        self,
        appid: int,
        samples: Mapping[str, Sequence[Review]],
        *,
        fetched_at: str,
    ) -> None:
        """Replace sample memberships while storing each unique review once."""
        unique_reviews: dict[str, Review] = {}
        for reviews in samples.values():
            for review in reviews:
                if review.appid != appid:
                    raise ValueError("sample review appid does not match collection appid")
                unique_reviews.setdefault(review.recommendation_id, review)

        with self.transaction():
            old_review_ids = [
                row["recommendation_id"]
                for row in self.connection.execute(
                    "SELECT DISTINCT recommendation_id FROM review_samples WHERE appid = ?",
                    (appid,),
                )
            ]
            for review in unique_reviews.values():
                self.upsert_review(**asdict(review), fetched_at=fetched_at)
            self.connection.execute("DELETE FROM review_samples WHERE appid = ?", (appid,))
            for sample_kind, reviews in samples.items():
                self.connection.executemany(
                    """
                    INSERT INTO review_samples(
                        appid, recommendation_id, sample_kind, sample_rank
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        (appid, review.recommendation_id, sample_kind, rank)
                        for rank, review in enumerate(reviews, start=1)
                    ),
                )
            for recommendation_id in old_review_ids:
                has_membership = self.connection.execute(
                    "SELECT 1 FROM review_samples WHERE recommendation_id = ? LIMIT 1",
                    (recommendation_id,),
                ).fetchone()
                if has_membership is None:
                    self._delete_review_search_row(recommendation_id)
                    self.connection.execute(
                        "DELETE FROM reviews WHERE recommendation_id = ?",
                        (recommendation_id,),
                    )

    def get_http_cache(self, cache_key: str) -> sqlite3.Row | None:
        """Return the cached response for a canonical request key, if present."""
        return self.connection.execute(
            "SELECT * FROM http_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()

    def upsert_http_cache(
        self,
        *,
        cache_key: str,
        url: str,
        status_code: int,
        response_body: str,
        etag: str | None,
        last_modified: str | None,
        fetched_at: str,
        expires_at: str,
    ) -> None:
        """Store a UTF-8 HTTP response and its freshness metadata."""
        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO http_cache(
                    cache_key, url, status_code, response_body, etag, last_modified,
                    fetched_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    url = excluded.url,
                    status_code = excluded.status_code,
                    response_body = excluded.response_body,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (
                    cache_key,
                    url,
                    status_code,
                    response_body,
                    etag,
                    last_modified,
                    fetched_at,
                    expires_at,
                ),
            )

    def upsert_source_item(
        self,
        source: str,
        item: DiscoveryItem,
        *,
        source_page: int | None = None,
        fetched_at: str | None = None,
    ) -> None:
        """Persist one discovery payload and a metadata-preserving minimal game row."""
        timestamp = fetched_at or _utc_now()
        with self.transaction():
            existing = self.connection.execute(
                "SELECT name FROM games WHERE appid = ?", (item.appid,)
            ).fetchone()
            if existing is None:
                self.upsert_game(
                    appid=item.appid,
                    name=item.name,
                    eligibility_status="pending",
                    updated_at=timestamp,
                    steamspy_positive=item.steamspy_positive,
                    steamspy_negative=item.steamspy_negative,
                )
            else:
                name = existing["name"] or item.name
                self._delete_game_search_row(item.appid)
                self.connection.execute(
                    """
                    UPDATE games
                    SET name = ?,
                        steamspy_positive = COALESCE(?, steamspy_positive),
                        steamspy_negative = COALESCE(?, steamspy_negative),
                        updated_at = ?
                    WHERE appid = ?
                    """,
                    (
                        name,
                        item.steamspy_positive,
                        item.steamspy_negative,
                        timestamp,
                        item.appid,
                    ),
                )
                self._insert_game_search_row(item.appid)

            self.connection.execute(
                """
                INSERT INTO source_items(source, appid, source_page, payload_json, fetched_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(source, appid) DO UPDATE SET
                    source_page = excluded.source_page,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (source, item.appid, source_page, item.payload_json, timestamp),
            )

    def enqueue_job(
        self,
        job_kind: str,
        item_key: str,
        *,
        resume: bool = True,
        updated_at: str | None = None,
    ) -> bool:
        """Queue work, preserving existing progress when resuming."""
        timestamp = updated_at or _utc_now()
        with self.transaction():
            if resume:
                cursor = self.connection.execute(
                    """
                    INSERT INTO sync_jobs(job_kind, item_key, status, updated_at)
                    VALUES(?, ?, 'pending', ?)
                    ON CONFLICT(job_kind, item_key) DO NOTHING
                    """,
                    (job_kind, item_key, timestamp),
                )
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO sync_jobs(job_kind, item_key, status, updated_at)
                    VALUES(?, ?, 'pending', ?)
                    ON CONFLICT(job_kind, item_key) DO UPDATE SET
                        status = 'pending',
                        attempt_count = 0,
                        lease_token = NULL,
                        next_attempt_at = NULL,
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (job_kind, item_key, timestamp),
                )
            return cursor.rowcount > 0

    def discard_job(self, job_kind: str, item_key: str) -> None:
        """Remove work that no longer satisfies provisional eligibility."""
        with self.transaction():
            self.connection.execute(
                "DELETE FROM sync_jobs WHERE job_kind = ? AND item_key = ?",
                (job_kind, item_key),
            )

    def claim_job(self, job_kind: str, *, now: str | None = None) -> sqlite3.Row | None:
        """Atomically claim the highest-priority due job within the attempt limit."""
        timestamp = now or _utc_now()
        with self.transaction():
            self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'permanent_failure', lease_token = NULL,
                    next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ?
                  AND status IN ('pending', 'failed')
                  AND attempt_count >= ?
                """,
                (timestamp, job_kind, self.max_attempts),
            )
            candidate = self.connection.execute(
                """
                SELECT jobs.item_key
                FROM sync_jobs AS jobs
                LEFT JOIN games
                  ON jobs.job_kind = 'collect'
                 AND CAST(games.appid AS TEXT) = jobs.item_key
                WHERE jobs.job_kind = ?
                  AND jobs.status IN ('pending', 'failed')
                  AND jobs.attempt_count < ?
                  AND (
                      jobs.next_attempt_at IS NULL
                      OR julianday(jobs.next_attempt_at) <= julianday(?)
                  )
                ORDER BY
                    COALESCE(
                        games.steam_total_reviews,
                        games.steamspy_positive + games.steamspy_negative,
                        0
                    ) DESC,
                    julianday(jobs.updated_at),
                    jobs.item_key
                LIMIT 1
                """,
                (job_kind, self.max_attempts, timestamp),
            ).fetchone()
            if candidate is None:
                return None
            lease_token = secrets.token_urlsafe(32)
            self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    lease_token = ?, next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                """,
                (lease_token, timestamp, job_kind, candidate["item_key"]),
            )
            return self.connection.execute(
                "SELECT * FROM sync_jobs WHERE job_kind = ? AND item_key = ?",
                (job_kind, candidate["item_key"]),
            ).fetchone()

    def complete_job(
        self,
        job_kind: str,
        item_key: str,
        *,
        expected_lease_token: str,
        updated_at: str | None = None,
    ) -> None:
        """Complete a running job only when the caller still owns its lease."""
        with self.transaction():
            cursor = self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'complete', lease_token = NULL, next_attempt_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                  AND status = 'running' AND lease_token = ?
                """,
                (
                    updated_at or _utc_now(),
                    job_kind,
                    item_key,
                    expected_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Lost job lease: {job_kind}/{item_key}")

    def fail_job(
        self,
        job_kind: str,
        item_key: str,
        error: BaseException | str,
        *,
        expected_lease_token: str,
        next_attempt_at: str,
        max_attempts: int,
        updated_at: str | None = None,
    ) -> None:
        """Schedule a bounded failure for retry or make it permanently terminal."""
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        with self.transaction():
            cursor = self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = CASE
                        WHEN attempt_count >= ? THEN 'permanent_failure'
                        ELSE 'failed'
                    END,
                    lease_token = NULL,
                    next_attempt_at = CASE
                        WHEN attempt_count >= ? THEN NULL
                        ELSE ?
                    END,
                    last_error = ?, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                  AND status = 'running' AND lease_token = ?
                """,
                (
                    max_attempts,
                    max_attempts,
                    next_attempt_at,
                    str(error)[:1000],
                    updated_at or _utc_now(),
                    job_kind,
                    item_key,
                    expected_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Lost job lease: {job_kind}/{item_key}")

    def record_game_status(
        self,
        appid: int,
        status: str,
        *,
        skip_reason: str | None = None,
        updated_at: str | None = None,
        steam_total_positive: int | None = None,
        steam_total_negative: int | None = None,
        steam_total_reviews: int | None = None,
        review_score: int | None = None,
        review_score_desc: str | None = None,
        reviews_fetched_at: str | None = None,
        replace_steam_aggregate: bool = False,
    ) -> None:
        """Update eligibility and aggregate fields without replacing prior metadata."""
        timestamp = updated_at or _utc_now()
        with self.transaction():
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO games(appid, name, eligibility_status, updated_at)
                VALUES(?, '', ?, ?)
                """,
                (appid, status, timestamp),
            )
            if inserted.rowcount > 0:
                self._insert_game_search_row(appid)
            if replace_steam_aggregate:
                self.connection.execute(
                    """
                    UPDATE games
                    SET steam_total_positive = ?, steam_total_negative = ?,
                        steam_total_reviews = ?, review_score = ?, review_score_desc = ?,
                        eligibility_status = ?, skip_reason = ?, reviews_fetched_at = ?,
                        updated_at = ?
                    WHERE appid = ?
                    """,
                    (
                        steam_total_positive,
                        steam_total_negative,
                        steam_total_reviews,
                        review_score,
                        review_score_desc,
                        status,
                        skip_reason,
                        reviews_fetched_at,
                        timestamp,
                        appid,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE games
                    SET eligibility_status = ?, skip_reason = ?, updated_at = ?
                    WHERE appid = ?
                    """,
                    (status, skip_reason, timestamp, appid),
                )

    def next_source_page(self, source: str) -> int:
        """Return the first SteamSpy page not represented in source items."""
        row = self.connection.execute(
            "SELECT MAX(source_page) FROM source_items WHERE source = ?", (source,)
        ).fetchone()
        return 0 if row[0] is None else row[0] + 1

    def job_status(self, job_kind: str, item_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM sync_jobs WHERE job_kind = ? AND item_key = ?",
            (job_kind, item_key),
        ).fetchone()
        return None if row is None else row["status"]

    def game_status(self, appid: int) -> str | None:
        row = self.connection.execute(
            "SELECT eligibility_status FROM games WHERE appid = ?", (appid,)
        ).fetchone()
        return None if row is None else row["eligibility_status"]

    def review_count(self, appid: int) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM reviews WHERE appid = ?", (appid,)
        ).fetchone()[0]

    def search_games(self, query: str, limit: int) -> list[dict[str, object]]:
        """Return BM25-ranked game matches with source-backed highlighted snippets."""
        match_query, tokens = _quoted_match_query(query)
        if not match_query or limit <= 0:
            return []
        rows = self.connection.execute(
            """
            SELECT
                games.appid, games.name, bm25(game_search) AS rank,
                games.short_description_text, games.detailed_description_text,
                games.about_the_game_text, games.developers_json, games.publishers_json,
                games.genres_json, games.categories_json
            FROM game_search
            JOIN games ON games.appid = game_search.rowid
            WHERE game_search MATCH ?
            ORDER BY rank, games.appid
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        return [
            {
                "appid": row["appid"],
                "name": row["name"],
                "rank": row["rank"],
                "snippet": _highlighted_snippet(
                    (
                        row["name"],
                        row["short_description_text"],
                        row["detailed_description_text"],
                        row["about_the_game_text"],
                        row["developers_json"],
                        row["publishers_json"],
                        row["genres_json"],
                        row["categories_json"],
                    ),
                    tokens,
                ),
            }
            for row in rows
        ]

    def search_reviews(self, query: str, limit: int) -> list[dict[str, object]]:
        """Return BM25-ranked review matches with game and sample context."""
        match_query, tokens = _quoted_match_query(query)
        if not match_query or limit <= 0:
            return []
        rows = self.connection.execute(
            """
            SELECT
                reviews.appid, games.name, reviews.recommendation_id,
                reviews.review_text, bm25(review_search) AS rank
            FROM review_search
            JOIN reviews ON reviews.rowid = review_search.rowid
            JOIN games ON games.appid = reviews.appid
            WHERE review_search MATCH ?
            ORDER BY rank, reviews.recommendation_id
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        memberships_by_review: dict[str, list[dict[str, object]]] = {
            row["recommendation_id"]: [] for row in rows
        }
        review_ids = tuple(memberships_by_review)
        parameter_limit = max(1, self.connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
        for offset in range(0, len(review_ids), parameter_limit):
            chunk = review_ids[offset : offset + parameter_limit]
            placeholders = ", ".join("?" for _ in chunk)
            memberships = self.connection.execute(
                f"""
                SELECT recommendation_id, sample_kind, sample_rank
                FROM review_samples
                WHERE recommendation_id IN ({placeholders})
                ORDER BY recommendation_id, sample_kind, sample_rank
                """,
                chunk,
            ).fetchall()
            for membership in memberships:
                memberships_by_review[membership["recommendation_id"]].append(
                    {
                        "sample_kind": membership["sample_kind"],
                        "sample_rank": membership["sample_rank"],
                    }
                )
        results = []
        for row in rows:
            results.append(
                {
                    "appid": row["appid"],
                    "name": row["name"],
                    "recommendation_id": row["recommendation_id"],
                    "rank": row["rank"],
                    "snippet": _highlighted_snippet((row["review_text"],), tokens),
                    "samples": memberships_by_review[row["recommendation_id"]],
                }
            )
        return results

    def _delete_game_search_row(self, appid: int) -> None:
        self.connection.execute(
            """
            INSERT INTO game_search(
                game_search, rowid, name, short_description_text, detailed_description_text,
                about_the_game_text, developers, publishers, genres, categories
            )
            SELECT
                'delete', appid, name, COALESCE(short_description_text, ''),
                COALESCE(detailed_description_text, ''), COALESCE(about_the_game_text, ''),
                COALESCE(developers_json, ''), COALESCE(publishers_json, ''),
                COALESCE(genres_json, ''), COALESCE(categories_json, '')
            FROM games
            WHERE appid = ?
            """,
            (appid,),
        )

    def _delete_review_search_row(self, recommendation_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO review_search(
                review_search, rowid, appid, recommendation_id, review_text
            )
            SELECT 'delete', rowid, appid, recommendation_id, review_text
            FROM reviews
            WHERE recommendation_id = ?
            """,
            (recommendation_id,),
        )

    def _insert_game_search_row(self, appid: int) -> None:
        self.connection.execute(
            """
            INSERT INTO game_search(
                rowid, name, short_description_text, detailed_description_text,
                about_the_game_text, developers, publishers, genres, categories
            )
            SELECT
                appid, name, COALESCE(short_description_text, ''),
                COALESCE(detailed_description_text, ''), COALESCE(about_the_game_text, ''),
                COALESCE(developers_json, ''), COALESCE(publishers_json, ''),
                COALESCE(genres_json, ''), COALESCE(categories_json, '')
            FROM games
            WHERE appid = ?
            """,
            (appid,),
        )

    def _ensure_sync_job_lease_token(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(sync_jobs)")}
        if "lease_token" not in columns:
            self.connection.execute("ALTER TABLE sync_jobs ADD COLUMN lease_token TEXT")

    def _recover_stale_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=1)).isoformat()
        self.connection.execute(
            """
            UPDATE sync_jobs
            SET status = CASE
                    WHEN attempt_count >= ? THEN 'permanent_failure'
                    ELSE 'pending'
                END,
                lease_token = NULL,
                next_attempt_at = NULL,
                updated_at = ?
            WHERE status = 'running' AND julianday(updated_at) < julianday(?)
            """,
            (self.max_attempts, now.isoformat(), cutoff),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quoted_match_query(query: str) -> tuple[str, tuple[_SearchToken, ...]]:
    tokens = _unicode61_tokens(query)
    match_query = " ".join(f'"{token.folded.replace(chr(34), chr(34) * 2)}"' for token in tokens)
    return match_query, tokens


def _highlighted_snippet(values: Sequence[str | None], query_tokens: Sequence[_SearchToken]) -> str:
    query_terms = frozenset(token.folded for token in query_tokens)
    if not query_terms:
        return next((value[:_SNIPPET_CONTEXT] for value in values if value), "")
    text = next((value for value in values if value), "")
    matches: tuple[_SearchToken, ...] = ()
    for value in values:
        if not value:
            continue
        candidate_matches = tuple(
            token for token in _unicode61_tokens(value) if token.folded in query_terms
        )
        if candidate_matches:
            text = value
            matches = candidate_matches
            break
    if not matches:
        return text[:_SNIPPET_CONTEXT]

    match_start = matches[0].start
    match_end = matches[0].end
    start = max(0, match_start - _SNIPPET_CONTEXT)
    end = min(len(text), match_end + _SNIPPET_CONTEXT)
    if start:
        next_space = text.find(" ", start)
        start = next_space + 1 if next_space >= 0 and next_space < match_start else start
    if end < len(text):
        previous_space = text.rfind(" ", match_end, end)
        end = previous_space if previous_space >= match_end else end
    highlighted_parts = []
    cursor = start
    for token in matches:
        highlight_start, highlight_end = token.start, token.end
        if highlight_start >= end or highlight_end <= start:
            continue
        if highlight_start < cursor:
            continue
        highlighted_parts.extend(
            (text[cursor:highlight_start], "<mark>", text[highlight_start:highlight_end], "</mark>")
        )
        cursor = highlight_end
    highlighted_parts.append(text[cursor:end])
    highlighted = "".join(highlighted_parts)
    return f"{'... ' if start else ''}{highlighted}{' ...' if end < len(text) else ''}"


def _unicode61_tokens(text: str) -> tuple[_SearchToken, ...]:
    tokens: list[_SearchToken] = []
    folded_parts: list[str] = []
    token_start: int | None = None
    token_end = 0

    def append_token() -> None:
        nonlocal token_start
        if token_start is not None and (folded := "".join(folded_parts)):
            tokens.append(_SearchToken(folded, token_start, token_end))
        folded_parts.clear()
        token_start = None

    for index, character in enumerate(text):
        category = unicodedata.category(character)
        is_token_character = category[0] in {"L", "N"} or category == "Co"
        if is_token_character:
            if token_start is None:
                token_start = index
            folded_parts.extend(_unicode61_fold(character))
            token_end = index + 1
        elif category[0] == "M" and token_start is not None:
            token_end = index + 1
        else:
            append_token()
    append_token()
    return tuple(tokens)


def _unicode61_fold(character: str) -> str:
    decomposed = unicodedata.normalize("NFD", character.lower())
    return "".join(part for part in decomposed if unicodedata.category(part)[0] != "M")
