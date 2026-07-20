# Russian GitHub README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an accurate Russian Windows/PowerShell guide and prevent common local/private artifacts from being committed.

**Architecture:** Replace the root README with a user-first guide backed by the current Typer help, settings defaults, and SQLite workflow. Extend the existing root `.gitignore`; do not change Python code, dependencies, schemas, or runtime behavior.

**Tech Stack:** GitHub-flavored Markdown, PowerShell, Python 3.11+, Typer CLI, SQLite/FTS5, Git ignore rules.

## Global Constraints

- Documentation is Russian and targets Windows/PowerShell only.
- Literal commands, options, paths, environment variables, SQL, and statuses remain in English.
- Primary setup uses `.venv` activation and also provides a no-activation fallback.
- CLI and environment defaults must match the current source exactly.
- Do not modify or add personal library, IDE, database, WAL, or SHM files.
- Do not claim SteamDB usage, paid SteamSpy access, or a required Steam API key.
- Do not add a license; describe the need to choose one separately.

---

### Task 1: Repository Hygiene

**Files:**
- Modify: `.gitignore`
- Verify: `git status --short --untracked-files=all`

**Interfaces:**
- Consumes: existing Python, build, SQLite, and Superpowers exclusions
- Produces: exclusions for JetBrains metadata, personal library JSON, and SQLite sidecars

- [ ] **Step 1: Confirm the artifacts currently appear**

Run `git status --short --untracked-files=all`.

Expected before editing: `.idea/*`, `data/steam_info.json`, and SQLite `-wal`/`-shm` files are untracked.

- [ ] **Step 2: Append the exact rules**

```gitignore

# Local IDE, personal library, and SQLite runtime state
.idea/
data/steam_info.json
*.sqlite3-wal
*.sqlite3-shm
```

- [ ] **Step 3: Verify the rules**

Run:

```powershell
git check-ignore -v .idea/misc.xml data/steam_info.json data/steam_catalog.sqlite3-wal data/steam_catalog.sqlite3-shm
git status --short --untracked-files=all
```

Expected: all four path categories match the new rules and disappear from status.

---

### Task 2: Russian Windows README

**Files:**
- Modify: `README.md`
- Reference: `src/steam_catalog/config.py`
- Reference: `src/steam_catalog/cli.py`

**Interfaces:**
- Documents all eight CLI commands and global `--database`
- Documents all seven `STEAM_CATALOG_*` settings and current defaults
- Documents personal-library JSON, SQLite/FTS5, exports, retries, and source limits

- [ ] **Step 1: Replace the README using the approved headings**

Use these headings in order:

```markdown
# Steam Catalog
## Возможности
## Требования
## Установка
## Первый запуск
## Как устроен сбор данных
## Управление объёмом работы
## Команды
## Импорт личной библиотеки
## База данных и поиск
## Экспорт
## Настройка через переменные окружения
## Решение проблем
## Разработка
## Источники и ограничения
## Лицензия
```

Describe the product as a local searchable SQLite catalog of released Steam games with verified review totals, metadata/descriptions, sampled helpful/recent positive/negative reviews, optional ownership/playtime, and FTS5.

- [ ] **Step 2: Write exact installation instructions**

State that PowerShell is open in the directory containing `pyproject.toml`. Include:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
steam-catalog --help
```

Require Python 3.11+. Explain `Activate.ps1`, include process-scoped `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, and this no-activation fallback:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\steam-catalog.exe --help
```

- [ ] **Step 3: Document first run and durable workflow**

Include both optional-library forms:

```powershell
steam-catalog run
steam-catalog run --library .\data\steam_info.json
```

Explain that `Discover` creates sparse candidates/jobs and `Collect` verifies at least 20 reviews, release/type eligibility, metadata, descriptions, and review samples. Sparse rows before collection are expected.

Show representative durable output:

```text
Discovery page 4: fetching...
Discovery page 4: saved, discovered=1000, queued=734, total_discovered=5000, total_queued=4465, 1.2s
[42/3731] Example Game (appid 123456) - saved, reviews=20, 1.7s
```

Define `saved`, `skipped`, `retry`, and `failed`; explain commit ordering, exit 130 on `Ctrl+C`, resumability, and future retries.

- [ ] **Step 4: Document workload modes and every command**

Include:

```powershell
steam-catalog run
steam-catalog run --page-limit 1
steam-catalog collect
steam-catalog collect --limit 100
steam-catalog collect --appid 4000
steam-catalog bootstrap --source steam
steam-catalog refresh --older-than-days 30 --failed
steam-catalog status
steam-catalog --database .\data\alternate.sqlite3 status
```

Provide a concise table for `run`, `init`, `import-library`, `bootstrap`, `collect`, `refresh`, `status`, and `export`. Warn against concurrent discovery and collection on one database.

- [ ] **Step 5: Document personal data, search, export, and settings**

Use this minimal valid JSON:

```json
[
  {
    "appid": 4000,
    "name": "Garry's Mod",
    "playtime_forever": 1200,
    "playtime_2weeks": 60,
    "rtime_last_played": 1767225600
  }
]
```

Explain repeat-import semantics and privacy. Document default `data\steam_catalog.sqlite3`, the existing FTS5 game/review queries, JSONL/CSV export examples, and exact defaults:

| Variable | Default |
| --- | ---: |
| `STEAM_CATALOG_DATABASE` | `data\steam_catalog.sqlite3` |
| `STEAM_CATALOG_REQUEST_TIMEOUT_SECONDS` | `30` |
| `STEAM_CATALOG_STEAM_INTERVAL_SECONDS` | `1` |
| `STEAM_CATALOG_STEAMSPY_INTERVAL_SECONDS` | `1` |
| `STEAM_CATALOG_STEAMSPY_ALL_INTERVAL_SECONDS` | `60` |
| `STEAM_CATALOG_MAX_ATTEMPTS` | `3` |
| `STEAM_CATALOG_CACHE_TTL_HOURS` | `24` |

Show process-scoped PowerShell assignment and warn against reducing source intervals.

- [ ] **Step 6: Add troubleshooting, development, limits, and license status**

Give exact remedies for: missing/old Python, blocked `Activate.ps1`, stale `.venv`, missing module/command, sparse discovered rows, future retries, and slow SteamSpy pages.

Development commands:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests
```

State that SteamSpy is free but rate-limited, Steam Store interfaces can change, SteamDB is not used, no Steam API key is currently required, and no `LICENSE` is included.

- [ ] **Step 7: Verify documentation and repository**

Run live help for the root CLI plus `run`, `bootstrap`, `collect`, `refresh`, and `export`. Then run:

```powershell
python -m pytest
python -m ruff check src tests
git diff --check
git status --short --untracked-files=all
```

Expected: commands/options and settings match the README; tests pass; Ruff/diff are clean; personal artifacts are ignored; only `README.md` and `.gitignore` are intended tracked changes.

- [ ] **Step 8: Commit**

```powershell
git add README.md .gitignore
git commit -m "docs: add Russian Windows guide"
```
