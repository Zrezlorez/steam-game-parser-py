# HTTP Cache Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop persisting raw HTTP responses and add `steam-catalog compact` to remove the existing cache and reclaim SQLite pages.

**Architecture:** Keep the existing HTTP client interface, retry behavior, rate limiting, and JSON validation, but bypass all SQLite cache reads and writes. Put destructive cache cleanup and SQLite maintenance in `Database.compact_http_cache()`, with a thin Typer command that reports the result.

**Tech Stack:** Python 3.11+, SQLite, httpx, Typer, pytest.

## Global Constraints

- Keep the `http_cache` table for compatibility but do not populate it during normal HTTP requests.
- Preserve catalog data, normalized reviews, FTS indexes, source items, and jobs.
- Do not add compression, replacement cache configuration, or a schema migration.
- Do not run compaction against the multi-gigabyte production database during verification.

---

### Task 1: Stop persisting raw HTTP responses

**Files:**
- Modify: `src/steam_catalog/http.py`
- Modify: `tests/test_http.py`

**Interfaces:**
- Consumes: Existing `CachedHttpClient.get_json(...) -> dict[str, Any]` callers.
- Produces: The same public method signature and network behavior without `http_cache` reads or writes.

- [x] **Step 1: Replace cache behavior tests with a raw-response non-persistence test**

Add a test that performs the same request twice, expects two transport calls, and asserts `SELECT count(*) FROM http_cache` is zero.

- [x] **Step 2: Run the focused test and verify the old implementation fails**

Run: `pytest tests/test_http.py::test_repeated_requests_are_not_persisted -v`

Expected: FAIL because the second request currently uses SQLite cache and `calls == 1`.

- [x] **Step 3: Remove cache reads, conditional revalidation, and response writes**

Keep request normalization, retries, rate buckets, response status validation, UTF-8 decoding, and `_decode_json`. `get_json()` must call `_request_with_retries(normalized_url, {}, rate_bucket)` and return decoded response JSON without calling `get_http_cache()` or `upsert_http_cache()`.

- [x] **Step 4: Run focused HTTP tests**

Run: `pytest tests/test_http.py -q`

Expected: all HTTP tests pass.

### Task 2: Add database compaction and CLI command

**Files:**
- Modify: `src/steam_catalog/db.py`
- Modify: `src/steam_catalog/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `Database.compact_http_cache() -> int`, returning the number of removed rows.
- Produces: `steam-catalog compact`, printing path, removed-row count, and database sizes before and after.

- [x] **Step 1: Add failing CLI coverage**

Create a temporary initialized database containing one game and one `http_cache` row. Invoke `compact`, then assert the command succeeds, reports one removed row, leaves the game intact, and leaves `http_cache` empty. Add `compact` to the exact help surface assertion.

- [x] **Step 2: Run the focused CLI test and verify failure**

Run: `pytest tests/test_cli.py -k "compact or help_lists_exact_command_surface" -v`

Expected: FAIL because `compact` is not registered.

- [x] **Step 3: Implement `Database.compact_http_cache()`**

Delete all `http_cache` rows in a committed transaction, execute `PRAGMA wal_checkpoint(TRUNCATE)`, execute `VACUUM`, and return the deleted-row count. Let SQLite locking and disk errors propagate to existing CLI error handling.

- [x] **Step 4: Register `steam-catalog compact`**

Read the file size before opening the maintenance operation, call `compact_http_cache()`, read the size after `VACUUM`, and print the resolved database path, removed rows, and both sizes in MiB.

- [x] **Step 5: Document usage and concurrency restriction**

Add `steam-catalog compact` to README command examples and state that it must not run concurrently with `run`, `bootstrap`, `collect`, or `collect-prices`.

- [x] **Step 6: Run focused verification**

Run: `pytest tests/test_http.py tests/test_cli.py -q`

Expected: all selected tests pass.

Run: `python -m steam_catalog.cli --help`

Expected: help lists `compact`.
