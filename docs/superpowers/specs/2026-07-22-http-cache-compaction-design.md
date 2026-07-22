# HTTP cache removal and database compaction

## Goal

Reduce the catalog database from roughly 4.6 GB to roughly 0.9–1.0 GB by stopping persistence of raw HTTP response bodies and providing one explicit maintenance command for existing databases.

## HTTP behavior

`CachedHttpClient.get_json()` will continue to normalize requests, apply rate limits, retry transient failures, validate status codes, and decode JSON. It will no longer read from or write to `http_cache`. Each request response will exist only in memory for the duration of parsing.

The `http_cache` table remains in the schema for compatibility with existing databases and migrations, but normal collection will no longer populate it.

## `compact` command

Add the command:

```bash
steam-catalog compact
```

The command will:

1. Open and initialize the configured database.
2. Record its current file size.
3. Delete every row from `http_cache` in a committed transaction.
4. Truncate the WAL with `PRAGMA wal_checkpoint(TRUNCATE)`.
5. Reclaim unused pages with `VACUUM`.
6. Print the database path, number of removed cache rows, and sizes before and after compaction.

SQLite locking or insufficient-disk-space errors will use the CLI's existing error handling. The user must not run `compact` concurrently with collection commands.

## Compatibility and scope

- Existing catalog data, normalized reviews, search indexes, source items, and job state are retained.
- No schema migration or table removal is required.
- No cache configuration replacement or compression layer is introduced.
- Repeated HTTP requests may perform additional network calls because responses are no longer cached.

## Verification

Targeted checks will cover that HTTP requests do not populate `http_cache`, `compact` deletes existing cache rows without deleting catalog data, and the CLI exposes the new command. A small temporary database will be used for compaction verification rather than the multi-gigabyte production database.
