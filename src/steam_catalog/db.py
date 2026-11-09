"""SQLite persistence for catalog games, reviews, search, and jobs."""

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from steam_catalog.errors import LeaseLostError
from steam_catalog.migrations import initialize_schema
from steam_catalog.models import DiscoveryItem, EligibilityStatus, Review
from steam_catalog.search import (
    delete_game_row,
    delete_review_row,
    insert_game_row,
    search_games,
    search_reviews,
)
from steam_catalog.utils import utc_now

DEFAULT_MAX_ATTEMPTS = 3


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
        """Create the schema, or migrate an existing database in place."""
        initialize_schema(self.connection)
        self._ensure_sync_job_lease_token()
        self._recover_stale_jobs()

    @contextmanager
    def transaction(self, *, on_commit: Callable[[], None] | None = None) -> Iterator[None]:
        """Run writes atomically, optionally publishing state after the outer commit."""
        if self.connection.in_transaction:
            if on_commit is not None:
                raise RuntimeError("on_commit requires an outermost transaction")
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
            self._commit_and_publish(on_commit)

    def _commit_and_publish(self, on_commit: Callable[[], None] | None) -> None:
        if on_commit is None:
            self.connection.commit()
            return

        committed = False
        interrupted: KeyboardInterrupt | None = None
        try:
            try:
                self.connection.commit()
                committed = True
            except KeyboardInterrupt as error:
                if self.connection.in_transaction:
                    self.connection.rollback()
                    raise
                committed = True
                interrupted = error
        finally:
            if committed:
                interrupted = self._finish_post_commit(on_commit, interrupted)

        if interrupted is not None:
            raise interrupted

    @staticmethod
    def _finish_post_commit(
        on_commit: Callable[[], None], interrupted: KeyboardInterrupt | None
    ) -> KeyboardInterrupt | None:
        """Finish an idempotent publication before replaying an interrupt."""
        while True:
            try:
                on_commit()
            except KeyboardInterrupt as error:
                interrupted = interrupted or error
            else:
                return interrupted

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
        prices_json: str | None = None,
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
            prices_json,
            None,
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
            delete_game_row(self.connection, appid)
            self.connection.execute(
                """
                INSERT INTO games(
                    appid, name, type, is_released, release_date, coming_soon,
                    short_description_html, short_description_text,
                    detailed_description_html, detailed_description_text,
                    about_the_game_html, about_the_game_text, developers_json,
                    publishers_json, languages_json, platforms_json, categories_json,
                    genres_json, header_image_url, screenshots_json, prices_json,
                    regional_prices_fetched_at,
                    steamspy_positive, steamspy_negative, steam_total_positive,
                    steam_total_negative, steam_total_reviews, review_score,
                    review_score_desc, eligibility_status, skip_reason,
                    metadata_fetched_at, reviews_fetched_at, updated_at
                ) VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    prices_json = excluded.prices_json,
                    regional_prices_fetched_at = NULL,
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
            delete_review_row(self.connection, recommendation_id)
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
                insert_game_row(self.connection, appid)
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
                    delete_review_row(self.connection, recommendation_id)
                    self.connection.execute(
                        "DELETE FROM reviews WHERE recommendation_id = ?",
                        (recommendation_id,),
                    )

    def compact_http_cache(self) -> int:
        """Remove legacy raw HTTP responses and reclaim their SQLite pages."""
        with self.transaction():
            cursor = self.connection.execute("DELETE FROM http_cache")
        checkpoint = self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and checkpoint[0] != 0:
            raise RuntimeError("Database is busy; stop other catalog processes and retry")
        self.connection.execute("VACUUM")
        return cursor.rowcount

    def upsert_source_item(
        self,
        source: str,
        item: DiscoveryItem,
        *,
        source_page: int | None = None,
        fetched_at: str | None = None,
    ) -> None:
        """Persist one discovery payload and a metadata-preserving minimal game row."""
        timestamp = fetched_at or utc_now()
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
                delete_game_row(self.connection, item.appid)
                self.connection.execute(
                    """
                    UPDATE games
                    SET name = ?,
                        steamspy_positive = COALESCE(?, steamspy_positive),
                        steamspy_negative = COALESCE(?, steamspy_negative)
                    WHERE appid = ?
                    """,
                    (
                        name,
                        item.steamspy_positive,
                        item.steamspy_negative,
                        item.appid,
                    ),
                )
                insert_game_row(self.connection, item.appid)

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
        timestamp = updated_at or utc_now()
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

    def schedule_refresh(
        self,
        cutoff: str,
        *,
        reason: str | None = None,
        include_failed: bool = False,
        updated_at: str | None = None,
    ) -> int:
        """Queue games selected by age or an exact skip reason."""
        timestamp = updated_at or utc_now()
        with self.transaction():
            rows = self.connection.execute(
                """
                SELECT CAST(games.appid AS TEXT) AS item_key
                FROM games
                LEFT JOIN sync_jobs
                  ON sync_jobs.job_kind = 'collect'
                 AND sync_jobs.item_key = CAST(games.appid AS TEXT)
                WHERE (
                        (
                            (
                                ? IS NULL
                                AND (
                                    games.reviews_fetched_at IS NULL
                                    OR julianday(games.reviews_fetched_at) < julianday(?)
                                )
                            )
                            OR (? IS NOT NULL AND games.skip_reason = ?)
                        )
                    AND COALESCE(sync_jobs.status, '') NOT IN ('running', 'permanent_failure')
                ) OR (? AND sync_jobs.status = 'permanent_failure')
                ORDER BY games.appid
                """,
                (reason, cutoff, reason, reason, include_failed),
            ).fetchall()
            for row in rows:
                self.enqueue_job("collect", row["item_key"], resume=False, updated_at=timestamp)
        return len(rows)

    def schedule_price_collection(self, *, updated_at: str | None = None) -> int:
        """Queue eligible games whose USD and UAH prices have not been collected."""
        timestamp = updated_at or utc_now()
        with self.transaction():
            rows = self.connection.execute(
                """
                SELECT CAST(games.appid AS TEXT) AS item_key
                FROM games
                LEFT JOIN sync_jobs
                  ON sync_jobs.job_kind = 'collect_prices'
                 AND sync_jobs.item_key = CAST(games.appid AS TEXT)
                WHERE games.eligibility_status = 'eligible'
                  AND games.regional_prices_fetched_at IS NULL
                  AND COALESCE(sync_jobs.status, '') NOT IN ('running', 'permanent_failure')
                ORDER BY games.appid
                """
            ).fetchall()
            for row in rows:
                status = self.job_status("collect_prices", row["item_key"])
                if status is None:
                    self.enqueue_job(
                        "collect_prices", row["item_key"], resume=True, updated_at=timestamp
                    )
                elif status == "complete":
                    self.enqueue_job(
                        "collect_prices", row["item_key"], resume=False, updated_at=timestamp
                    )
        return len(rows)

    def claim_job(
        self,
        job_kind: str,
        *,
        now: str | None = None,
        lease_token: str | None = None,
    ) -> sqlite3.Row | None:
        """Atomically claim the highest-priority due job within the attempt limit."""
        timestamp = now or utc_now()
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
                LEFT JOIN games ON games.appid = CAST(jobs.item_key AS INTEGER)
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
            selected_lease_token = lease_token or secrets.token_urlsafe(32)
            self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    lease_token = ?, next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                """,
                (selected_lease_token, timestamp, job_kind, candidate["item_key"]),
            )
            return self.connection.execute(
                "SELECT * FROM sync_jobs WHERE job_kind = ? AND item_key = ?",
                (job_kind, candidate["item_key"]),
            ).fetchone()

    def claim_specific_job(
        self,
        job_kind: str,
        item_key: str,
        *,
        now: str | None = None,
        lease_token: str | None = None,
    ) -> sqlite3.Row | None:
        """Atomically claim one requested due job without changing other queued work."""
        timestamp = now or utc_now()
        selected_lease_token = lease_token or secrets.token_urlsafe(32)
        with self.transaction():
            self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'permanent_failure', lease_token = NULL,
                    next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                  AND status IN ('pending', 'failed')
                  AND attempt_count >= ?
                """,
                (timestamp, job_kind, item_key, self.max_attempts),
            )
            cursor = self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    lease_token = ?, next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                  AND status IN ('pending', 'failed')
                  AND attempt_count < ?
                  AND (
                      next_attempt_at IS NULL
                      OR julianday(next_attempt_at) <= julianday(?)
                  )
                """,
                (
                    selected_lease_token,
                    timestamp,
                    job_kind,
                    item_key,
                    self.max_attempts,
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return self.connection.execute(
                "SELECT * FROM sync_jobs WHERE job_kind = ? AND item_key = ?",
                (job_kind, item_key),
            ).fetchone()

    def requeue_and_claim_specific_job(
        self,
        job_kind: str,
        item_key: str,
        *,
        now: str | None = None,
        lease_token: str | None = None,
    ) -> sqlite3.Row | None:
        """Reset and claim one explicit target unless another worker owns it."""
        timestamp = now or utc_now()
        selected_lease_token = lease_token or secrets.token_urlsafe(32)
        with self.transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO sync_jobs(
                    job_kind, item_key, status, attempt_count, next_attempt_at,
                    last_error, updated_at, lease_token
                ) VALUES(?, ?, 'running', 1, NULL, NULL, ?, ?)
                ON CONFLICT(job_kind, item_key) DO UPDATE SET
                    status = 'running',
                    attempt_count = 1,
                    next_attempt_at = NULL,
                    last_error = NULL,
                    updated_at = excluded.updated_at,
                    lease_token = excluded.lease_token
                WHERE sync_jobs.status <> 'running'
                """,
                (job_kind, item_key, timestamp, selected_lease_token),
            )
            if cursor.rowcount != 1:
                return None
            return self.connection.execute(
                "SELECT * FROM sync_jobs WHERE job_kind = ? AND item_key = ?",
                (job_kind, item_key),
            ).fetchone()

    def release_job(
        self,
        job_kind: str,
        item_key: str,
        *,
        expected_lease_token: str,
        updated_at: str | None = None,
    ) -> None:
        """Return interrupted work to the pending queue without consuming an attempt."""
        with self.transaction():
            cursor = self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'pending', attempt_count = MAX(0, attempt_count - 1),
                    lease_token = NULL, next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ? AND item_key = ?
                  AND status = 'running' AND lease_token = ?
                """,
                (
                    updated_at or utc_now(),
                    job_kind,
                    item_key,
                    expected_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"Lost job lease: {job_kind}/{item_key}")

    def release_job_by_lease(
        self,
        job_kind: str,
        lease_token: str,
        *,
        updated_at: str | None = None,
    ) -> bool:
        """Release interrupted work when only its preallocated lease token is known."""
        with self.transaction():
            cursor = self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'pending', attempt_count = MAX(0, attempt_count - 1),
                    lease_token = NULL, next_attempt_at = NULL, updated_at = ?
                WHERE job_kind = ? AND status = 'running' AND lease_token = ?
                """,
                (updated_at or utc_now(), job_kind, lease_token),
            )
        return cursor.rowcount == 1

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
                    updated_at or utc_now(),
                    job_kind,
                    item_key,
                    expected_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"Lost job lease: {job_kind}/{item_key}")

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
                    updated_at or utc_now(),
                    job_kind,
                    item_key,
                    expected_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"Lost job lease: {job_kind}/{item_key}")

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
        timestamp = updated_at or utc_now()
        with self.transaction():
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO games(appid, name, eligibility_status, updated_at)
                VALUES(?, '', ?, ?)
                """,
                (appid, status, timestamp),
            )
            if inserted.rowcount > 0:
                insert_game_row(self.connection, appid)
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

    def update_regional_prices(
        self,
        appid: int,
        regional_prices_json: str,
        *,
        fetched_at: str,
        expected_lease_token: str | None = None,
    ) -> None:
        """Merge USD/UAH into the current KZT snapshot and complete its job."""
        regional_prices = json.loads(regional_prices_json)
        if not isinstance(regional_prices, dict) or set(regional_prices) != {"USD", "UAH"}:
            raise ValueError("Regional prices must contain exactly USD and UAH")

        with self.transaction():
            row = self.connection.execute(
                "SELECT eligibility_status, prices_json FROM games WHERE appid = ?", (appid,)
            ).fetchone()
            if row is None or row["eligibility_status"] != "eligible":
                raise ValueError(f"appid {appid} is not an eligible game")
            current_prices = json.loads(row["prices_json"] or "{}")
            if not isinstance(current_prices, dict) or "KZT" not in current_prices:
                raise ValueError(f"appid {appid} has no KZT price snapshot")
            merged_prices = {"KZT": current_prices["KZT"], **regional_prices}
            self.connection.execute(
                """
                UPDATE games
                SET prices_json = ?, regional_prices_fetched_at = ?, updated_at = ?
                WHERE appid = ?
                """,
                (
                    json.dumps(merged_prices, ensure_ascii=False, separators=(",", ":")),
                    fetched_at,
                    fetched_at,
                    appid,
                ),
            )
            if expected_lease_token is not None:
                self.complete_job(
                    "collect_prices",
                    str(appid),
                    expected_lease_token=expected_lease_token,
                    updated_at=fetched_at,
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

    def job_queue_size(self, job_kind: str) -> int:
        """Return jobs of one kind that remain eligible for a future claim."""
        return self.connection.execute(
            """
            SELECT COUNT(*)
            FROM sync_jobs
            WHERE job_kind = ?
              AND status IN ('pending', 'failed')
              AND attempt_count < ?
            """,
            (job_kind, self.max_attempts),
        ).fetchone()[0]

    def collection_queue_size(self) -> int:
        """Return collection jobs that remain eligible for a future claim."""
        return self.job_queue_size("collect")

    def game_progress_details(self, appid: int) -> tuple[str | None, int, int]:
        """Return display-safe post-commit details for collection progress."""
        row = self.connection.execute(
            """
            SELECT games.name, COALESCE(games.steam_total_reviews, 0) AS total_reviews,
                   COUNT(reviews.recommendation_id) AS sample_count
            FROM games
            LEFT JOIN reviews ON reviews.appid = games.appid
            WHERE games.appid = ?
            GROUP BY games.appid, games.name, games.steam_total_reviews
            """,
            (appid,),
        ).fetchone()
        if row is None:
            return None, 0, 0
        return row["name"], row["total_reviews"], row["sample_count"]

    def has_authoritative_verification(self, appid: int) -> bool:
        """Return whether Steam-owned review data proves prior verification."""
        row = self.connection.execute(
            """
            SELECT 1
            FROM games
            WHERE appid = ?
              AND (reviews_fetched_at IS NOT NULL OR steam_total_reviews IS NOT NULL)
            """,
            (appid,),
        ).fetchone()
        return row is not None

    def game_status(self, appid: int) -> str | None:
        row = self.connection.execute(
            "SELECT eligibility_status FROM games WHERE appid = ?", (appid,)
        ).fetchone()
        return None if row is None else row["eligibility_status"]

    def review_count(self, appid: int) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM reviews WHERE appid = ?", (appid,)
        ).fetchone()[0]

    def status_counts(self) -> dict[str, int]:
        """Return the stable user-facing catalog and collection status categories."""
        game_categories = {
            "eligible": ("eligibility_status = ?", ("eligible",)),
            "provisional-below-threshold": (
                "eligibility_status = ?",
                ("below_provisional_threshold",),
            ),
            "verified-below-threshold": (
                "eligibility_status = ?",
                ("below_verified_threshold",),
            ),
            "skipped": ("eligibility_status = ?", ("ineligible",)),
            "metadata-complete": ("metadata_fetched_at IS NOT NULL", ()),
            "review-complete": ("reviews_fetched_at IS NOT NULL", ()),
        }
        counts = {
            label: self.connection.execute(
                f"SELECT COUNT(*) FROM games WHERE {predicate}", parameters
            ).fetchone()[0]
            for label, (predicate, parameters) in game_categories.items()
        }
        for label, status in {
            "pending": "pending",
            "running": "running",
            "retrying": "failed",
            "permanent-failure": "permanent_failure",
        }.items():
            counts[label] = self.connection.execute(
                "SELECT COUNT(*) FROM sync_jobs WHERE status = ?", (status,)
            ).fetchone()[0]
        return counts

    def search_games(self, query: str, limit: int) -> list[dict[str, object]]:
        """Return BM25-ranked game matches with source-backed highlighted snippets."""
        return search_games(self.connection, query, limit)

    def search_reviews(self, query: str, limit: int) -> list[dict[str, object]]:
        """Return BM25-ranked review matches with game and sample context."""
        return search_reviews(self.connection, query, limit)

    def _ensure_sync_job_lease_token(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(sync_jobs)")}
        if "lease_token" not in columns:
            self.connection.execute("ALTER TABLE sync_jobs ADD COLUMN lease_token TEXT")

    def _recover_stale_jobs(self) -> None:
        now = datetime.now(UTC)
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
