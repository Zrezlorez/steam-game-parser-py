from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import steam_catalog.cli as cli_module
from steam_catalog.cli import app
from steam_catalog.db import Database
from steam_catalog.models import DiscoveryItem
from steam_catalog.pipeline import CatalogPipeline, CollectionResult, RunStats


runner = CliRunner()


@pytest.fixture
def initialized_database_path(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite3"
    database = Database(path)
    database.initialize()
    database.enqueue_job("collect", "10", updated_at="2026-07-20T00:00:00+00:00")
    database.record_game_status(20, "eligible", updated_at="2026-07-20T00:00:00+00:00")
    database.close()
    return path


def _open_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def test_init_creates_database(tmp_path: Path):
    database_path = tmp_path / "catalog.sqlite3"

    result = runner.invoke(app, ["--database", str(database_path), "init"])

    assert result.exit_code == 0
    assert database_path.exists()
    assert "initialized" in result.stdout.lower()


def test_help_lists_exact_command_surface():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "init",
        "bootstrap",
        "collect",
        "refresh",
        "import-library",
        "status",
        "export",
        "run",
    ):
        assert command in result.stdout


def test_collect_help_does_not_advertise_unsupported_resume_switches():
    result = runner.invoke(app, ["collect", "--help"])

    assert result.exit_code == 0
    assert "--resume" not in result.stdout
    assert "--no-resume" not in result.stdout


def test_collect_rejects_removed_no_resume_option(tmp_path: Path):
    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), "collect", "--no-resume"],
    )

    assert result.exit_code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["collect", "--limit", "0"],
        ["collect", "--appid", "0"],
        ["bootstrap", "--page-limit", "0"],
        ["refresh", "--older-than-days", "0"],
    ],
)
def test_positive_options_reject_zero(tmp_path: Path, arguments: list[str]):
    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), *arguments],
    )

    assert result.exit_code == 2


def test_status_reports_every_required_category(tmp_path: Path):
    path = tmp_path / "status.sqlite3"
    database = _open_database(path)
    statuses = (
        "eligible",
        "below_provisional_threshold",
        "below_verified_threshold",
        "ineligible",
    )
    for appid, status in enumerate(statuses, start=1):
        database.record_game_status(appid, status, updated_at="2026-07-20T00:00:00+00:00")
    database.connection.execute(
        "UPDATE games SET metadata_fetched_at = ? WHERE appid = 1",
        ("2026-07-20T00:00:00+00:00",),
    )
    database.connection.execute(
        "UPDATE games SET reviews_fetched_at = ? WHERE appid = 1",
        ("2026-07-20T00:00:00+00:00",),
    )
    for offset, status in enumerate(("pending", "running", "failed", "permanent_failure")):
        database.enqueue_job("collect", str(100 + offset))
        database.connection.execute(
            "UPDATE sync_jobs SET status = ? WHERE item_key = ?",
            (status, str(100 + offset)),
        )
    database.close()

    result = runner.invoke(app, ["--database", str(path), "status"])

    assert result.exit_code == 0
    for label in (
        "eligible",
        "provisional-below-threshold",
        "verified-below-threshold",
        "skipped",
        "metadata-complete",
        "review-complete",
        "pending",
        "running",
        "retrying",
        "permanent-failure",
    ):
        assert f"{label}: 1" in result.stdout.lower()


def test_status_initializes_missing_database(tmp_path: Path):
    path = tmp_path / "new.sqlite3"

    result = runner.invoke(app, ["--database", str(path), "status"])

    assert result.exit_code == 0
    assert "eligible: 0" in result.stdout.lower()


def test_refresh_schedules_only_old_rows_and_optionally_permanent_failures(tmp_path: Path):
    path = tmp_path / "refresh.sqlite3"
    database = _open_database(path)
    for appid, status, fetched_at in (
        (10, "eligible", "2000-01-01T00:00:00+00:00"),
        (11, "eligible", "2999-01-01T00:00:00+00:00"),
        (12, "ineligible", "2999-01-01T00:00:00+00:00"),
    ):
        database.record_game_status(
            appid,
            status,
            reviews_fetched_at=fetched_at,
            replace_steam_aggregate=True,
            updated_at=fetched_at,
        )
    database.enqueue_job("collect", "12")
    database.connection.execute(
        "UPDATE sync_jobs SET status = 'permanent_failure' WHERE item_key = '12'"
    )
    database.close()

    result = runner.invoke(
        app,
        ["--database", str(path), "refresh", "--older-than-days", "30"],
    )

    assert result.exit_code == 0
    database = _open_database(path)
    assert database.job_status("collect", "10") == "pending"
    assert database.job_status("collect", "11") is None
    assert database.job_status("collect", "12") == "permanent_failure"
    database.close()

    failed_result = runner.invoke(
        app,
        ["--database", str(path), "refresh", "--older-than-days", "30", "--failed"],
    )

    assert failed_result.exit_code == 0
    database = _open_database(path)
    assert database.job_status("collect", "12") == "pending"
    database.close()


def test_refresh_reason_schedules_exact_matches_without_age_cutoff(tmp_path: Path):
    path = tmp_path / "refresh-reason.sqlite3"
    database = _open_database(path)
    for appid, reason in ((10, "unavailable"), (11, "unreleased"), (12, "unavailable")):
        database.record_game_status(
            appid,
            "ineligible",
            skip_reason=reason,
            reviews_fetched_at="2999-01-01T00:00:00+00:00",
            replace_steam_aggregate=True,
            updated_at="2999-01-01T00:00:00+00:00",
        )
    database.enqueue_job("collect", "12")
    database.connection.execute(
        "UPDATE sync_jobs SET status = 'running', lease_token = 'owned' WHERE item_key = '12'"
    )
    database.close()

    result = runner.invoke(
        app,
        ["--database", str(path), "refresh", "--reason", "unavailable"],
    )

    assert result.exit_code == 0
    assert "scheduled: 1" in result.stdout.lower()
    database = _open_database(path)
    assert database.job_status("collect", "10") == "pending"
    assert database.job_status("collect", "11") is None
    assert database.job_status("collect", "12") == "running"
    database.close()


def test_refresh_reason_unknown_and_empty_values_are_safe(tmp_path: Path):
    path = tmp_path / "refresh-reason-validation.sqlite3"
    database = _open_database(path)
    database.record_game_status(
        10,
        "ineligible",
        skip_reason="unavailable",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    database.close()

    unknown = runner.invoke(
        app,
        ["--database", str(path), "refresh", "--reason", "typo"],
    )
    empty = runner.invoke(
        app,
        ["--database", str(path), "refresh", "--reason", "   "],
    )

    assert unknown.exit_code == 0
    assert "scheduled: 0" in unknown.stdout.lower()
    assert empty.exit_code == 1
    assert "reason must not be empty" in empty.stdout.lower()


def test_import_library_reports_concise_data_error(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "catalog.sqlite3"),
            "import-library",
            str(path),
        ],
    )

    assert result.exit_code == 1
    assert "invalid library json" in result.stdout.lower()
    assert "traceback" not in result.stdout.lower()


def test_invalid_environment_setting_reports_concise_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STEAM_CATALOG_MAX_ATTEMPTS", "0")

    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), "status"],
    )

    assert result.exit_code == 1
    assert "error: max_attempts must be positive" in result.stdout.lower()
    assert "traceback" not in result.stdout.lower()


def test_import_library_and_export_are_wired_offline(tmp_path: Path):
    library = tmp_path / "library.json"
    library.write_text('[{"appid":10,"name":"Owned","playtime_forever":5}]', encoding="utf-8")
    database_path = tmp_path / "catalog.sqlite3"

    imported = runner.invoke(
        app,
        ["--database", str(database_path), "import-library", str(library)],
    )
    output = tmp_path / "catalog.jsonl"
    exported = runner.invoke(
        app,
        ["--database", str(database_path), "export", str(output), "--format", "jsonl"],
    )

    assert imported.exit_code == 0
    assert "imported: 1" in imported.stdout.lower()
    assert exported.exit_code == 0
    assert "exported: 0" in exported.stdout.lower()
    assert output.read_text(encoding="utf-8") == ""


def test_bootstrap_delegates_without_printing_payloads(tmp_path: Path, monkeypatch):
    class FakePipeline:
        def bootstrap(self, source: str, *, resume: bool, page_limit: int | None, on_progress=None):
            assert (source, resume, page_limit) == ("steamspy", True, 2)
            return RunStats(discovered=3, queued=2)

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield FakePipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "catalog.sqlite3"),
            "bootstrap",
            "--source",
            "steamspy",
            "--resume",
            "--page-limit",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "discovered: 3" in result.stdout.lower()
    assert "queued: 2" in result.stdout.lower()
    assert "payload" not in result.stdout.lower()


def test_run_orchestrates_all_phases_and_forwards_options(tmp_path: Path, monkeypatch):
    library = tmp_path / "library.json"
    library.write_text('[{"appid":10,"name":"Owned"}]', encoding="utf-8")
    observed: list[object] = []

    @contextmanager
    def fake_pipeline(database, settings):
        class FakePipeline:
            bootstrap_progress = RunStats()

            def bootstrap(
                self, source: str, *, resume: bool, page_limit: int | None, on_progress=None
            ):
                observed.append((source, resume, page_limit))
                return RunStats(discovered=1, queued=0)

        yield FakePipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "catalog.sqlite3"),
            "run",
            "--library",
            str(library),
            "--source",
            "steam",
            "--no-resume",
            "--page-limit",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert observed == [("steam", False, 2)]
    assert result.stdout.index("Initialize") < result.stdout.index("Import library")
    assert result.stdout.index("Import library") < result.stdout.index("Discover")
    assert result.stdout.index("Discover") < result.stdout.index("Collect")
    assert result.stdout.index("Collect") < result.stdout.index("Summary")


def test_run_without_library_omits_import_phase(tmp_path: Path, monkeypatch):
    @contextmanager
    def fake_pipeline(database, settings):
        class FakePipeline:
            def bootstrap(
                self, source: str, *, resume: bool, page_limit: int | None, on_progress=None
            ):
                return RunStats()

        yield FakePipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), "run", "--page-limit", "1"],
    )

    assert result.exit_code == 0
    assert "Import library" not in result.stdout


def test_run_prints_discovery_page_progress(tmp_path: Path, monkeypatch):
    @contextmanager
    def fake_pipeline(database, settings):
        class FakePipeline:
            def bootstrap(
                self,
                source: str,
                *,
                resume: bool,
                page_limit: int | None,
                on_progress,
            ):
                on_progress(SimpleNamespace(stage="fetching", source_page=4))
                on_progress(
                    SimpleNamespace(
                        stage="saved",
                        source_page=4,
                        discovered=1000,
                        queued=734,
                        total_discovered=5000,
                        total_queued=4465,
                        elapsed_seconds=1.25,
                    )
                )
                return RunStats(discovered=1000, queued=734)

        yield FakePipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), "run", "--page-limit", "1"],
    )

    assert result.exit_code == 0
    assert "Discovery page 4: fetching..." in result.stdout
    assert (
        "Discovery page 4: saved, discovered=1000, queued=734, "
        "total_discovered=5000, total_queued=4465, 1.2s" in result.stdout
    )


def test_run_interrupt_releases_active_collection_job(tmp_path: Path, monkeypatch):
    path = tmp_path / "run-interrupt.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.close()

    @contextmanager
    def fake_pipeline(database, settings):
        class InterruptingPipeline:
            bootstrap_progress = RunStats()

            def bootstrap(
                self, source: str, *, resume: bool, page_limit: int | None, on_progress=None
            ):
                return RunStats()

            def collect_one(self, appid: int, *, lease_token: str | None = None):
                raise KeyboardInterrupt

        yield InterruptingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(app, ["--database", str(path), "run", "--page-limit", "1"])

    assert result.exit_code == 130
    assert "Summary" in result.stdout
    assert "Interrupted. Completed: 0; Failed: 0" in result.stdout
    database = _open_database(path)
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("pending", 0, None)
    database.close()


def test_bootstrap_keyboard_interrupt_reports_progress_and_exits_130(tmp_path: Path, monkeypatch):
    class InterruptingPipeline:
        def bootstrap(self, source: str, *, resume: bool, page_limit: int | None, on_progress=None):
            raise KeyboardInterrupt

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield InterruptingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), "bootstrap"],
    )

    assert result.exit_code == 130
    assert "interrupted" in result.stdout.lower()
    assert "discovered: 0" in result.stdout.lower()
    assert "queued: 0" in result.stdout.lower()


def test_bootstrap_interrupt_reports_current_run_deltas_not_historical_totals(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "bootstrap-history.sqlite3"
    database = _open_database(path)
    database.upsert_source_item(
        "steamspy",
        DiscoveryItem(appid=1, name="Historical", payload_json='{"appid":1}'),
    )
    for item_key, status in (("1", "complete"), ("2", "permanent_failure"), ("3", "pending")):
        database.enqueue_job("collect", item_key)
        database.connection.execute(
            "UPDATE sync_jobs SET status = ? WHERE item_key = ?", (status, item_key)
        )
    database.close()

    @contextmanager
    def fake_pipeline(database, settings):
        class InterruptingPipeline:
            bootstrap_progress = RunStats()

            def bootstrap(
                self, source: str, *, resume: bool, page_limit: int | None, on_progress=None
            ):
                database.upsert_source_item(
                    source,
                    DiscoveryItem(appid=4, name="Current", payload_json='{"appid":4}'),
                )
                database.enqueue_job("collect", "4")
                self.bootstrap_progress = RunStats(discovered=1, queued=1)
                raise KeyboardInterrupt

        yield InterruptingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(app, ["--database", str(path), "bootstrap"])

    assert result.exit_code == 130
    assert "discovered: 1" in result.stdout.lower()
    assert "queued: 1" in result.stdout.lower()


def test_bootstrap_interrupt_counts_forced_rediscovery_of_existing_item(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "bootstrap-rediscovery.sqlite3"
    candidate = DiscoveryItem(
        appid=10,
        name="Rediscovered",
        steamspy_positive=20,
        steamspy_negative=0,
        provisional_reviews=20,
        payload_json='{"appid":10}',
    )
    database = _open_database(path)
    database.upsert_source_item("steamspy", candidate, source_page=0)
    database.close()

    class StaticSteamSpy:
        def fetch_page(self, page: int):
            assert page == 0
            return [candidate]

    @contextmanager
    def interrupting_pipeline(database, settings):
        pipeline = CatalogPipeline(database, steam=object(), steamspy=StaticSteamSpy())
        yield pipeline
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", interrupting_pipeline)

    result = runner.invoke(
        app,
        [
            "--database",
            str(path),
            "bootstrap",
            "--no-resume",
            "--page-limit",
            "1",
        ],
    )

    assert result.exit_code == 130
    assert "discovered: 1" in result.stdout.lower()
    assert "queued: 1" in result.stdout.lower()


def test_bootstrap_interrupt_does_not_net_cancel_discarded_and_queued_jobs(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "bootstrap-discard-and-queue.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.close()
    candidates = [
        DiscoveryItem(
            appid=10,
            name="Rejected",
            provisional_reviews=19,
            payload_json='{"appid":10}',
        ),
        DiscoveryItem(
            appid=11,
            name="Queued",
            provisional_reviews=20,
            payload_json='{"appid":11}',
        ),
    ]

    class StaticSteamSpy:
        def fetch_page(self, page: int):
            assert page == 0
            return candidates

    @contextmanager
    def interrupting_pipeline(database, settings):
        pipeline = CatalogPipeline(database, steam=object(), steamspy=StaticSteamSpy())
        yield pipeline
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", interrupting_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(path), "bootstrap", "--page-limit", "1"],
    )

    assert result.exit_code == 130
    assert "discovered: 2" in result.stdout.lower()
    assert "queued: 1" in result.stdout.lower()
    database = _open_database(path)
    assert database.job_status("collect", "10") is None
    assert database.job_status("collect", "11") == "pending"
    database.close()


def test_collect_keyboard_interrupt_releases_job_and_exits_130(tmp_path: Path, monkeypatch):
    path = tmp_path / "interrupt.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "8")
    database.connection.execute("UPDATE sync_jobs SET status = 'complete' WHERE item_key = '8'")
    database.enqueue_job("collect", "9")
    database.connection.execute("UPDATE sync_jobs SET status = 'failed' WHERE item_key = '9'")
    database.enqueue_job("collect", "10")
    database.close()

    class InterruptingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            raise KeyboardInterrupt

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield InterruptingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "completed: 0" in result.stdout.lower()
    assert "interrupted" in result.stdout.lower()
    database = _open_database(path)
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("pending", 0, None)
    database.close()


def test_collect_interrupt_after_committed_claim_recovers_active_lease(tmp_path: Path, monkeypatch):
    path = tmp_path / "claim-interrupt.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.close()
    claim_job = Database.claim_job

    def interrupt_after_claim(self, job_kind, *, now=None, lease_token=None):
        claimed = claim_job(self, job_kind, now=now, lease_token=lease_token)
        assert claimed is not None
        raise KeyboardInterrupt

    monkeypatch.setattr(Database, "claim_job", interrupt_after_claim)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "completed: 0" in result.stdout.lower()
    database = _open_database(path)
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("pending", 0, None)
    database.close()


def test_collect_interrupt_after_failure_persistence_reports_durable_counts(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "failure-interrupt.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "9")
    database.connection.execute(
        """
        UPDATE sync_jobs
        SET status = 'failed', next_attempt_at = '2999-01-01T00:00:00Z'
        WHERE item_key = '9'
        """
    )
    database.enqueue_job("collect", "10")
    database.close()
    fail_job = Database.fail_job

    class FailingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            raise ValueError("source failed")

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield FailingPipeline()

    def interrupt_after_failure(self, *args, **kwargs):
        fail_job(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)
    monkeypatch.setattr(Database, "fail_job", interrupt_after_failure)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "failed: 1" in result.stdout.lower()
    database = _open_database(path)
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("failed", 1, None)
    database.close()


def test_collect_interrupt_during_pipeline_cleanup_reports_durable_completion(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "cleanup-interrupt.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "9")
    database.connection.execute("UPDATE sync_jobs SET status = 'complete' WHERE item_key = '9'")
    database.enqueue_job("collect", "10")
    database.close()

    @contextmanager
    def interrupting_pipeline(database, settings):
        class CompletingPipeline:
            def collect_one(self, appid: int, *, lease_token: str | None = None):
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield CompletingPipeline()
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", interrupting_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "completed: 1" in result.stdout.lower()
    database = _open_database(path)
    assert database.job_status("collect", "10") == "complete"
    database.close()


def test_collect_interrupt_after_recorded_completion_counts_appid_once(tmp_path: Path, monkeypatch):
    progress_type = getattr(cli_module, "_CollectionProgress", None)
    assert progress_type is not None
    original_record = progress_type.record
    path = tmp_path / "recorded-completion.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.close()

    @contextmanager
    def completing_pipeline(database, settings):
        class CompletingPipeline:
            def collect_one(self, appid: int, *, lease_token: str | None = None):
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield CompletingPipeline()

    def interrupt_after_record(self, item_key: str, outcome: str):
        original_record(self, item_key, outcome)
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", completing_pipeline)
    monkeypatch.setattr(progress_type, "record", interrupt_after_record)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "completed: 1" in result.stdout.lower()
    assert "failed: 0" in result.stdout.lower()


def test_collect_interrupt_after_recorded_failure_counts_appid_once(tmp_path: Path, monkeypatch):
    progress_type = getattr(cli_module, "_CollectionProgress", None)
    assert progress_type is not None
    original_record = progress_type.record
    path = tmp_path / "recorded-failure.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.close()

    class FailingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            raise ValueError("source failed")

    @contextmanager
    def failing_pipeline(*args, **kwargs):
        yield FailingPipeline()

    def interrupt_after_record(self, item_key: str, outcome: str):
        original_record(self, item_key, outcome)
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", failing_pipeline)
    monkeypatch.setattr(progress_type, "record", interrupt_after_record)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "completed: 0" in result.stdout.lower()
    assert "failed: 1" in result.stdout.lower()


def test_collect_interrupt_ignores_unrelated_collector_terminal_change(tmp_path: Path, monkeypatch):
    path = tmp_path / "unrelated-collector.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10", updated_at="2026-07-20T00:00:00Z")
    database.enqueue_job("collect", "20", updated_at="2026-07-20T00:00:01Z")
    database.close()

    class InterruptingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            assert appid == 10
            other = _open_database(path)
            try:
                unrelated = other.claim_specific_job(
                    "collect", "20", lease_token="unrelated-opaque-lease"
                )
                assert unrelated is not None
                other.complete_job(
                    "collect",
                    "20",
                    expected_lease_token="unrelated-opaque-lease",
                )
            finally:
                other.close()
            raise KeyboardInterrupt

    @contextmanager
    def interrupting_pipeline(*args, **kwargs):
        yield InterruptingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", interrupting_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--limit", "1"],
    )

    assert result.exit_code == 130
    assert "completed: 0" in result.stdout.lower()
    assert "failed: 0" in result.stdout.lower()
    database = _open_database(path)
    assert database.job_status("collect", "10") == "pending"
    assert database.job_status("collect", "20") == "complete"
    database.close()


@pytest.mark.parametrize(
    "initial_status",
    [None, "pending", "failed", "complete", "permanent_failure"],
)
def test_collect_appid_requeues_and_claims_exact_target(
    tmp_path: Path, monkeypatch, initial_status: str | None
):
    path = tmp_path / "target.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    if initial_status is not None:
        database.enqueue_job("collect", "11")
        database.connection.execute(
            """
            UPDATE sync_jobs
            SET status = ?, attempt_count = 2, next_attempt_at = '2999-01-01T00:00:00Z',
                last_error = 'old failure'
            WHERE item_key = '11'
            """,
            (initial_status,),
        )
    database.close()
    observed_lease: list[str] = []

    @contextmanager
    def fake_pipeline(database, settings):
        class CompletingPipeline:
            def collect_one(self, appid: int, *, lease_token: str | None = None):
                assert appid == 11
                assert lease_token is not None
                observed_lease.append(lease_token)
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield CompletingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--appid", "11"],
    )

    assert result.exit_code == 0
    assert len(observed_lease) == 1
    database = _open_database(path)
    assert database.job_status("collect", "10") == "pending"
    assert database.job_status("collect", "11") == "complete"
    assert (
        database.connection.execute(
            "SELECT attempt_count FROM sync_jobs WHERE item_key = '11'"
        ).fetchone()[0]
        == 1
    )
    database.close()


def test_collect_appid_does_not_steal_running_target(tmp_path: Path, monkeypatch):
    path = tmp_path / "running-target.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    lease = database.claim_job("collect", lease_token="existing-lease")
    database.close()

    @contextmanager
    def unused_pipeline(*args, **kwargs):
        raise AssertionError("running target must not be collected")
        yield

    monkeypatch.setattr("steam_catalog.cli._pipeline", unused_pipeline)

    result = runner.invoke(app, ["--database", str(path), "collect", "--appid", "10"])

    assert result.exit_code == 1
    assert "already running" in result.stdout.lower()
    database = _open_database(path)
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '10'"
    ).fetchone()
    assert tuple(row) == ("running", lease["attempt_count"], "existing-lease")
    database.close()


def test_collect_appid_failure_uses_bounded_job_failure_lifecycle(tmp_path: Path, monkeypatch):
    path = tmp_path / "direct-failure.sqlite3"

    class FailingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            assert lease_token is not None
            raise ValueError("source failed")

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield FailingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(app, ["--database", str(path), "collect", "--appid", "4000"])

    assert result.exit_code == 1
    assert "[1/1] Unknown name (appid 4000) - retry," in result.stdout
    assert "source failed" in result.stdout
    database = _open_database(path)
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '4000'"
    ).fetchone()
    assert tuple(row) == ("failed", 1, None)
    database.close()


def test_collect_direct_appid_interrupt_reports_zero_durable_counts(tmp_path: Path, monkeypatch):
    class InterruptingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            assert lease_token is not None
            raise KeyboardInterrupt

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield InterruptingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "direct.sqlite3"), "collect", "--appid", "4000"],
    )

    assert result.exit_code == 130
    assert "completed: 0" in result.stdout.lower()
    assert "failed: 0" in result.stdout.lower()
    database = _open_database(tmp_path / "direct.sqlite3")
    row = database.connection.execute(
        "SELECT status, attempt_count, lease_token FROM sync_jobs WHERE item_key = '4000'"
    ).fetchone()
    assert tuple(row) == ("pending", 0, None)
    database.close()


def test_collect_direct_appid_commit_then_cleanup_interrupt_reports_completion(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "direct-complete.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "4000")
    database.connection.execute("UPDATE sync_jobs SET status = 'complete' WHERE item_key = '4000'")
    database.close()

    @contextmanager
    def interrupting_pipeline(database, settings):
        class CompletingPipeline:
            def collect_one(self, appid: int, *, lease_token: str | None = None):
                assert lease_token is not None
                database.record_game_status(
                    appid,
                    "eligible",
                    updated_at="2026-07-20T00:00:00+00:00",
                )
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield CompletingPipeline()
        raise KeyboardInterrupt

    monkeypatch.setattr("steam_catalog.cli._pipeline", interrupting_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(path), "collect", "--appid", "4000"],
    )

    assert result.exit_code == 130
    assert "completed: 1" in result.stdout.lower()
    assert "failed: 0" in result.stdout.lower()
    database = _open_database(path)
    assert database.game_status(4000) == "eligible"
    assert database.job_status("collect", "4000") == "complete"
    database.close()


def test_collect_appid_delegates_without_review_text(tmp_path: Path, monkeypatch):
    @contextmanager
    def fake_pipeline(database, settings):
        class FakePipeline:
            def collect_one(self, appid: int, *, lease_token: str | None = None):
                assert appid == 4000
                assert lease_token is not None
                database.upsert_game(
                    appid=appid,
                    name="Garry's Mod",
                    eligibility_status="eligible",
                    updated_at="2026-07-20T00:00:00+00:00",
                )
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield FakePipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "catalog.sqlite3"), "collect", "--appid", "4000"],
    )

    assert result.exit_code == 0
    assert "eligible: 1" in result.stdout.lower()
    assert "[1/1] Garry's Mod (appid 4000) - saved, reviews=0, samples=0," in result.stdout
    assert "review text" not in result.stdout.lower()


def test_collect_prints_one_post_commit_line_per_game(tmp_path: Path, monkeypatch):
    path = tmp_path / "progress.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.enqueue_job("collect", "20")
    database.close()

    @contextmanager
    def fake_pipeline(database, settings):
        class CompletingPipeline:
            def collect_one(self, appid: int, *, lease_token: str | None = None):
                database.upsert_game(
                    appid=appid,
                    name=f"Game {appid}",
                    eligibility_status="eligible",
                    updated_at="2026-07-20T00:00:00+00:00",
                )
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield CompletingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)

    result = runner.invoke(app, ["--database", str(path), "collect"])

    assert result.exit_code == 0
    assert "[1/2] Game 10 (appid 10) - saved, reviews=0, samples=0," in result.stdout
    assert "[2/2] Game 20 (appid 20) - saved, reviews=0, samples=0," in result.stdout


def test_collect_prints_retry_only_after_failure_is_durable(tmp_path: Path, monkeypatch):
    path = tmp_path / "retry-progress.sqlite3"
    database = _open_database(path)
    database.enqueue_job("collect", "10")
    database.close()
    durable_statuses: list[str | None] = []
    print_event = cli_module._print_collection_event

    class FailingPipeline:
        def collect_one(self, appid: int, *, lease_token: str | None = None):
            raise ValueError("source failed")

    @contextmanager
    def fake_pipeline(*args, **kwargs):
        yield FailingPipeline()

    def inspect_event(event):
        current = _open_database(path)
        durable_statuses.append(current.job_status("collect", str(event.appid)))
        current.close()
        print_event(event)

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)
    monkeypatch.setattr("steam_catalog.cli._print_collection_event", inspect_event)

    result = runner.invoke(app, ["--database", str(path), "collect"])

    assert result.exit_code == 1
    assert durable_statuses == ["failed"]
    assert "[1/1] Unknown name (appid 10) - retry," in result.stdout
    assert "source failed" in result.stdout
