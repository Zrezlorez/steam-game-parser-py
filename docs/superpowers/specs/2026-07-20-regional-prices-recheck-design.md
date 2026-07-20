# Regional Prices and Reason-Based Recheck Design

## Goal

Make Steam collection deterministic for the Kazakhstan storefront, store prices in tenge, US dollars, and hryvnias, make skipped records easy to requeue by reason, and remove the ambiguity between Steam's total review count and locally stored review samples.

## Confirmed behavior

- Kazakhstan (`KZ`) is the only storefront used to decide whether an application is available and to supply its non-price Steam Store metadata.
- Prices are collected for `KZ`, `US`, and `UA`, producing KZT, USD, and UAH entries respectively.
- The existing singular `price` representation is removed completely. Existing regionally ambiguous price data is not migrated.
- The existing `refresh` command gains a reason selector; no separate recheck command is added.
- Collection progress reports both Steam's aggregate review count and the number of unique review samples stored locally.

## Steam requests and availability

`SteamClient` requests app details with an explicit `cc` query parameter. The KZ request is authoritative:

1. Fetch `appdetails?appids=<appid>&cc=KZ`.
2. If KZ returns `success=false`, record the existing `ineligible` status and `unavailable` skip reason. Do not use US or UA to override that result.
3. If KZ succeeds, parse all ordinary game metadata and its KZT price.
4. Fetch the US and UA variants and extract only their price information.
5. Continue the existing type and release checks against KZ metadata.

The HTTP cache key already contains normalized query parameters, so each country response has a separate cache entry. Explicit country selection makes behavior independent of whether the collector runs in Russia or on a foreign VPS.

A valid `success=false` response for US or UA means that currency has no price and is stored as `null`; it does not make a KZ-available game unavailable. Network failures, malformed payloads, and other expected collection errors retain the existing durable retry behavior rather than silently producing incomplete price data.

For free games, each successfully queried storefront is represented by an explicit zero-valued price object even when Steam omits `price_overview`. The `is_free` value from each storefront's own successful response determines whether its missing price is a zero price; US and UA `success=false` remain `null`.

## Price representation and migration

The `games.price_json` column and the `GameDetails.price_json` field are removed. They are replaced by `games.prices_json` and `GameDetails.prices_json`.

The stored JSON object uses currency codes as stable keys:

```json
{
  "KZT": {"currency": "KZT", "initial": 7000, "final": 3500, "discount_percent": 50},
  "USD": {"currency": "USD", "initial": 1999, "final": 999, "discount_percent": 50},
  "UAH": null
}
```

Monetary integers retain Steam's smallest-unit representation. Each of `KZT`, `USD`, and `UAH` is always present; a missing regional offer is represented by `null`. Price objects preserve the Steam fields already accepted from `price_overview`, including any display strings supplied by Steam. A successful storefront response whose `price_overview.currency` does not match the requested currency is treated as malformed and follows the retry policy instead of being stored under a false key.

Database initialization migrates an existing catalog by adding `prices_json` and removing `price_json`, using a table rebuild only when required by the available SQLite migration operations. The old values are intentionally discarded because their requests did not identify a storefront. Foreign keys, indexes, FTS synchronization, and all unrelated game fields must survive the migration. Fresh databases are created with only `prices_json`.

JSONL exports expose decoded data as `prices`. CSV replaces the old `price` column with a `prices` column containing compact JSON. There is no deprecated `price` alias.

## Reason-based refresh

The existing command supports:

```powershell
steam-catalog refresh --reason unavailable
```

`--reason` accepts any non-empty string and immediately schedules every game whose `skip_reason` matches it exactly. A typo or unknown value safely produces `Scheduled: 0`, which also keeps the command forward-compatible with future reasons.

When `--reason` is present, the age cutoff does not apply to reason-matched records. Without `--reason`, `refresh --older-than-days N` retains its current behavior. `--failed` retains its existing meaning and may additionally requeue permanent failures in either mode. Existing running jobs are not stolen or reset.

The database selection and requeue operation remain transactional and reset the selected jobs to the same clean pending state used by the current refresh implementation.

## Review progress output

The current `reviews=N` progress value comes from the count of unique rows stored in the local `reviews` table. It is a bounded sample count and can be 17 when overlapping sample categories deduplicate, even if Steam reports thousands of reviews.

After a successful collection, progress displays both values:

```text
[42/3731] Life is Strange™ (appid 319630) - saved, reviews=124176, samples=17, 1.7s
```

`reviews` is `games.steam_total_reviews`; `samples` is the local stored-review count. Skipped, retry, and failed lines keep their existing concise format unless aggregate data is already part of their current output contract.

## Error handling and durable state

- KZ `success=false` is a durable `unavailable` skip, not a retryable transport error.
- US or UA `success=false` produces a `null` price for that currency.
- HTTP errors and malformed successful responses follow the existing bounded retry and permanent-failure policy.
- Price and metadata updates commit atomically with eligibility, aggregate review data, samples, and job completion.
- Rechecking an unavailable game replaces its stale status and price data only after a successful collection transaction.

## Testing and verification

Automated tests cover:

- explicit `cc=KZ`, `cc=US`, and `cc=UA` app-details requests with distinct cache keys;
- KZ as the sole authority for availability and metadata;
- successful three-currency parsing, a regionally missing price, free-game zero prices, and malformed response handling;
- schema creation and migration with `price_json` absent and `prices_json` present while unrelated data, indexes, and FTS behavior remain intact;
- JSONL and CSV exports containing `prices` and no `price` field;
- `refresh --reason unavailable`, unknown reasons, interaction with `--failed`, protection of running jobs, and unchanged age-based refresh behavior;
- progress output containing the Steam total and deduplicated sample count with distinct labels;
- regression coverage for existing eligibility, retry, interruption, and collection behavior.

Final verification runs focused tests during development, the complete pytest suite, Ruff formatting/lint checks, and a mocked Life Is Strange scenario for appid `319630`. A live KZ smoke test is optional because network access and Steam responses are external; locally, the existing database record can be requeued with `refresh --reason unavailable` after deployment.

## Out of scope

- Using VPS location or automatic IP geolocation as storefront selection.
- Treating Russian-language support as an eligibility filter.
- Currency conversion from a single regional price.
- Historical price tracking.
- A new standalone recheck command.
- Migrating the old regionally ambiguous `price_json` values.
