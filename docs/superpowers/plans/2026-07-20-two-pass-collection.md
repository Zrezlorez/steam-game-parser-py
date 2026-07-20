# Two-Pass Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove one Steam review request from primary collection and add a separate resumable USD/UAH price pass.

**Architecture:** Primary collection reuses its broad recent review page and stores only KZ metadata/KZT. Regional prices use a distinct `collect_prices` job kind, merge USD/UAH into `prices_json`, and record completion in `regional_prices_fetched_at` without touching other game data.

**Tech Stack:** Python 3.11+, Typer, SQLite, HTTPX, Ruff.

## Global Constraints

- `run` must not invoke regional-price collection automatically.
- `prices_json` uses absent USD/UAH keys for uncollected regions and explicit `null` for queried regions without an offer.
- Existing databases must migrate additively without losing catalog data.
- Existing collection leases, retry policy, interruption behavior, and rate limiting remain authoritative.
- Per user instruction, omit the full pytest cycle; verify syntax, Ruff, CLI help, and focused mocked behavior instead.

---

### Task 1: Reuse the broad recent review page

**Files:**
- Modify: `src/steam_catalog/pipeline.py`

**Interfaces:**
- Consumes: `SteamProvider.fetch_reviews(appid, filter, review_type, limit) -> ReviewPage`
- Produces: `CatalogPipeline.collect_review_samples(appid, summary, recent_page)` with no second broad recent request.

- [ ] Change `collect_one` to fetch `filter="recent"`, `review_type="all"`, `limit=100` as its first review request.
- [ ] Pass that page into `collect_review_samples` after eligibility checks.
- [ ] Change `collect_review_samples` to use the supplied recent page and fetch only the broad helpful page plus targeted shortfall pages.
- [ ] Run `python -m compileall src/steam_catalog` and confirm exit code 0.
- [ ] Commit as `perf: reuse recent review page`.

### Task 2: Split KZ metadata from regional prices

**Files:**
- Modify: `src/steam_catalog/sources/steam.py`
- Modify: `src/steam_catalog/pipeline.py`

**Interfaces:**
- Produces: `SteamClient.fetch_app_details(appid) -> GameDetails | None`, requesting only KZ.
- Produces: `SteamClient.fetch_regional_prices(appid) -> str`, returning compact JSON containing `USD` and `UAH` keys.
- Produces: `CatalogPipeline.collect_prices_one(appid, lease_token=None) -> CollectionResult`.

- [ ] Refactor `fetch_app_details` to parse KZ metadata and set `prices_json` to a one-key KZT object.
- [ ] Add `fetch_regional_prices` that requests US and UA, validates expected currencies, and returns both keys atomically.
- [ ] Extend `SteamProvider` with the regional-price method.
- [ ] Add pipeline merging that preserves the existing KZT entry, writes USD/UAH, and completes only the `collect_prices` job.
- [ ] Run `python -m compileall src/steam_catalog` and confirm exit code 0.
- [ ] Commit as `perf: split regional price collection`.

### Task 3: Persist and queue the regional pass

**Files:**
- Modify: `src/steam_catalog/db.py`

**Interfaces:**
- Produces: `Database.schedule_price_collection(updated_at=None) -> int`.
- Produces: `Database.update_regional_prices(appid, prices_json, fetched_at, expected_lease_token=None) -> None`.
- Produces: eligibility and progress lookup helpers used by the CLI.

- [ ] Add nullable `regional_prices_fetched_at` to fresh schema and additive migrations.
- [ ] Ensure primary metadata upserts clear the timestamp and replace old three-region JSON with the new KZT-only snapshot.
- [ ] Schedule `collect_prices` jobs only for eligible rows with a null regional timestamp, leaving running/completed work intact.
- [ ] Add an atomic update that verifies the lease, merges prices, records the timestamp, and completes the job.
- [ ] Run database initialization against a temporary SQLite path and inspect `PRAGMA table_info(games)` for the new column.
- [ ] Commit as `feat: queue regional price collection`.

### Task 4: Add the `collect-prices` command

**Files:**
- Modify: `src/steam_catalog/cli.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `steam-catalog collect-prices [--limit N] [--appid APPID]`.

- [ ] Reuse the collection command's lease/retry/interruption conventions with the `collect_prices` job kind.
- [ ] Bulk mode schedules missing eligible games, then consumes them up to `--limit` with durable progress lines and summary.
- [ ] Direct mode validates eligibility and forcibly requeues exactly one app before collection.
- [ ] Document the faster main pass, absent-vs-null currency semantics, second-pass commands, resumption, and direct refresh in Russian README.
- [ ] Run CLI `--help` and `collect-prices --help`; confirm command and options are present.
- [ ] Run Ruff format/check and fix all reported issues.
- [ ] Inspect `git diff --check` and commit as `feat: add regional price command`.

### Task 5: Focused behavioral smoke check

**Files:**
- No production changes expected.

**Interfaces:**
- Verifies the complete data flow without public network calls.

- [ ] Use the existing fake providers and a temporary SQLite database to collect one eligible game; confirm the initial review call requests 100 recent reviews and stored JSON contains only KZT.
- [ ] Run the regional pass for that game; confirm JSON contains KZT, USD, and UAH and `regional_prices_fetched_at` is populated.
- [ ] Run `python -m compileall src/steam_catalog`, Ruff format check, and Ruff lint once more.
- [ ] Inspect `git status --short` and ensure only intentional user/runtime files remain uncommitted.
