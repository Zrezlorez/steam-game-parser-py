# Regional Prices and Reason-Based Recheck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use KZ as the authoritative Steam storefront, store KZT/USD/UAH prices, requeue games by skip reason, and clarify review totals in collection progress.

**Architecture:** Keep the existing client, pipeline, single `games` table, and Typer commands. `SteamClient.fetch_app_details()` performs one authoritative KZ request plus US and UA price requests, then returns the existing `GameDetails` value with one compact `prices_json` field; schema version 2 replaces `price_json` without migrating its ambiguous contents.

**Tech Stack:** Python 3.11+, Typer, SQLite, httpx, pytest, Ruff.

## Global Constraints

- KZ alone determines availability and non-price metadata.
- Store only `prices_json` / exported `prices`; remove `price_json` / exported `price` completely.
- Keep the solution inside existing files and dependencies; add no table, service, or abstraction layer.
- Preserve current durable retry, lease, cache, and transaction behavior.

---

### Task 1: Regional prices, schema migration, export, and progress labels

**Files:**
- Modify: `src/steam_catalog/models.py`
- Modify: `src/steam_catalog/sources/steam.py`
- Modify: `src/steam_catalog/db.py`
- Modify: `src/steam_catalog/exporters.py`
- Modify: `src/steam_catalog/cli.py`
- Modify: `tests/fixtures/steam_appdetails.json`
- Modify: `tests/test_steam.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_exporters.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `GameDetails.prices_json: str | None`.
- Produces: `Database.game_progress_details(appid) -> tuple[str | None, int, int]` containing name, Steam total reviews, and stored samples.
- Preserves: `SteamClient.fetch_app_details(appid) -> GameDetails | None` and pipeline callers.

- [ ] **Step 1: Add failing source, migration, export, and progress tests**

Assert that `fetch_app_details(10)` requests countries in order `KZ`, `US`, `UA`; KZ `success=false` stops and returns `None`; US/UA `success=false` yields a null currency; each successful price currency matches KZT/USD/UAH; free regional responses yield zero values. Update schema expectations to require `prices_json` and forbid `price_json`, and add a version-1 migration test that inserts an old price before initialization and verifies it is discarded. Update exporter assertions to require `prices` and no `price`. Update collection output assertions to expect:

```text
saved, reviews=124176, samples=17
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m pytest tests/test_steam.py tests/test_db.py tests/test_exporters.py tests/test_cli.py -q
```

Expected: failures mention missing KZ/US/UA calls, `prices_json`, schema version 2, and the new progress fields.

- [ ] **Step 3: Implement the minimal regional price flow**

In `SteamClient.fetch_app_details`, request KZ first and return early on `success=false`. Reuse its payload for KZT, request US and UA only after KZ succeeds, validate any supplied `price_overview.currency`, and attach compact JSON shaped as:

```python
{
    "KZT": kz_price_or_none,
    "USD": usd_price_or_none,
    "UAH": uah_price_or_none,
}
```

Use each response's own `is_free` flag to emit `currency`, `initial=0`, `final=0`, and `discount_percent=0` when `price_overview` is absent for a free game. Keep malformed successful responses as `ValueError` so the existing collection retry policy handles them.

- [ ] **Step 4: Replace the persisted and exported price field**

Set `SCHEMA_VERSION = 2`; create fresh databases with `prices_json`; migrate version 1 with:

```sql
ALTER TABLE games DROP COLUMN price_json;
ALTER TABLE games ADD COLUMN prices_json TEXT;
UPDATE schema_version SET version = 2;
```

Change `upsert_game` to use an explicit column list so migrated column order is irrelevant. Replace model and exporter mappings with `prices_json -> prices`, add compact `prices` JSON to CSV, and remove every runtime `price_json` / `price` reference.

- [ ] **Step 5: Report aggregate reviews and samples separately**

Select both `games.steam_total_reviews` and `COUNT(reviews.recommendation_id)` in `game_progress_details`; carry them as `total_reviews` and `sample_count` in `_CollectionEvent`; render the detail only for saved outcomes as:

```python
f", reviews={event.total_reviews}, samples={event.sample_count}"
```

- [ ] **Step 6: Run focused tests and commit**

Run the Step 2 command. Expected: all selected tests pass.

```powershell
git add src tests
git commit -m "feat: collect regional prices"
```

---

### Task 2: Refresh by reason and user documentation

**Files:**
- Modify: `src/steam_catalog/db.py`
- Modify: `src/steam_catalog/cli.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `Database.schedule_refresh(cutoff, *, reason: str | None = None, include_failed=False, updated_at=None) -> int`.
- Produces: CLI option `steam-catalog refresh --reason TEXT`.

- [ ] **Step 1: Add failing refresh tests**

Create games with `skip_reason` values `unavailable`, `unreleased`, and `None`, including a future-dated unavailable row, a running unavailable job, and a permanent failure. Assert `--reason unavailable` immediately requeues only exact matches, leaves running work untouched, returns `Scheduled: 0` for an unknown reason, rejects whitespace-only input, and includes permanent failures only with `--failed`. Retain the existing age-only test unchanged.

- [ ] **Step 2: Run refresh tests and verify failure**

Run:

```powershell
python -m pytest tests/test_db.py tests/test_cli.py -k "refresh" -q
```

Expected: failures show that `reason` and `--reason` are not yet accepted.

- [ ] **Step 3: Implement the selector**

Add `reason: str | None` to `schedule_refresh`. When non-null, match `games.skip_reason = ?` instead of the age expression; in both modes exclude running and permanent-failure jobs from the ordinary selection, then union permanent failures only when `include_failed` is true. In `refresh_command`, reject `not reason.strip()` with `ValueError("reason must not be empty")`, pass the stripped reason, and keep the current cutoff calculation for the no-reason path.

- [ ] **Step 4: Update README examples and field descriptions**

Document `steam-catalog refresh --reason unavailable`, KZ-authoritative collection, `prices` with KZT/USD/UAH, and the distinction between `reviews=N` and `samples=N`. Remove documentation for singular exported `price` if present.

- [ ] **Step 5: Run all verification and commit**

Run:

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Expected: all tests pass and both Ruff commands exit 0.

```powershell
git add src tests README.md
git commit -m "feat: refresh skipped games by reason"
```
