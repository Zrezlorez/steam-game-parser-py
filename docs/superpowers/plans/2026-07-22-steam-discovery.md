# Official Steam Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore official paginated Steam discovery with an API key and preserve explicit direct-Steam rechecks for games below the verified review threshold.

**Architecture:** Load an optional Steam Web API key from settings and pass it explicitly to `SteamClient`. The client yields parsed `IStoreService/GetAppList/v1` pages; the pipeline commits each page independently and queues new appids through its existing bootstrap logic. Existing `refresh --reason below_verified_threshold` remains the separate recheck path.

**Tech Stack:** Python 3.11+, SQLite, httpx, Typer, pytest.

## Global Constraints

- SteamSpy remains the default source and must not require a Steam Web API key.
- `--source steam` must fail before HTTP access when the key is missing or blank.
- Request only games and do not introduce another app-list provider.
- Do not change database schema or automatically requeue completed jobs during bootstrap resume.
- Tests use mocked HTTP responses and temporary databases; never persist a real API key.

---

### Task 1: Load and wire the Steam Web API key

**Files:**
- Modify: `src/steam_catalog/config.py`
- Modify: `src/steam_catalog/cli.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `Settings.steam_web_api_key: str | None`.
- Consumes: `SteamClient(http, api_key=settings.steam_web_api_key)` in `_pipeline()`.

- [x] **Step 1: Add failing configuration tests**

Assert `STEAM_CATALOG_STEAM_WEB_API_KEY` is loaded after trimming surrounding whitespace, and missing or whitespace-only values become `None`.

- [x] **Step 2: Implement the setting and CLI wiring**

Add the immutable setting field, normalize it in `Settings.from_env()`, and pass it into `SteamClient` without logging it.

- [x] **Step 3: Verify configuration tests**

Run: `pytest tests/test_config.py -q`

Expected: all configuration tests pass.

### Task 2: Replace the removed app-list API with paginated official discovery

**Files:**
- Modify: `src/steam_catalog/sources/steam.py`
- Modify: `tests/test_steam.py`

**Interfaces:**
- Produces: `SteamClient.fetch_app_pages() -> Iterator[tuple[list[DiscoveryItem], bool]]`, where the boolean marks the final page.
- Produces: `parse_app_list_page(payload) -> tuple[list[DiscoveryItem], bool, int | None]`.

- [x] **Step 1: Add failing API and parser tests**

Cover a missing-key error before any HTTP call, two response pages, `key`, `include_games=true`, `max_results=50000`, the second-page `last_appid`, compact payload preservation, malformed response rejection, and non-advancing continuation cursors.

- [x] **Step 2: Implement page parsing**

Read `payload["response"]["apps"]`, retain positive integer appids, return exact-boolean `have_more_results`, and accept a positive integer `last_appid` only when present. Reject malformed top-level and pagination fields.

- [x] **Step 3: Implement key validation and pagination**

Use `https://api.steampowered.com/IStoreService/GetAppList/v1/`. Require a nonblank key, send only documented parameters, yield each page, stop when `have_more_results` is false, and reject missing or non-advancing continuation cursors.

- [x] **Step 4: Verify Steam source tests**

Run: `pytest tests/test_steam.py -q`

Expected: all Steam source tests pass.

### Task 3: Commit official pages independently and document both workflows

**Files:**
- Modify: `src/steam_catalog/pipeline.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reviews.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `SteamProvider.fetch_app_pages() -> Iterator[tuple[list[DiscoveryItem], bool]]`.
- Produces: sequential official `source_page` values and existing `BootstrapProgressEvent` events per page.

- [x] **Step 1: Add failing pipeline coverage**

Provide two official pages containing an app absent from SteamSpy. Assert two saved progress events, separate `source_page` values, persisted source items, and new `collect` jobs. Assert `page_limit=1` consumes only one official page.

- [x] **Step 2: Change the provider protocol and official discovery loop**

Call `fetch_app_pages()` lazily, announce `fetching` before each page request, yield each page separately to existing bootstrap persistence, and honor `page_limit` for official pages.

- [x] **Step 3: Preserve and prove threshold refresh behavior**

Add regression coverage that `schedule_refresh(..., reason="below_verified_threshold")` requeues every matching non-running job, and that subsequent collection reads Steam review totals through `fetch_reviews()`.

- [x] **Step 4: Document configuration and commands**

Document how to obtain/set `STEAM_CATALOG_STEAM_WEB_API_KEY`, run official discovery followed by collection, and separately refresh `below_verified_threshold` followed by collection.

- [x] **Step 5: Run focused verification**

Run: `pytest tests/test_config.py tests/test_steam.py tests/test_pipeline.py tests/test_cli.py -q`

Expected: all selected tests pass.

Run: `ruff check --no-cache src tests`

Expected: no lint errors.
