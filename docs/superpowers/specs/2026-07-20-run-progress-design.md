# `run` Command And Durable Progress Design

## Goal

Make a full catalog update easy to start and visibly active. A single command must initialize the database, optionally import the user's Steam library, discover catalog candidates, and collect every queued game while reporting progress that matches durable SQLite state.

## Command Interface

Add `steam-catalog run` while keeping all existing commands available.

The command accepts:

- `--library PATH` to import `steam_info.json` before discovery. It is optional so subsequent runs do not need to repeat the import.
- `--source steamspy|steam`, defaulting to `steamspy`, with the same meaning as `bootstrap`.
- `--resume/--no-resume`, defaulting to resume, with the same meaning as `bootstrap`.
- `--page-limit INTEGER` as an optional discovery bound. It does not limit collection.

There is deliberately no collection limit: `run` consumes eligible queued work until the queue has no immediately claimable jobs. The existing `collect --limit` command remains available for bounded/manual runs. The global `--database` option continues to work before `run`.

## Orchestration

The CLI orchestrates existing operations in this order:

1. Open and initialize the database.
2. Import the library when `--library` is supplied.
3. Bootstrap discovery using the selected source and resume settings.
4. Consume collect jobs until no job is immediately claimable.
5. Print a final summary for this invocation.

The implementation reuses existing database, importer, bootstrap, and collection behavior. It does not invoke CLI commands as subprocesses and does not duplicate source clients or collection rules.

Existing standalone commands keep their current behavior, but `collect` also uses the shared progress reporting boundary so its unbounded and bounded forms gain per-game visibility.

## Console Output

Output is console-only. No log file is created.

Each phase begins with a short heading. Discovery reports durable aggregate/page progress where the existing bootstrap boundary permits it. Collection prints one line after each job outcome has been committed, for example:

```text
[42/18320] Factorio (appid 427520) - saved, reviews=20, 1.7s
[43/18320] Unknown name (appid 123456) - retry, 0.8s: HTTP 429
```

Each collection line contains:

- current invocation sequence and the queue size observed at collection start;
- game name when known, with `Unknown name` as the fallback;
- appid;
- durable outcome (`saved`, `skipped`, `retry`, or `failed`);
- useful result counts when available;
- elapsed processing time.

Messages are emitted only after the corresponding transaction or job-state update succeeds. Therefore every printed terminal outcome is reflected in SQLite. Raw HTTP payloads, review text, tracebacks, and routine request diagnostics are not printed.

The final summary reports completed, skipped/rejected where distinguishable, retry/failed counts, and elapsed time for the invocation. A non-interrupted run exits nonzero when durable failures occurred, preserving the current collection contract.

## Progress Boundary

Collection progress is represented by a small event/value object passed to an optional callback. The pipeline or shared collection helper constructs the event only after `collect_one` has completed its commit, or after the caller has durably recorded a failed/retry job state. CLI rendering stays outside the pipeline so database and source logic remain independent of Typer.

The event includes appid, resolved name, outcome, review/result counts when available, elapsed seconds, and sequence totals supplied by the orchestrator. Existing callers may omit the callback.

## Interruptions And Errors

`Ctrl+C` releases an active lease when the job has not already reached a durable terminal state. Already committed games and completed discovery pages remain untouched. The command prints an interrupted summary and exits with status 130. Running the same command again resumes discovery and collection from SQLite.

Expected per-game errors are persisted through the existing retry/failure policy, logged once after that state is committed, and collection continues. Initialization, library import, or discovery errors that prevent the next phase stop `run` with the existing concise `Error: ...` handling.

"Until empty" means until there are no jobs claimable now. Jobs scheduled for a future retry remain in SQLite and are shown in the summary/status; `run` does not sleep for retry delays indefinitely.

## Tests And Documentation

Focused tests will verify:

- command registration and option forwarding;
- phase ordering with and without `--library`;
- per-game output fields and ordering after durable completion/failure;
- processing until the immediately claimable queue is empty;
- active lease release, summary, and exit code on `Ctrl+C`;
- unchanged standalone command compatibility.

The README quick start will use the single `steam-catalog run --library ..\data\steam_info.json` command, explain that later runs can use `steam-catalog run`, and retain the individual commands for advanced control. Final verification consists of focused tests during development, one full pytest run, Ruff, and a small mocked or live smoke test where practical.

## Non-Goals

- Persistent log files or configurable logging frameworks.
- A PowerShell wrapper.
- Parallel collection.
- Automatic periodic retries or daemon behavior.
- Removing or renaming existing commands.
