# Steam Catalog

Steam Catalog builds a local SQLite catalog of released Steam games with verified review totals,
store metadata, small categorized review samples, optional personal-library data, full-text search,
and streaming JSONL or CSV exports. It requires Python 3.11 or newer.

## Quick Start

Run this exact PowerShell sequence from the directory that contains `steam_catalog` and `data`:

```powershell
cd steam_catalog
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
steam-catalog run --library ..\data\steam_info.json
```

The first `run` initializes SQLite, imports the library, discovers games through SteamSpy, and
consumes all collection jobs available now. Later incremental updates need only:

```powershell
steam-catalog run
```

Each game is printed after its result is committed to SQLite. Pressing Ctrl+C exits with status
130 and leaves the catalog resumable with the same command. Network failures are scheduled for a
later retry; `run` stops when no job is claimable now instead of waiting for retry delays.

The global `--database` option selects another SQLite file. It must appear before the command:

```powershell
steam-catalog --database .\data\alternate.sqlite3 run
python -m steam_catalog --database .\data\alternate.sqlite3 status
```

Every data command initializes the schema when needed. The individual commands below remain
available when bounded runs or manual control are useful.

## Collection Workflow

SteamSpy is the recommended discovery source because its provisional counts avoid unnecessary
Steam Store requests. Steam's application list is available as a fallback.

```powershell
# Discover all SteamSpy pages and preserve completed pages on restart.
steam-catalog bootstrap --source steamspy --resume

# Or discover from Steam's app list.
steam-catalog bootstrap --source steam --resume

# Try a bounded collection run, then consume the complete durable queue.
steam-catalog collect --limit 10
steam-catalog collect

# Fetch exactly one app without changing unrelated queued work.
steam-catalog collect --appid 4000

# Inspect eligibility, completeness, queue, retry, and failure counts.
steam-catalog status
```

The initial bootstrap and full collection can take many hours. Cached requests and durable jobs
make later runs incremental. Pressing Ctrl+C leaves work resumable and exits with status 130.

To schedule records whose authoritative Steam verification is older than a UTC cutoff, use
`refresh`. Rows that have never been verified are conservatively stale. Permanent failures remain
untouched unless `--failed` is explicit:

```powershell
steam-catalog refresh --older-than-days 30
steam-catalog refresh --older-than-days 30 --failed
steam-catalog collect
```

## Library Import

`import-library` accepts a top-level JSON array of Steam library entries keyed by `appid`. Repeated
imports update library-owned fields without replacing collected catalog metadata.

```powershell
steam-catalog import-library ..\data\steam_info.json
```

## Search

The database maintains FTS5 indexes for normalized game metadata and stored review samples. Open
the configured database in any SQLite client and use queries such as:

```sql
SELECT games.appid, games.name
FROM game_search
JOIN games ON games.appid = game_search.rowid
WHERE game_search MATCH 'factory automation'
ORDER BY bm25(game_search)
LIMIT 20;

SELECT reviews.appid, games.name, reviews.recommendation_id
FROM review_search
JOIN reviews ON reviews.rowid = review_search.rowid
JOIN games ON games.appid = reviews.appid
WHERE review_search MATCH 'difficulty curve'
ORDER BY bm25(review_search)
LIMIT 20;
```

## Export

Exports include only verified eligible games and replace the destination atomically. JSONL can
optionally include the stored review records; CSV deliberately stays game-oriented.

```powershell
steam-catalog export .\data\catalog.jsonl --format jsonl
steam-catalog export .\data\catalog-with-reviews.jsonl --format jsonl --include-reviews
steam-catalog export .\data\catalog.csv --format csv
```

Normal CLI output contains counts and statuses, not raw source payloads or review text.

## Sources And Limits

- Steam Web API supplies the application list; Steam Store endpoints supply app details and review
  summaries/samples.
- SteamSpy supplies discovery pages and provisional review counts. It has no paid requirement, but
  its API has strict rate limits; the collector deliberately spaces full-catalog requests.
- SteamDB is not used or accessed.
- Source endpoints and response formats are third-party interfaces and may change. Parser errors
  are reported concisely and leave durable work available for a later retry.
- Runtime dependencies remain `httpx` and `typer`; development checks additionally use `pytest`,
  `respx`, and `ruff`.
