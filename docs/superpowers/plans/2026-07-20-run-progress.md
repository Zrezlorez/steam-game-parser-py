# Run Command And Durable Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one resumable `steam-catalog run` command and concise console progress emitted after every durable collection outcome.

**Architecture:** Keep orchestration and rendering in the existing Typer CLI, but extract the current queue loop into one private function shared by `collect` and `run`. Add two read-only `Database` methods for the initial queue denominator and post-commit game display data; no logging framework, subprocess, wrapper, schema migration, or new dependency is needed.

**Tech Stack:** Python 3.11+, Typer, SQLite through the existing `Database`, pytest, Ruff.

## Global Constraints

- Keep all seven existing commands and add `run` as the eighth command.
- `run` consumes jobs until none are immediately claimable; it has no collection limit.
- `--library PATH` is optional; `--source` defaults to `steamspy`; `--resume` defaults to true; `--page-limit` limits discovery only.
- Console output only; never print raw source payloads, review text, or tracebacks.
- Emit a game outcome only after the corresponding SQLite commit succeeds.
- `Ctrl+C` preserves completed work, releases unfinished leases, prints durable counts, and exits 130.
- Future-dated retries are left in SQLite; `run` does not wait for their retry time.
- Do not touch untracked `.idea/` files or `data/steam_info.json`.

## File Map

- Modify `src/steam_catalog/db.py`: add read-only queue-size and game-progress queries.
- Modify `src/steam_catalog/cli.py`: define progress events/rendering, share queue consumption, and add `run` orchestration.
- Modify `tests/test_db.py`: verify progress queries independently of CLI behavior.
- Modify `tests/test_cli.py`: verify rendering, command orchestration, durable failures, and interruption behavior.
- Modify `README.md`: replace the multi-command quick start with `run` and retain advanced commands.

---

### Task 1: Read-Only Progress Data

**Files:**
- Modify: `src/steam_catalog/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `Database.collection_queue_size() -> int`
- Produces: `Database.game_progress_details(appid: int) -> tuple[str | None, int]`
- Consumes: existing `games`, `reviews`, and `sync_jobs` tables and `Database.max_attempts`

- [ ] **Step 1: Write failing database tests**

Append tests which create pending, future-retry, permanent-failure, and completed jobs, then assert that only actionable backlog states below the attempt ceiling contribute to the starting denominator. Store one named game with no review samples and assert the name/count tuple; assert `(None, 0)` for an unknown appid.

```python
def test_collection_queue_size_counts_pending_and_retry_backlog(tmp_path: Path):
    database = Database(tmp_path / "progress.sqlite3", max_attempts=3)
    database.initialize()
    for appid in (10, 20, 30, 40):
        database.enqueue_job("collect", str(appid))
    database.connection.execute(
        "UPDATE sync_jobs SET status = 'failed', next_attempt_at = '2999-01-01T00:00:00Z' "
        "WHERE item_key = '20'"
    )
    database.connection.execute(
        "UPDATE sync_jobs SET status = 'permanent_failure' WHERE item_key = '30'"
    )
    database.connection.execute(
        "UPDATE sync_jobs SET status = 'complete' WHERE item_key = '40'"
    )

    assert database.collection_queue_size() == 2
    database.close()


def test_game_progress_details_returns_name_and_stored_review_count(tmp_path: Path):
    database = Database(tmp_path / "details.sqlite3")
    database.initialize()
    database.upsert_game(
        appid=10,
        name="Test Game",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00+00:00",
    )

    assert database.game_progress_details(10) == ("Test Game", 0)
    assert database.game_progress_details(999) == (None, 0)
    database.close()
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_db.py -k "collection_queue_size or game_progress_details" -v`

Expected: FAIL because both `Database` methods are absent.

- [ ] **Step 3: Implement the two read-only methods**

Add these methods beside `job_status`, using parameterized SQL and the connection row factory:

```python
def collection_queue_size(self) -> int:
    """Return collection jobs that remain eligible for a future claim."""
    return self.connection.execute(
        """
        SELECT COUNT(*)
        FROM sync_jobs
        WHERE job_kind = 'collect'
          AND status IN ('pending', 'failed')
          AND attempt_count < ?
        """,
        (self.max_attempts,),
    ).fetchone()[0]

def game_progress_details(self, appid: int) -> tuple[str | None, int]:
    """Return display-safe post-commit details for collection progress."""
    row = self.connection.execute(
        """
        SELECT games.name, COUNT(reviews.recommendation_id) AS review_count
        FROM games
        LEFT JOIN reviews ON reviews.appid = games.appid
        WHERE games.appid = ?
        GROUP BY games.appid, games.name
        """,
        (appid,),
    ).fetchone()
    if row is None:
        return None, 0
    return row["name"], row["review_count"]
```

- [ ] **Step 4: Run database tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_db.py -v`

Expected: all `tests/test_db.py` tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/steam_catalog/db.py tests/test_db.py
git commit -m "feat: expose durable collection progress data"
```

---

### Task 2: Shared Collection Progress And Rendering

**Files:**
- Modify: `src/steam_catalog/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Database.collection_queue_size()` and `Database.game_progress_details(appid)` from Task 1
- Produces: private immutable `_CollectionEvent`
- Produces: `_consume_collection_jobs(database, pipeline, settings, *, limit, appid, on_event, progress) -> None`
- Produces: `_print_collection_event(event: _CollectionEvent) -> None`
- Preserves: `_CollectionProgress.completed`, `.failed`, and `.total` used by interruption tests

- [ ] **Step 1: Write failing output and durability tests**

Add a test with two queued games and a fake pipeline that completes each owned lease and records game names. Invoke `collect` and assert ordered lines contain `[1/2]`, `[2/2]`, both names/appids, `saved`, `reviews=0`, and a seconds suffix. Add a failure test where `collect_one` raises, then assert the line says `retry` only after `Database.fail_job` has persisted status `failed`.

```python
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
                database.record_game_status(
                    appid,
                    "eligible",
                    updated_at="2026-07-20T00:00:00+00:00",
                )
                database.connection.execute(
                    "UPDATE games SET name = ? WHERE appid = ?",
                    (f"Game {appid}", appid),
                )
                database.complete_job("collect", str(appid), expected_lease_token=lease_token)
                return CollectionResult(appid=appid, status="eligible")

        yield CompletingPipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)
    result = runner.invoke(app, ["--database", str(path), "collect"])

    assert result.exit_code == 0
    assert "[1/2] Game 10 (appid 10) - saved, reviews=0" in result.stdout
    assert "[2/2] Game 20 (appid 20) - saved, reviews=0" in result.stdout
```

- [ ] **Step 2: Run focused CLI tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "post_commit_line or durable_retry_line" -v`

Expected: FAIL because per-game progress is not rendered.

- [ ] **Step 3: Add event types and deterministic formatter**

Import `monotonic` from `time` and `Literal` from `typing`, then add:

```python
CollectionOutcome = Literal["saved", "skipped", "retry", "failed"]


@dataclass(frozen=True)
class _CollectionEvent:
    sequence: int
    total: int
    appid: int
    name: str | None
    outcome: CollectionOutcome
    review_count: int
    elapsed_seconds: float
    error: str | None = None


def _print_collection_event(event: _CollectionEvent) -> None:
    name = event.name or "Unknown name"
    detail = f", reviews={event.review_count}" if event.outcome == "saved" else ""
    error = f": {event.error}" if event.error else ""
    typer.echo(
        f"[{event.sequence}/{event.total}] {name} (appid {event.appid}) - "
        f"{event.outcome}{detail}, {event.elapsed_seconds:.1f}s{error}"
    )
```

Extend `_CollectionProgress` to accept the four display outcomes while retaining compatibility semantics:

```python
def record(self, item_key: str, outcome: CollectionOutcome) -> None:
    self.outcomes[item_key] = outcome

@property
def completed(self) -> int:
    return sum(outcome in {"saved", "skipped"} for outcome in self.outcomes.values())

@property
def failed(self) -> int:
    return sum(outcome in {"retry", "failed"} for outcome in self.outcomes.values())
```

Also expose `saved`, `skipped`, `retrying`, and `permanent_failures` count properties for summaries.

- [ ] **Step 4: Extract and instrument the existing collection loop**

Move the body of `collect_command` into `_consume_collection_jobs`. Keep the current lease token allocation, five-minute retry scheduling, durable failure handling, and interrupt reconciliation unchanged. Before each `collect_one`, capture `started = monotonic()`. After success classify `eligible` as `saved` and all other `CollectionResult.status` values as `skipped`; after `fail_job`, query `job_status` and classify `permanent_failure` as `failed`, otherwise `retry`.

Use this exact boundary so `collect` and `run` cannot drift:

```python
def _consume_collection_jobs(
    database: Database,
    pipeline: CatalogPipeline,
    settings: Settings,
    *,
    limit: int | None,
    appid: int | None,
    on_event: Callable[[_CollectionEvent], None],
    progress: _CollectionProgress,
) -> None:
```

After calling `progress.record`, query `game_progress_details`, construct `_CollectionEvent`, and invoke `on_event`. Compute `total` once from `collection_queue_size()` (or `1` for direct appid), bounded by `limit` when supplied. This ordering is required:

```python
result = pipeline.collect_one(appid, lease_token=active_lease_token)
outcome: CollectionOutcome = "saved" if result.status == "eligible" else "skipped"
progress.record(item_key, outcome)
name, review_count = database.game_progress_details(appid)
on_event(
    _CollectionEvent(
        sequence=progress.total,
        total=display_total,
        appid=appid,
        name=name,
        outcome=outcome,
        review_count=review_count,
        elapsed_seconds=monotonic() - started,
    )
)
```

For errors, call `database.fail_job(...)` before `progress.record(...)` and callback emission. Preserve reconciliation when interruption lands between commit, progress recording, callback, or pipeline cleanup. `collect_command` opens the existing contexts, calls the shared function, prints a detailed summary, and keeps exit statuses 0, 1, and 130.

- [ ] **Step 5: Run all CLI tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`

Expected: all CLI tests PASS, including the existing interruption race tests.

- [ ] **Step 6: Commit**

```powershell
git add src/steam_catalog/cli.py tests/test_cli.py
git commit -m "feat: log durable collection progress"
```

---

### Task 3: Integrated `run` Command

**Files:**
- Modify: `src/steam_catalog/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_consume_collection_jobs(...)` and `_print_collection_event(...)` from Task 2
- Produces: Typer command `run_command(context, library, source, resume, page_limit) -> None`
- Preserves: global `--database` placement and every existing standalone command

- [ ] **Step 1: Write failing command and orchestration tests**

Update the command-surface test to include `run`. Add one test using a temporary library plus a fake pipeline that records calls. Assert phase ordering (`Initialize`, `Import library`, `Discover`, `Collect`, `Summary`), importer execution only when `--library` is supplied, forwarding of `source/resume/page_limit`, collection until `claim_job` returns `None`, and final success.

```python
def test_run_orchestrates_all_phases_and_forwards_options(tmp_path: Path, monkeypatch):
    library = tmp_path / "library.json"
    library.write_text('[{"appid":10,"name":"Owned"}]', encoding="utf-8")
    observed: list[object] = []

    @contextmanager
    def fake_pipeline(database, settings):
        class FakePipeline:
            bootstrap_progress = RunStats()

            def bootstrap(self, source: str, *, resume: bool, page_limit: int | None):
                observed.append((source, resume, page_limit))
                return RunStats(discovered=1, queued=0)

        yield FakePipeline()

    monkeypatch.setattr("steam_catalog.cli._pipeline", fake_pipeline)
    result = runner.invoke(
        app,
        [
            "--database", str(tmp_path / "catalog.sqlite3"),
            "run", "--library", str(library), "--source", "steam", "--no-resume",
            "--page-limit", "2",
        ],
    )

    assert result.exit_code == 0
    assert observed == [("steam", False, 2)]
    assert result.stdout.index("Initialize") < result.stdout.index("Import library")
    assert result.stdout.index("Import library") < result.stdout.index("Discover")
    assert result.stdout.index("Discover") < result.stdout.index("Collect")
    assert result.stdout.index("Collect") < result.stdout.index("Summary")
```

Add a `KeyboardInterrupt` case in collection that verifies the active job returns to `pending`, prior completed games remain complete, output contains the interrupted summary, and exit code is 130. Add a no-library case that verifies no import phase is printed.

- [ ] **Step 2: Run focused run-command tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "test_run" -v`

Expected: FAIL because `run` is not registered.

- [ ] **Step 3: Implement `run_command`**

Register the command with the same option enums and bounds as bootstrap:

```python
@app.command("run")
@_command_errors
def run_command(
    context: typer.Context,
    library: Annotated[Path | None, typer.Option("--library", dir_okay=False)] = None,
    source: Annotated[DiscoverySource, typer.Option("--source")] = DiscoverySource.STEAMSPY,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    page_limit: Annotated[int | None, typer.Option("--page-limit", min=1)] = None,
) -> None:
    """Initialize, optionally import, discover, and collect until idle."""
```

Inside one `_database(settings)` context, print and execute initialization (already guaranteed by the context), optional `import_library`, then open `_pipeline`, call `bootstrap`, and call `_consume_collection_jobs` with `limit=None`, `appid=None`, and `_print_collection_event`. Keep separate bootstrap and collection progress objects so interruption summaries report only durable current-invocation work. Catch `KeyboardInterrupt`, print the appropriate durable summary, and raise `typer.Exit(130)`; after normal completion, print the detailed final summary and raise `typer.Exit(1)` only when retry/permanent-failure outcomes occurred in this invocation.

- [ ] **Step 4: Run all CLI tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`

Expected: all CLI tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/steam_catalog/cli.py tests/test_cli.py
git commit -m "feat: add integrated catalog run command"
```

---

### Task 4: Quick Start And Final Verification

**Files:**
- Modify: `README.md`
- Test: existing full test suite and Ruff configuration

**Interfaces:**
- Documents: `steam-catalog run --library ..\data\steam_info.json`
- Documents: subsequent resumable `steam-catalog run`
- Retains: individual command examples for advanced/manual operation

- [ ] **Step 1: Update the README quick start**

Replace the four separate data commands after installation with:

```powershell
steam-catalog run --library ..\data\steam_info.json
```

Immediately explain that later invocations use `steam-catalog run`, progress is printed after durable writes, `Ctrl+C` exits 130 with resumable state, and future-dated retries require a later invocation. Keep the Collection Workflow section for users who need bounded collection, alternate discovery, refresh, status, or export.

- [ ] **Step 2: Run formatting and static checks**

Run: `.\.venv\Scripts\python.exe -m ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 3: Run the full test suite once**

Run: `.\.venv\Scripts\python.exe -m pytest`

Expected: all tests PASS.

- [ ] **Step 4: Run CLI smoke checks without network collection**

Run:

```powershell
.\.venv\Scripts\steam-catalog.exe --help
.\.venv\Scripts\steam-catalog.exe run --help
.\.venv\Scripts\steam-catalog.exe --database .\data\smoke.sqlite3 status
```

Expected: top-level help lists `run`; run help lists `--library`, `--source`, `--resume/--no-resume`, and `--page-limit`; status exits 0 and prints category counts. Remove only the newly created `data/smoke.sqlite3` after verifying its resolved path is inside this repository.

- [ ] **Step 5: Inspect the final diff and commit documentation**

Run: `git diff --check` and `git status --short`.

Expected: no whitespace errors; only intended tracked files plus the user's pre-existing untracked `.idea/` and `data/steam_info.json` appear.

```powershell
git add README.md
git commit -m "docs: simplify catalog startup"
```
