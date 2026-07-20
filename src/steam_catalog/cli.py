"""Typer command-line interface for the Steam catalog collector."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import wraps
from pathlib import Path
import secrets
import sqlite3
from time import monotonic
from typing import Annotated, Any, Literal, ParamSpec, TypeAlias, TypeVar

import httpx
import typer

from steam_catalog.config import Settings
from steam_catalog.db import Database
from steam_catalog.exporters import export_csv, export_jsonl
from steam_catalog.http import CachedHttpClient
from steam_catalog.importers.library import import_library
from steam_catalog.pipeline import BootstrapProgressEvent, CatalogPipeline, RunStats
from steam_catalog.sources.steam import SteamClient
from steam_catalog.sources.steamspy import SteamSpyClient


class DiscoverySource(str, Enum):
    STEAMSPY = "steamspy"
    STEAM = "steam"


class ExportFormat(str, Enum):
    JSONL = "jsonl"
    CSV = "csv"


CollectionOutcome: TypeAlias = Literal["saved", "skipped", "retry", "failed"]


@dataclass(frozen=True)
class _CollectionEvent:
    sequence: int
    total: int
    appid: int
    name: str | None
    outcome: CollectionOutcome
    total_reviews: int
    sample_count: int
    elapsed_seconds: float
    error: str | None = None


@dataclass(frozen=True)
class _PriceEvent:
    sequence: int
    total: int
    appid: int
    name: str | None
    outcome: Literal["saved", "retry", "failed"]
    elapsed_seconds: float
    error: str | None = None


@dataclass
class _CollectionProgress:
    outcomes: dict[str, str] = field(default_factory=dict)

    def record(self, item_key: str, outcome: CollectionOutcome) -> None:
        if outcome not in {"saved", "skipped", "retry", "failed"}:
            raise ValueError(f"Unsupported collection outcome: {outcome}")
        self.outcomes[item_key] = outcome

    @property
    def completed(self) -> int:
        return self.saved + self.skipped

    @property
    def failed(self) -> int:
        return self.retrying + self.permanent_failures

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def saved(self) -> int:
        return sum(outcome == "saved" for outcome in self.outcomes.values())

    @property
    def skipped(self) -> int:
        return sum(outcome == "skipped" for outcome in self.outcomes.values())

    @property
    def retrying(self) -> int:
        return sum(outcome == "retry" for outcome in self.outcomes.values())

    @property
    def permanent_failures(self) -> int:
        return sum(outcome == "failed" for outcome in self.outcomes.values())


def _print_collection_event(event: _CollectionEvent) -> None:
    name = event.name or "Unknown name"
    detail = (
        f", reviews={event.total_reviews}, samples={event.sample_count}"
        if event.outcome == "saved"
        else ""
    )
    error = f": {event.error}" if event.error else ""
    typer.echo(
        f"[{event.sequence}/{event.total}] {name} (appid {event.appid}) - "
        f"{event.outcome}{detail}, {event.elapsed_seconds:.1f}s{error}"
    )


def _print_collection_summary(
    progress: _CollectionProgress, elapsed_seconds: float, *, interrupted: bool = False
) -> None:
    prefix = "Interrupted. " if interrupted else ""
    typer.echo(f"{prefix}Completed: {progress.completed}; Failed: {progress.failed}")
    typer.echo(
        f"Saved: {progress.saved}; Skipped: {progress.skipped}; "
        f"Retrying: {progress.retrying}; Permanent failures: {progress.permanent_failures}; "
        f"Elapsed: {elapsed_seconds:.1f}s"
    )


def _print_price_event(event: _PriceEvent) -> None:
    name = event.name or "Unknown name"
    label = "prices saved" if event.outcome == "saved" else event.outcome
    error = f": {event.error}" if event.error else ""
    typer.echo(
        f"[{event.sequence}/{event.total}] {name} (appid {event.appid}) - "
        f"{label}, {event.elapsed_seconds:.1f}s{error}"
    )


def _print_price_summary(
    progress: _CollectionProgress, elapsed_seconds: float, *, interrupted: bool = False
) -> None:
    prefix = "Interrupted. " if interrupted else ""
    typer.echo(
        f"{prefix}Saved: {progress.saved}; Retrying: {progress.retrying}; "
        f"Permanent failures: {progress.permanent_failures}; Elapsed: {elapsed_seconds:.1f}s"
    )


def _print_bootstrap_event(event: BootstrapProgressEvent) -> None:
    page = "Steam app list" if event.source_page is None else f"Discovery page {event.source_page}"
    if event.stage == "fetching":
        typer.echo(f"{page}: fetching...")
        return
    typer.echo(
        f"{page}: saved, discovered={event.discovered}, queued={event.queued}, "
        f"total_discovered={event.total_discovered}, total_queued={event.total_queued}, "
        f"{event.elapsed_seconds:.1f}s"
    )


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
    pipeline: CatalogPipeline | None = None
    stats = RunStats()
    try:
        with _database(settings) as database:
            with _pipeline(database, settings) as active_pipeline:
                pipeline = active_pipeline
                stats = active_pipeline.bootstrap(
                    source.value,
                    resume=resume,
                    page_limit=page_limit,
                    on_progress=_print_bootstrap_event,
                )
    except KeyboardInterrupt as error:
        progress = getattr(pipeline, "bootstrap_progress", stats)
        typer.echo("Interrupted; completed discovery pages remain resumable.")
        typer.echo(f"Discovered: {progress.discovered}")
        typer.echo(f"Queued: {progress.queued}")
        raise typer.Exit(130) from error
    typer.echo(f"Discovered: {stats.discovered}")
    typer.echo(f"Queued: {stats.queued}")


@app.command("run")
@_command_errors
def run_command(
    context: typer.Context,
    library: Annotated[
        Path | None, typer.Option("--library", help="Steam library JSON path.", dir_okay=False)
    ] = None,
    source: Annotated[DiscoverySource, typer.Option("--source")] = DiscoverySource.STEAMSPY,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    page_limit: Annotated[int | None, typer.Option("--page-limit", min=1)] = None,
) -> None:
    """Initialize, optionally import, discover, and collect until idle."""
    settings: Settings = context.obj
    collection_progress = _CollectionProgress()
    bootstrap_stats = RunStats()
    active_pipeline: CatalogPipeline | None = None
    phase = "initialize"
    started = monotonic()
    try:
        with _database(settings) as database:
            typer.echo(f"Initialize: {database.path}")
            if library is not None:
                phase = "import"
                typer.echo(f"Import library: {library}")
                import_stats = import_library(database, library)
                typer.echo(
                    f"Imported: {import_stats.imported}; "
                    f"Placeholders created: {import_stats.placeholders_created}"
                )

            phase = "discover"
            typer.echo(f"Discover: {source.value}")
            with _pipeline(database, settings) as pipeline:
                active_pipeline = pipeline
                bootstrap_stats = pipeline.bootstrap(
                    source.value,
                    resume=resume,
                    page_limit=page_limit,
                    on_progress=_print_bootstrap_event,
                )
                typer.echo(f"Discovered: {bootstrap_stats.discovered}")
                typer.echo(f"Queued: {bootstrap_stats.queued}")

                phase = "collect"
                typer.echo("Collect")
                _consume_collection_jobs(
                    database,
                    pipeline,
                    settings,
                    limit=None,
                    on_event=_print_collection_event,
                    progress=collection_progress,
                )
    except KeyboardInterrupt as error:
        if phase == "discover":
            durable = getattr(active_pipeline, "bootstrap_progress", bootstrap_stats)
            typer.echo("Interrupted; completed discovery pages remain resumable.")
            typer.echo(f"Discovered: {durable.discovered}")
            typer.echo(f"Queued: {durable.queued}")
        elif phase == "collect":
            typer.echo("Summary")
            _print_collection_summary(collection_progress, monotonic() - started, interrupted=True)
        else:
            typer.echo(f"Interrupted during {phase}.")
        raise typer.Exit(130) from error

    typer.echo("Summary")
    _print_collection_summary(collection_progress, monotonic() - started)
    if collection_progress.failed:
        raise typer.Exit(1)


def _consume_collection_jobs(
    database: Database,
    pipeline: CatalogPipeline,
    settings: Settings,
    *,
    limit: int | None,
    on_event: Callable[[_CollectionEvent], None],
    progress: _CollectionProgress,
) -> None:
    """Consume collection work while publishing only durable outcomes."""
    active_lease_token: str | None = None
    active_item_key: str | None = None
    try:
        display_total = database.collection_queue_size()
        if limit is not None:
            display_total = min(display_total, limit)
        while limit is None or progress.total < limit:
            active_lease_token = secrets.token_urlsafe(32)
            lease = database.claim_job("collect", lease_token=active_lease_token)
            if lease is None:
                active_lease_token = None
                break
            item_key = lease["item_key"]
            active_item_key = item_key
            try:
                _collect_claimed_job(
                    database,
                    pipeline,
                    settings,
                    item_key=item_key,
                    lease_token=active_lease_token,
                    sequence=progress.total + 1,
                    display_total=display_total,
                    on_event=on_event,
                    progress=progress,
                )
            except _EXPECTED_ERRORS:
                pass
            active_lease_token = None
            active_item_key = None
    except KeyboardInterrupt:
        _reconcile_active_job(database, active_lease_token, active_item_key, progress)
        raise


def _collect_specific_app(
    database: Database,
    settings: Settings,
    appid: int,
    *,
    on_event: Callable[[_CollectionEvent], None],
    progress: _CollectionProgress,
) -> None:
    item_key = str(appid)
    lease_token = secrets.token_urlsafe(32)
    lease = database.requeue_and_claim_specific_job("collect", item_key, lease_token=lease_token)
    if lease is None:
        raise ValueError(f"Collect job for appid {appid} is already running")
    try:
        with _pipeline(database, settings) as pipeline:
            _collect_claimed_job(
                database,
                pipeline,
                settings,
                item_key=item_key,
                lease_token=lease_token,
                sequence=1,
                display_total=1,
                on_event=on_event,
                progress=progress,
            )
    except KeyboardInterrupt:
        _reconcile_active_job(database, lease_token, item_key, progress)
        raise


def _reconcile_active_job(
    database: Database,
    lease_token: str | None,
    item_key: str | None,
    progress: _CollectionProgress,
) -> None:
    if lease_token is None:
        return
    released = database.release_job_by_lease("collect", lease_token)
    if released or item_key is None or item_key in progress.outcomes:
        return
    active_status = database.job_status("collect", item_key)
    if active_status == "complete":
        game_status = database.game_status(int(item_key))
        progress.record(item_key, "saved" if game_status == "eligible" else "skipped")
    elif active_status in {"failed", "permanent_failure"}:
        progress.record(
            item_key,
            "failed" if active_status == "permanent_failure" else "retry",
        )


def _collect_claimed_job(
    database: Database,
    pipeline: CatalogPipeline,
    settings: Settings,
    *,
    item_key: str,
    lease_token: str,
    sequence: int,
    display_total: int,
    on_event: Callable[[_CollectionEvent], None],
    progress: _CollectionProgress,
) -> None:
    appid = int(item_key)
    started = monotonic()
    try:
        result = pipeline.collect_one(appid, lease_token=lease_token)
    except _EXPECTED_ERRORS as error:
        database.fail_job(
            "collect",
            item_key,
            error,
            expected_lease_token=lease_token,
            next_attempt_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            max_attempts=settings.max_attempts,
        )
        job_status = database.job_status("collect", item_key)
        outcome: CollectionOutcome = "failed" if job_status == "permanent_failure" else "retry"
        progress.record(item_key, outcome)
        name, total_reviews, sample_count = database.game_progress_details(appid)
        on_event(
            _CollectionEvent(
                sequence=sequence,
                total=display_total,
                appid=appid,
                name=name,
                outcome=outcome,
                total_reviews=total_reviews,
                sample_count=sample_count,
                elapsed_seconds=monotonic() - started,
                error=str(error),
            )
        )
        raise

    outcome = "saved" if result.status == "eligible" else "skipped"
    progress.record(item_key, outcome)
    name, total_reviews, sample_count = database.game_progress_details(appid)
    on_event(
        _CollectionEvent(
            sequence=sequence,
            total=display_total,
            appid=appid,
            name=name,
            outcome=outcome,
            total_reviews=total_reviews,
            sample_count=sample_count,
            elapsed_seconds=monotonic() - started,
        )
    )


def _consume_price_jobs(
    database: Database,
    pipeline: CatalogPipeline,
    settings: Settings,
    *,
    limit: int | None,
    progress: _CollectionProgress,
) -> None:
    """Consume resumable regional-price jobs with durable progress output."""
    active_lease_token: str | None = None
    active_item_key: str | None = None
    try:
        display_total = database.job_queue_size("collect_prices")
        if limit is not None:
            display_total = min(display_total, limit)
        while limit is None or progress.total < limit:
            active_lease_token = secrets.token_urlsafe(32)
            lease = database.claim_job("collect_prices", lease_token=active_lease_token)
            if lease is None:
                active_lease_token = None
                break
            active_item_key = lease["item_key"]
            try:
                _collect_claimed_price_job(
                    database,
                    pipeline,
                    settings,
                    item_key=active_item_key,
                    lease_token=active_lease_token,
                    sequence=progress.total + 1,
                    display_total=display_total,
                    progress=progress,
                )
            except _EXPECTED_ERRORS:
                pass
            active_lease_token = None
            active_item_key = None
    except KeyboardInterrupt:
        _reconcile_active_price_job(database, active_lease_token, active_item_key, progress)
        raise


def _reconcile_active_price_job(
    database: Database,
    lease_token: str | None,
    item_key: str | None,
    progress: _CollectionProgress,
) -> None:
    if lease_token is None:
        return
    released = database.release_job_by_lease("collect_prices", lease_token)
    if released or item_key is None or item_key in progress.outcomes:
        return
    status = database.job_status("collect_prices", item_key)
    if status == "complete":
        progress.record(item_key, "saved")
    elif status in {"failed", "permanent_failure"}:
        progress.record(item_key, "failed" if status == "permanent_failure" else "retry")


def _collect_claimed_price_job(
    database: Database,
    pipeline: CatalogPipeline,
    settings: Settings,
    *,
    item_key: str,
    lease_token: str,
    sequence: int,
    display_total: int,
    progress: _CollectionProgress,
    force_refresh: bool = False,
) -> None:
    appid = int(item_key)
    started = monotonic()
    try:
        pipeline.collect_prices_one(appid, lease_token=lease_token, force_refresh=force_refresh)
    except _EXPECTED_ERRORS as error:
        database.fail_job(
            "collect_prices",
            item_key,
            error,
            expected_lease_token=lease_token,
            next_attempt_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            max_attempts=settings.max_attempts,
        )
        job_status = database.job_status("collect_prices", item_key)
        outcome: Literal["retry", "failed"] = (
            "failed" if job_status == "permanent_failure" else "retry"
        )
        progress.record(item_key, outcome)
        name, _, _ = database.game_progress_details(appid)
        _print_price_event(
            _PriceEvent(
                sequence=sequence,
                total=display_total,
                appid=appid,
                name=name,
                outcome=outcome,
                elapsed_seconds=monotonic() - started,
                error=str(error),
            )
        )
        raise

    progress.record(item_key, "saved")
    name, _, _ = database.game_progress_details(appid)
    _print_price_event(
        _PriceEvent(
            sequence=sequence,
            total=display_total,
            appid=appid,
            name=name,
            outcome="saved",
            elapsed_seconds=monotonic() - started,
        )
    )


def _collect_specific_prices(
    database: Database,
    settings: Settings,
    appid: int,
    progress: _CollectionProgress,
) -> None:
    if database.game_status(appid) != "eligible":
        raise ValueError(f"appid {appid} is not an eligible game")
    item_key = str(appid)
    lease_token = secrets.token_urlsafe(32)
    lease = database.requeue_and_claim_specific_job(
        "collect_prices", item_key, lease_token=lease_token
    )
    if lease is None:
        raise ValueError(f"Price job for appid {appid} is already running")
    try:
        with _pipeline(database, settings) as pipeline:
            _collect_claimed_price_job(
                database,
                pipeline,
                settings,
                item_key=item_key,
                lease_token=lease_token,
                sequence=1,
                display_total=1,
                progress=progress,
                force_refresh=True,
            )
    except KeyboardInterrupt:
        _reconcile_active_price_job(database, lease_token, item_key, progress)
        raise


@app.command("collect")
@_command_errors
def collect_command(
    context: typer.Context,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    appid: Annotated[int | None, typer.Option("--appid", min=1)] = None,
) -> None:
    """Collect one app or consume resumable queued work."""
    settings: Settings = context.obj
    progress = _CollectionProgress()
    started = monotonic()
    direct_status: str | None = None
    try:
        with _database(settings) as database:
            if appid is not None:
                _collect_specific_app(
                    database,
                    settings,
                    appid=appid,
                    on_event=_print_collection_event,
                    progress=progress,
                )
                direct_status = database.game_status(appid)
            else:
                with _pipeline(database, settings) as pipeline:
                    _consume_collection_jobs(
                        database,
                        pipeline,
                        settings,
                        limit=limit,
                        on_event=_print_collection_event,
                        progress=progress,
                    )
    except KeyboardInterrupt as error:
        _print_collection_summary(progress, monotonic() - started, interrupted=True)
        raise typer.Exit(130) from error

    if appid is not None:
        typer.echo(f"{(direct_status or 'unknown').replace('_', '-')}: 1")
        return

    _print_collection_summary(progress, monotonic() - started)
    if progress.failed:
        raise typer.Exit(1)


@app.command("collect-prices")
@_command_errors
def collect_prices_command(
    context: typer.Context,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    appid: Annotated[int | None, typer.Option("--appid", min=1)] = None,
) -> None:
    """Collect USD and UAH prices for eligible games in a separate pass."""
    settings: Settings = context.obj
    progress = _CollectionProgress()
    started = monotonic()
    try:
        with _database(settings) as database:
            if appid is not None:
                _collect_specific_prices(database, settings, appid, progress)
            else:
                scheduled = database.schedule_price_collection()
                typer.echo(f"Price jobs ready: {scheduled}")
                with _pipeline(database, settings) as pipeline:
                    _consume_price_jobs(
                        database,
                        pipeline,
                        settings,
                        limit=limit,
                        progress=progress,
                    )
    except KeyboardInterrupt as error:
        _print_price_summary(progress, monotonic() - started, interrupted=True)
        raise typer.Exit(130) from error

    _print_price_summary(progress, monotonic() - started)
    if progress.failed:
        raise typer.Exit(1)


@app.command("refresh")
@_command_errors
def refresh_command(
    context: typer.Context,
    older_than_days: Annotated[int, typer.Option("--older-than-days", min=1)] = 30,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    failed: Annotated[bool, typer.Option("--failed")] = False,
) -> None:
    """Queue stale catalog records for collection again."""
    settings: Settings = context.obj
    if reason is not None:
        reason = reason.strip()
        if not reason:
            raise ValueError("reason must not be empty")
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=older_than_days)
    with _database(settings) as database:
        scheduled = database.schedule_refresh(
            cutoff.isoformat(),
            reason=reason,
            include_failed=failed,
            updated_at=now.isoformat(),
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
