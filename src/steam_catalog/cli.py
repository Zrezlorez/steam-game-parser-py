"""Typer command-line interface for the Steam catalog collector."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import wraps
from pathlib import Path
import secrets
import sqlite3
from typing import Annotated, Any, ParamSpec, TypeVar

import httpx
import typer

from steam_catalog.config import Settings
from steam_catalog.db import Database
from steam_catalog.exporters import export_csv, export_jsonl
from steam_catalog.http import CachedHttpClient
from steam_catalog.importers.library import import_library
from steam_catalog.pipeline import CatalogPipeline
from steam_catalog.sources.steam import SteamClient
from steam_catalog.sources.steamspy import SteamSpyClient


class DiscoverySource(str, Enum):
    STEAMSPY = "steamspy"
    STEAM = "steam"


class ExportFormat(str, Enum):
    JSONL = "jsonl"
    CSV = "csv"


_P = ParamSpec("_P")
_R = TypeVar("_R")
_EXPECTED_ERRORS = (ValueError, OSError, RuntimeError, sqlite3.Error, httpx.HTTPError)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build and maintain a local searchable Steam catalog.",
)


def _command_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except typer.Exit:
            raise
        except _EXPECTED_ERRORS as error:
            typer.echo(f"Error: {error}")
            raise typer.Exit(1) from error

    return wrapped


@app.callback()
@_command_errors
def main(
    context: typer.Context,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="SQLite catalog path.", dir_okay=False),
    ] = None,
) -> None:
    """Load settings shared by every command."""
    context.obj = Settings.from_env(database)


@contextmanager
def _database(settings: Settings) -> Iterator[Database]:
    database = Database(settings.database, max_attempts=settings.max_attempts)
    try:
        database.initialize()
        yield database
    finally:
        database.close()


@contextmanager
def _pipeline(database: Database, settings: Settings) -> Iterator[CatalogPipeline]:
    http = CachedHttpClient(database=database, settings=settings)
    try:
        yield CatalogPipeline(
            database,
            steam=SteamClient(http),
            steamspy=SteamSpyClient(http),
        )
    finally:
        http.close()


@app.command("init")
@_command_errors
def init_command(context: typer.Context) -> None:
    """Initialize the catalog database schema."""
    settings: Settings = context.obj
    with _database(settings) as database:
        typer.echo(f"Initialized: {database.path}")


@app.command("bootstrap")
@_command_errors
def bootstrap_command(
    context: typer.Context,
    source: Annotated[DiscoverySource, typer.Option("--source")] = DiscoverySource.STEAMSPY,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    page_limit: Annotated[int | None, typer.Option("--page-limit", min=1)] = None,
) -> None:
    """Discover catalog candidates and queue eligible collection work."""
    settings: Settings = context.obj
    starting_progress = (0, 0)
    try:
        with _database(settings) as database:
            starting_progress = _bootstrap_progress_totals(database, source.value)
            with _pipeline(database, settings) as pipeline:
                stats = pipeline.bootstrap(source.value, resume=resume, page_limit=page_limit)
    except KeyboardInterrupt as error:
        with _database(settings) as database:
            current_progress = _bootstrap_progress_totals(database, source.value)
        discovered = max(0, current_progress[0] - starting_progress[0])
        queued = max(0, current_progress[1] - starting_progress[1])
        typer.echo("Interrupted; completed discovery pages remain resumable.")
        typer.echo(f"Discovered: {discovered}")
        typer.echo(f"Queued: {queued}")
        raise typer.Exit(130) from error
    typer.echo(f"Discovered: {stats.discovered}")
    typer.echo(f"Queued: {stats.queued}")


@app.command("collect")
@_command_errors
def collect_command(
    context: typer.Context,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    appid: Annotated[int | None, typer.Option("--appid", min=1)] = None,
) -> None:
    """Collect one app or consume resumable queued work."""
    settings: Settings = context.obj
    completed = 0
    failed = 0
    active_lease_token: str | None = None
    active_item_key: str | None = None
    direct_claimed = False
    direct_status_before: str | None = None
    starting_job_totals = (0, 0)
    try:
        with _database(settings) as database:
            starting_job_totals = _collection_job_totals(database)
            if appid is not None:
                item_key = str(appid)
                direct_status_before = database.job_status("collect", item_key)
                active_lease_token = secrets.token_urlsafe(32)
                active_item_key = item_key
                lease = database.requeue_and_claim_specific_job(
                    "collect", item_key, lease_token=active_lease_token
                )
                if lease is None:
                    active_lease_token = None
                    active_item_key = None
                    raise ValueError(f"Collect job for appid {appid} is already running")
                direct_claimed = True
                with _pipeline(database, settings) as pipeline:
                    try:
                        result = pipeline.collect_one(appid, lease_token=active_lease_token)
                    except _EXPECTED_ERRORS as error:
                        database.fail_job(
                            "collect",
                            item_key,
                            error,
                            expected_lease_token=active_lease_token,
                            next_attempt_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                            max_attempts=settings.max_attempts,
                        )
                        active_lease_token = None
                        active_item_key = None
                        raise
                active_lease_token = None
                active_item_key = None
                typer.echo(f"{result.status.replace('_', '-')}: 1")
                return

            with _pipeline(database, settings) as pipeline:
                while limit is None or completed + failed < limit:
                    active_lease_token = secrets.token_urlsafe(32)
                    lease = database.claim_job("collect", lease_token=active_lease_token)
                    if lease is None:
                        active_lease_token = None
                        break
                    item_key = lease["item_key"]
                    active_item_key = item_key
                    try:
                        pipeline.collect_one(int(item_key), lease_token=active_lease_token)
                    except _EXPECTED_ERRORS as error:
                        database.fail_job(
                            "collect",
                            item_key,
                            error,
                            expected_lease_token=active_lease_token,
                            next_attempt_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                            max_attempts=settings.max_attempts,
                        )
                        failed += 1
                        active_lease_token = None
                        active_item_key = None
                        typer.echo(f"Failed appid {item_key}: {error}")
                    else:
                        completed += 1
                        active_lease_token = None
                        active_item_key = None
    except KeyboardInterrupt as error:
        active_completed = 0
        active_failed = 0
        with _database(settings) as database:
            if active_lease_token is not None:
                released = database.release_job_by_lease("collect", active_lease_token)
                if not released and active_item_key is not None:
                    active_status = database.job_status("collect", active_item_key)
                    active_completed = int(active_status == "complete")
                    active_failed = int(active_status in {"failed", "permanent_failure"})
            durable_completed, durable_failed = _durable_collection_deltas(
                database,
                starting_job_totals,
                direct_status_before=direct_status_before,
                direct_claimed=direct_claimed,
            )
        completed = max(completed + active_completed, durable_completed)
        failed = max(failed + active_failed, durable_failed)
        typer.echo(f"Interrupted. Completed: {completed}; Failed: {failed}")
        raise typer.Exit(130) from error

    typer.echo(f"Completed: {completed}")
    typer.echo(f"Failed: {failed}")
    if failed:
        raise typer.Exit(1)


def _bootstrap_progress_totals(database: Database, source: str) -> tuple[int, int]:
    discovered = database.connection.execute(
        "SELECT COUNT(*) FROM source_items WHERE source = ?", (source,)
    ).fetchone()[0]
    queued = database.connection.execute(
        """
        SELECT COUNT(*)
        FROM sync_jobs
        WHERE job_kind = 'collect' AND status IN ('pending', 'failed')
        """
    ).fetchone()[0]
    return discovered, queued


def _collection_job_totals(database: Database) -> tuple[int, int]:
    row = database.connection.execute(
        """
        SELECT
            COALESCE(SUM(status = 'complete'), 0) AS completed,
            COALESCE(SUM(status IN ('failed', 'permanent_failure')), 0) AS failed
        FROM sync_jobs
        WHERE job_kind = 'collect'
        """
    ).fetchone()
    return row["completed"], row["failed"]


def _durable_collection_deltas(
    database: Database,
    starting_totals: tuple[int, int],
    *,
    direct_status_before: str | None = None,
    direct_claimed: bool = False,
) -> tuple[int, int]:
    ending_totals = _collection_job_totals(database)
    completed = ending_totals[0] - starting_totals[0]
    failed = ending_totals[1] - starting_totals[1]
    if direct_claimed:
        completed += int(direct_status_before == "complete")
        failed += int(direct_status_before in {"failed", "permanent_failure"})
    return max(0, completed), max(0, failed)


@app.command("refresh")
@_command_errors
def refresh_command(
    context: typer.Context,
    older_than_days: Annotated[int, typer.Option("--older-than-days", min=1)] = 30,
    failed: Annotated[bool, typer.Option("--failed")] = False,
) -> None:
    """Queue stale catalog records for collection again."""
    settings: Settings = context.obj
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=older_than_days)
    with _database(settings) as database:
        scheduled = database.schedule_refresh(
            cutoff.isoformat(), include_failed=failed, updated_at=now.isoformat()
        )
    typer.echo(f"Scheduled: {scheduled}")


@app.command("import-library")
@_command_errors
def import_library_command(context: typer.Context, path: Path) -> None:
    """Import a Steam library JSON file."""
    settings: Settings = context.obj
    with _database(settings) as database:
        stats = import_library(database, path)
    typer.echo(f"Imported: {stats.imported}")
    typer.echo(f"Placeholders created: {stats.placeholders_created}")


@app.command("status")
@_command_errors
def status_command(context: typer.Context) -> None:
    """Show catalog eligibility, completeness, and queue counts."""
    settings: Settings = context.obj
    with _database(settings) as database:
        counts = database.status_counts()
    for label, count in counts.items():
        typer.echo(f"{label}: {count}")


@app.command("export")
@_command_errors
def export_command(
    context: typer.Context,
    output: Path,
    format: Annotated[ExportFormat, typer.Option("--format")] = ExportFormat.JSONL,
    include_reviews: Annotated[bool, typer.Option("--include-reviews")] = False,
) -> None:
    """Export eligible games to JSON Lines or CSV."""
    settings: Settings = context.obj
    if format is ExportFormat.CSV and include_reviews:
        raise ValueError("--include-reviews is available only for JSONL exports")
    with _database(settings) as database:
        if format is ExportFormat.CSV:
            count = export_csv(database, output)
        else:
            count = export_jsonl(database, output, include_reviews=include_reviews)
    typer.echo(f"Exported: {count}")


def run() -> Any:
    """Invoke the Typer application."""
    return app()
