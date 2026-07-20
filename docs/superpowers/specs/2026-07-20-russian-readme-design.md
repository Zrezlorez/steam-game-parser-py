# Russian GitHub README Design

## Goal

Replace the current English README with a clear Russian guide that lets a Windows/PowerShell user understand the project, install it from scratch, run collection safely, and diagnose common startup or data-state questions without reading the source code.

## Audience And Scope

The README targets Windows users who are comfortable pasting PowerShell commands but may not know Python virtual environments, editable installs, SQLite, or the project's two-stage collection model.

Only Windows and PowerShell commands will be documented. The change is documentation and repository hygiene only: application behavior, dependencies, database schema, and CLI interfaces remain unchanged.

## README Structure

The document will prioritize the first successful run, then provide reference material:

1. Project purpose and collected data.
2. Key properties: local SQLite, resumability, SteamSpy discovery, Steam Store verification, FTS5, and optional personal-library import.
3. Requirements: Windows, PowerShell, Python 3.11 or newer, internet access, and expected runtime/disk caveats.
4. Exact installation from a Git checkout: enter the repository directory, create `.venv`, activate it, install the project, and verify `steam-catalog --help`.
5. First run with optional `data/steam_info.json`, plus later incremental runs.
6. Explanation of `Discover` versus `Collect`, including why most rows initially contain placeholders and when descriptions/reviews appear.
7. Progress output, durable `saved` messages, safe `Ctrl+C`, resume behavior, future retries, and `--page-limit 1` for incremental discovery/collection.
8. A command reference for `run`, `init`, `import-library`, `bootstrap`, `collect`, `refresh`, `status`, and `export` with useful examples.
9. Personal-library JSON requirements, a minimal example, repeat-import semantics, and a privacy warning.
10. Default database location, alternate `--database`, status inspection, FTS5 search examples, and exports.
11. Supported `STEAM_CATALOG_*` environment variables with exact defaults from `Settings`.
12. Troubleshooting for PowerShell execution policy, broken or stale `.venv`, missing `steam-catalog`, running outside the repository root during installation, sparse rows during discovery, retries, and long SteamSpy waits.
13. Development installation and verification commands.
14. Data-source limitations, lack of SteamDB usage, no paid SteamSpy requirement, and third-party API instability.
15. A short publication note that no license is currently supplied and a `LICENSE` should be chosen separately before inviting reuse.

## Launch Guidance

The primary instructions will use activation because it gives the shortest commands after setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
steam-catalog --help
```

The README will state that these commands must run from the directory containing `pyproject.toml`. It will include a no-activation fallback using `.\.venv\Scripts\python.exe` and `.\.venv\Scripts\steam-catalog.exe`, which also explains the role of `Activate.ps1`.

The recommended first catalog run is:

```powershell
steam-catalog run --library .\data\steam_info.json
```

The library option is explicitly optional. Later runs use `steam-catalog run`. Users who want visible partial progress sooner can use `steam-catalog run --page-limit 1`; users who interrupted discovery and want to process the existing queue can use `steam-catalog collect` while discovery is not running.

The README will warn against running bootstrap/run and collect concurrently against the same database.

## Accuracy And Safety

All command names, option placement, environment defaults, output semantics, and paths will be copied from the current CLI/configuration. The README will not claim that SteamDB is queried, that SteamSpy requires payment, that a Steam API key is required, or that every discovered placeholder has full metadata.

Personal data will be described as optional and local. The README will explicitly warn not to publish `steam_info.json` or a populated personal database without reviewing its contents.

## Git Ignore Changes

Extend `.gitignore` with:

```gitignore
.idea/
data/steam_info.json
*.sqlite3-wal
*.sqlite3-shm
```

The existing SQLite database exclusion remains. No current untracked personal/IDE/database files will be added, removed, or modified.

## Verification

Verification will include:

- compare every documented command and option with live `--help` output;
- compare every environment default with `src/steam_catalog/config.py`;
- scan the README for stale English operational sections and unsupported claims;
- run `git diff --check`;
- run the existing full pytest suite and Ruff because `.gitignore` and documentation do not alter production behavior but the repository should still close cleanly;
- confirm `git status` lists no personal artifacts as candidates after the new ignore rules.
