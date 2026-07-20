# Task 4 Report: Bootstrap and Eligibility Pipeline

## Delivered

- Added `CatalogPipeline.bootstrap(source, *, resume=True, page_limit=None) -> RunStats`
  for SteamSpy paging and Steam app-list fallback.
- Persisted every discovery payload and a minimal game row, queued only provisional
  candidates meeting the configured threshold, and preserved prior collected metadata.
- Made each bootstrap page atomic so an interrupted page is retried rather than partially
  skipped; restart mode requeues work, while resume mode begins at the next stored page.
- Added atomic due-job claiming with attempt increments, bounded failure text, retry scheduling,
  permanent failures, completion, and startup recovery of hour-old running jobs.
- Added `CatalogPipeline.collect_one(appid) -> CollectionResult`, which fetches the Steam review
  summary before store details, treats Steam totals as authoritative, and stops immediately below
  the verified threshold.
- Persisted eligible and explicitly skipped app details with `not_a_game`, `unreleased`, and
  `unavailable` reasons. Each terminal game update and collect-job completion share one SQLite
  transaction.
- Used fake provider clients only in tests. No live network request was made.

## TDD Evidence

All commands used the existing offline Task 3 interpreter and local dependency path:

```powershell
$env:PYTHONPATH = '.venv\Lib\site-packages'
& 'C:\Users\zrezl\.codex\visualizations\2026\07\12\019f56e1-af6e-7131-a5de-0885dc372162\steam_catalog_venv\Scripts\python.exe' -m pytest -p no:cacheprovider --basetemp <external-temp> ...
```

### RED

- Initial pipeline suite: collection stopped with `ModuleNotFoundError: No module named
  'steam_catalog.pipeline'`.
- Job repository cycle: `4 failed, 3 passed`; `claim_job`, `fail_job`, and `complete_job` were
  absent, and a stale running job remained `running` after reopen.
- Collection integration cycle: `3 failed, 11 passed`; skipped details were not stored, a claimed
  job remained `running`, and forced completion failure did not roll back the game write.
- Self-review resumability cycle: `3 failed`; a failed bootstrap left a partially committed page,
  a provisional refresh left stale collect work pending, and equivalent `Z`/`+00:00` due times
  were compared incorrectly.
- Metadata preservation cycle: the provisional refresh test failed because verified review count
  `42` was replaced by `NULL`.

### GREEN

- Core brief tests: `3 passed`.
- Job repository tests: `7 passed`.
- Bootstrap, collection, and atomic rollback tests: `14 passed`.
- Self-review edge cases: `3 passed`.
- Final focused Task 4 suite: `17 passed in 0.79s`.

## Final Verification

- Focused pipeline suite: `17 passed in 0.79s`.
- Full suite: `91 passed in 1.79s`.
- `ruff check --no-cache src tests`: `All checks passed!`.
- `ruff format --check --no-cache src tests`: `16 files already formatted`.
- `git diff --check`: exit code `0`; only Git's LF-to-CRLF advisory was emitted.

The direct Ruff formatting write was denied by workspace permissions. Ruff's read-only diff
contained one signature-only change, which was applied with `apply_patch`; the final format check
then passed.

## Self-Review

- Checked every Task 4 brief requirement against implementation and tests.
- Confirmed claims run under `BEGIN IMMEDIATE`, retry timestamps use SQLite date parsing rather
  than lexical ISO-string ordering, and stale recovery retains attempt history.
- Confirmed provisional refreshes preserve descriptions, FTS entries, and verified Steam
  aggregates while removing stale collect work.
- Confirmed the authoritative review probe is never persisted as a review sample and no details
  request occurs below the verified threshold.
- Confirmed eligible, skipped, and below-threshold outcomes commit game and job state atomically.
- Confirmed existing schema, savepoints, clients, and frozen domain models were reused unchanged.

## Concerns

- Adaptive review sampling remains intentionally deferred to Task 5; Task 4 stores only the
  authoritative aggregate from its one-review-page eligibility probe.

---

## Review Fixes at HEAD `3cf383a`

### Delivered

- Made repeated resume bootstrap idempotent for terminal collect work. A game is marked
  `pending` only when `enqueue_job` creates or explicitly requeues its job; unchanged
  `complete` and `permanent_failure` jobs retain their terminal game state.
- Fenced `complete_job` and `fail_job` with the claimed `attempt_count`. Both operations now
  require the expected lease, update only a matching `running` row, and raise `RuntimeError`
  when no row matches so an enclosing game transaction rolls back.
- Passed the claimed lease through every `CatalogPipeline.collect_one` terminal path. Direct
  collection without a sync job remains supported, while queued work requires a lease.
- Applied a configured attempt ceiling during stale recovery and before each claim. Exhausted
  stale, pending, and failed work becomes `permanent_failure` and cannot be reclaimed.
- Separated provider field ownership without a schema migration. Discovery refreshes source
  payloads and SteamSpy counts and fills only a missing name; Steam details preserve SteamSpy
  aggregates.
- Prioritized collect claims by `COALESCE(steam_total_reviews,
  steamspy_positive + steamspy_negative, 0) DESC`, then SQLite `julianday(updated_at)`, then
  `item_key`, all inside the existing `BEGIN IMMEDIATE` claim transaction.

### RED Evidence

The unchanged Task 4 suite first established a clean baseline:

```text
tests/test_pipeline.py: 17 passed in 0.78s
```

After adding the review regressions and updating expected lease call sites, before production
changes:

```text
tests/test_pipeline.py: 13 failed, 11 passed in 1.14s
```

The failures matched the findings:

- Lease ownership: `complete_job` and `fail_job` rejected the new
  `expected_attempt_count` argument, and `collect_one` rejected `lease_attempt_count`.
- Field ownership: discovery replaced `Collected name` with `Game 10`.
- Attempt exhaustion: a pending job at attempt three was claimed again, and `Database` had no
  configured attempt ceiling for stale recovery.
- Priority: the claim path had no review-count ordering and could not complete through the new
  fenced API.
- Repeated Steam fallback: terminal-state setup could not use a fenced completion/failure and
  the existing bootstrap path unconditionally wrote game status `pending`.

### GREEN Evidence

After implementing the repository and pipeline changes:

```text
tests/test_pipeline.py: 24 passed in 1.13s
full suite: 98 passed in 2.08s
ruff check --no-cache src tests: All checks passed!
```

The stale-failure regression was then strengthened to place a game status write around the
illegal `fail_job` transition. Its focused rerun remained green:

```text
tests/test_pipeline.py: 24 passed in 1.07s
ruff format --check --no-cache src tests: 16 files already formatted
```

As in the original Task 4 work, Ruff's direct formatting write was denied by workspace
permissions. Its read-only `--diff` contained only deterministic line reflows; those exact
changes were applied with `apply_patch` before the successful format check.

### Review-Fix Self-Review

- Confirmed claim selection, attempt normalization, lease increment, and candidate update remain
  within one immediate transaction.
- Confirmed both terminal job methods predicate on `status = 'running'` and the exact claimed
  attempt, and raise inside nested savepoints so surrounding game writes are rolled back.
- Confirmed two crash/recovery cycles at a two-attempt ceiling end in `permanent_failure`, and an
  already exhausted pending row is normalized before selection.
- Confirmed Steam fallback resume preserves both complete/eligible and
  permanent-failure/ineligible pairs, while `resume=False` still explicitly requeues work.
- Confirmed bootstrap -> collect -> refresh retains the Steam store name and description,
  refreshes SteamSpy counts, and retains the authoritative Steam review total.
- Confirmed priority falls back from Steam totals to SteamSpy totals and parses timezone offsets
  with `julianday` before using the stable item key tie-break.

### Review-Fix Concerns

- The attempt ceiling is process configuration on `Database` because the approved schema has no
  per-job limit column. All workers opening the same catalog must therefore use the same
  `max_attempts` value.

### Final Review-Fix Verification

```text
focused pipeline suite: 24 passed in 1.60s
full suite: 98 passed in 2.63s
ruff check --no-cache src tests: All checks passed!
ruff format --check --no-cache src tests: 16 files already formatted
git diff --check: exit 0 (LF-to-CRLF advisory only)
```
