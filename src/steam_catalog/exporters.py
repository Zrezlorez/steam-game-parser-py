"""Streaming exports for eligible Steam catalog games."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import csv
import json
import os
from pathlib import Path
import tempfile
from typing import Any, TextIO

from steam_catalog.db import Database


CSV_COLUMNS = (
    "appid",
    "name",
    "release_date",
    "total_reviews",
    "owned",
    "playtime_forever_minutes",
    "playtime_2weeks_minutes",
    "last_played_at",
    "type",
    "is_released",
    "coming_soon",
    "review_score",
    "review_score_desc",
    "developers",
    "publishers",
    "languages",
    "platforms",
    "categories",
    "genres",
    "short_description",
    "detailed_description",
    "about_the_game",
    "header_image_url",
)

_JSON_COLUMNS = {
    "developers_json": "developers",
    "publishers_json": "publishers",
    "languages_json": "languages",
    "platforms_json": "platforms",
    "categories_json": "categories",
    "genres_json": "genres",
    "screenshots_json": "screenshots",
    "price_json": "price",
}
_GAME_BOOLEAN_COLUMNS = ("is_released", "coming_soon")
_REVIEW_BOOLEAN_COLUMNS = (
    "voted_up",
    "steam_purchase",
    "received_for_free",
    "written_during_early_access",
)


def export_jsonl(db: Database, output: Path | str, *, include_reviews: bool = False) -> int:
    """Write eligible games as one UTF-8 JSON object per line and atomically replace output."""
    count = 0
    with _atomic_text_output(Path(output), encoding="utf-8") as handle:
        for record in _game_records(db):
            if include_reviews:
                record["reviews"] = list(_review_records(db, record["appid"]))
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            count += 1
    return count


def export_csv(db: Database, output: Path | str) -> int:
    """Write eligible games with stable plain-text CSV columns and atomically replace output."""
    count = 0
    with _atomic_text_output(Path(output), encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="raise")
        writer.writeheader()
        for record in _game_records(db):
            writer.writerow(_csv_record(record))
            count += 1
    return count


def _game_records(db: Database) -> Iterator[dict[str, Any]]:
    cursor = db.connection.execute(
        """
        SELECT
            games.*,
            user_library.appid IS NOT NULL AS owned,
            user_library.name_at_import AS library_name_at_import,
            user_library.playtime_forever_minutes AS library_playtime_forever_minutes,
            user_library.playtime_2weeks_minutes AS library_playtime_2weeks_minutes,
            user_library.last_played_at AS library_last_played_at,
            user_library.platform_playtime_json AS library_platform_playtime_json,
            user_library.source_payload_json AS library_source_payload_json,
            user_library.imported_at AS library_imported_at
        FROM games
        LEFT JOIN user_library ON user_library.appid = games.appid
        WHERE games.eligibility_status = 'eligible'
        ORDER BY games.appid
        """
    )
    for row in cursor:
        yield _game_record(row)


def _game_record(row: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(row)
    owned = bool(record.pop("owned"))
    for database_key, export_key in _JSON_COLUMNS.items():
        record[export_key] = _decode_json(record.pop(database_key))
    for key in _GAME_BOOLEAN_COLUMNS:
        if record[key] is not None:
            record[key] = bool(record[key])

    record["owned"] = owned
    library = {
        "name_at_import": record.pop("library_name_at_import"),
        "playtime_forever_minutes": record.pop("library_playtime_forever_minutes"),
        "playtime_2weeks_minutes": record.pop("library_playtime_2weeks_minutes"),
        "last_played_at": record.pop("library_last_played_at"),
        "platform_playtimes": _decode_json(record.pop("library_platform_playtime_json")),
        "source_payload": _decode_json(record.pop("library_source_payload_json")),
        "imported_at": record.pop("library_imported_at"),
    }
    record["library"] = library if owned else None
    return record


def _review_records(db: Database, appid: int) -> Iterator[dict[str, Any]]:
    cursor = db.connection.execute(
        "SELECT * FROM reviews WHERE appid = ? ORDER BY recommendation_id", (appid,)
    )
    for row in cursor:
        record = dict(row)
        for key in _REVIEW_BOOLEAN_COLUMNS:
            if record[key] is not None:
                record[key] = bool(record[key])
        yield record


def _csv_record(record: Mapping[str, Any]) -> dict[str, Any]:
    library = record["library"]
    assert library is None or isinstance(library, dict)
    return {
        "appid": record["appid"],
        "name": record["name"],
        "release_date": record["release_date"],
        "total_reviews": record["steam_total_reviews"],
        "owned": int(record["owned"]),
        "playtime_forever_minutes": None
        if library is None
        else library["playtime_forever_minutes"],
        "playtime_2weeks_minutes": None if library is None else library["playtime_2weeks_minutes"],
        "last_played_at": None if library is None else library["last_played_at"],
        "type": record["type"],
        "is_released": _csv_boolean(record["is_released"]),
        "coming_soon": _csv_boolean(record["coming_soon"]),
        "review_score": record["review_score"],
        "review_score_desc": record["review_score_desc"],
        "developers": _plain_json(record["developers"]),
        "publishers": _plain_json(record["publishers"]),
        "languages": _plain_json(record["languages"]),
        "platforms": _plain_json(record["platforms"]),
        "categories": _plain_json(record["categories"]),
        "genres": _plain_json(record["genres"]),
        "short_description": record["short_description_text"],
        "detailed_description": record["detailed_description_text"],
        "about_the_game": record["about_the_game_text"],
        "header_image_url": record["header_image_url"],
    }


def _decode_json(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _csv_boolean(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _plain_json(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(key for key, item in value.items() if item)
    return str(value)


@contextmanager
def _atomic_text_output(output: Path, *, encoding: str) -> Iterator[TextIO]:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            yield handle
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
