# Two-Pass Collection Design

## Goal

Reduce the normal per-game collection time by removing one redundant Steam review request and moving USD and UAH price requests into a separate resumable command.

## Primary collection

The existing `collect` and `run` commands remain the primary catalog-building path. For each candidate they:

1. Fetch one broad recent review page with up to 100 reviews. Its `query_summary` is the authoritative aggregate used for the 20-review threshold and stored Steam review totals.
2. Reject games below the verified threshold without making Store metadata requests.
3. Fetch Kazakhstan (`cc=KZ`) Store metadata and the KZT offer only.
4. Apply the existing availability, application type, and release checks.
5. Fetch the broad helpful review page and only the targeted polarity pages needed to fill sample shortfalls.
6. Store metadata, the KZT price, aggregate review counts, and review samples atomically.

The previous preliminary recent-review request with `num_per_page=1` is removed. The broad recent page is reused both as the aggregate source and as the recent sample source. This saves one Steam request for every candidate while preserving the existing review threshold and sample semantics.

`prices_json` contains only a `KZT` key after primary collection. A present key with a `null` value means that storefront was queried successfully but had no offer. An absent `USD` or `UAH` key means that storefront has not yet been collected. This distinction is part of the persisted data contract and export behavior.

## Regional price collection

A new command performs the optional second pass:

```powershell
steam-catalog collect-prices
steam-catalog collect-prices --limit 100
steam-catalog collect-prices --appid 22380
```

The bulk command schedules and consumes regional-price jobs only for eligible games whose USD/UAH pass has not completed. It does not process ineligible, pending, or provisionally rejected records. `--limit` limits work completed by the current invocation. Re-running the command resumes incomplete work without re-fetching completed games.

`--appid` targets one eligible game and forces a fresh USD/UAH collection even when a completed regional-price timestamp exists. It refuses non-eligible or unknown applications with a concise error instead of changing their catalog status.

Each regional-price job requests `appdetails` with `cc=US` and `cc=UA`, extracts only the expected USD and UAH offers, merges them with the existing KZT entry, and records `regional_prices_fetched_at`. A valid regional `success=false` result is stored as `null` for that currency. Malformed payloads, HTTP failures, and rate-limit exhaustion use the existing retry and permanent-failure behavior.

Regional price updates never replace game metadata, eligibility, review aggregates, or review samples. USD and UAH are committed together only after both requests succeed, so an interrupted job cannot leave a falsely completed half-pass.

## Durable state and migration

The `games` table gains a nullable `regional_prices_fetched_at` column. Existing databases receive the column through the current additive migration mechanism. Existing rows keep `NULL`, causing eligible games to be scheduled for their first regional-price pass.

The existing `sync_jobs` table is reused with a distinct regional-price job kind. Queue claims, leases, retry counts, delayed retry, permanent failure, interruption recovery, and resume behavior follow the same rules as collection jobs. The two job kinds do not overwrite or steal one another.

Primary collection clears `regional_prices_fetched_at` because refreshed KZ metadata starts a new price snapshot and any old USD/UAH values are removed. A subsequent `collect-prices` creates one internally consistent three-region snapshot.

## Console output

`collect-prices` prints durable per-game outcomes using the established progress style, followed by saved, retry, failed, and elapsed totals. Successful lines identify that regional prices were saved; retry and failure lines include the error. Only committed outcomes are reported as saved.

`run` does not automatically invoke `collect-prices`. This keeps the fast catalog-building path separate, as requested.

## Testing

Automated tests cover:

- reuse of the initial broad recent page for both aggregate counts and recent samples;
- removal of the redundant one-review request;
- KZ-only primary Store requests and KZT-only primary price JSON;
- USD/UAH parsing and merging without metadata changes;
- the distinction between absent uncollected currencies and present `null` unavailable currencies;
- migration of existing databases with a nullable regional timestamp;
- scheduling only eligible incomplete games;
- `--limit`, forced `--appid`, retries, permanent failures, interruption, and resume behavior;
- unchanged primary collection, exports, and existing refresh behavior.

Final verification runs focused tests, the complete pytest suite, and Ruff formatting and lint checks. Network calls remain mocked so verification is deterministic.

## Out of scope

- Steam Web API keys, which do not accelerate the Store metadata or review endpoints used here.
- Automatic execution of the price pass from `run`.
- Historical price tracking or price-change alerts.
- Parallel Steam requests or lower rate-limit intervals.
- A normalized regional-price history table.
