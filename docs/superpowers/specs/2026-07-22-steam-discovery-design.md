# Official Steam discovery

## Goal

Restore `steam-catalog bootstrap --source steam --resume` so it discovers games missing from SteamSpy through the current official Steam Web API. Preserve the existing direct-Steam refresh path for games previously rejected by the verified review threshold.

## Configuration

Add the optional environment setting:

```text
STEAM_CATALOG_STEAM_WEB_API_KEY
```

The key is required only when discovery source `steam` is selected. Other commands and SteamSpy discovery continue to work without it. A missing or blank key must produce a concise error before an HTTP request is made.

## Official discovery API

Replace the removed `ISteamApps/GetAppList/v2` endpoint with:

```text
https://api.steampowered.com/IStoreService/GetAppList/v1/
```

Requests include the API key, `include_games=true`, `max_results`, and `last_appid` after the first page. The client follows `response.have_more_results` and `response.last_appid` until completion. It rejects malformed responses, non-advancing cursors, and a continuation response without a valid `last_appid`.

Only entries with a positive integer `appid` are emitted. Names and compact source payloads are preserved. The official list has no review count, so these candidates bypass SteamSpy's provisional review threshold and are verified later by `collect` through the Steam Reviews API.

## Pipeline behavior

Official discovery yields one API page at a time. The pipeline commits each page separately, records a sequential `source_page`, and reports normal bootstrap progress. This avoids holding one SQLite transaction while additional network pages are fetched.

`--resume` retains existing job state. Previously completed or rejected appids are not automatically requeued; newly discovered appids receive normal `collect` jobs.

## Review-threshold refresh

The existing commands remain the explicit way to recheck every game rejected after direct Steam review verification:

```bash
steam-catalog refresh --reason below_verified_threshold
steam-catalog collect
```

`refresh` resets matching non-running collection jobs, and `collect` obtains the current review aggregate directly from Steam. This behavior will receive explicit regression coverage and README documentation. It will not be folded into `bootstrap`.

## Compatibility and errors

- SteamSpy remains the default discovery source.
- No database migration is required.
- No third-party app-list provider is introduced.
- HTTP retries and rate limiting continue through the existing HTTP client.
- A 401 or 403 response from Steam uses the CLI's existing concise HTTP error handling.

## Verification

Tests will cover configuration loading, missing-key failure, multi-page parsing, cursor validation, per-page pipeline commits/progress, discovery of an app absent from SteamSpy, and direct-Steam requeue of `below_verified_threshold`. Verification uses mocked HTTP responses and temporary databases; no real API key is stored or used by tests.
