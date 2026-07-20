"""Strict personal Steam library imports keyed by application ID."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from steam_catalog.db import Database


_PLATFORM_FIELDS = {
    "windows": "playtime_windows_forever",
    "mac": "playtime_mac_forever",
    "linux": "playtime_linux_forever",
    "deck": "playtime_deck_forever",
}


@dataclass(frozen=True)
class ImportStats:
    """Counts produced by one complete library import."""

    imported: int = 0
    placeholders_created: int = 0


@dataclass(frozen=True)
class _LibraryEntry:
    appid: int
    name_at_import: str
    playtime_forever_minutes: int
    playtime_2weeks_minutes: int | None
    last_played_at: str | None
    platform_playtime_json: str | None
    source_payload_json: str


def import_library(db: Database, path: Path) -> ImportStats:
    """Import a Steam library JSON array exactly once per appid in one transaction."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid library JSON: {error.msg}") from error
    if not isinstance(payload, list):
        raise ValueError("Library file must contain a top-level JSON array")

    entries = [_parse_entry(item, index) for index, item in enumerate(payload)]
    timestamp = datetime.now(UTC).isoformat()
    placeholders_created = 0
    with db.transaction():
        for entry in entries:
            if db.upsert_library_entry(
                appid=entry.appid,
                name_at_import=entry.name_at_import,
                playtime_forever_minutes=entry.playtime_forever_minutes,
                playtime_2weeks_minutes=entry.playtime_2weeks_minutes,
                last_played_at=entry.last_played_at,
                platform_playtime_json=entry.platform_playtime_json,
                source_payload_json=entry.source_payload_json,
                imported_at=timestamp,
            ):
                placeholders_created += 1
    return ImportStats(imported=len(entries), placeholders_created=placeholders_created)


def _parse_entry(value: object, index: int) -> _LibraryEntry:
    if not isinstance(value, dict):
        raise ValueError(f"Library entry {index} must be an object")
    appid = _positive_integer(value.get("appid"), "appid")
    name = value.get("name", "")
    if not isinstance(name, str):
        raise ValueError(f"Library entry {index} name must be a string")
    playtime_forever = _nonnegative_integer(value.get("playtime_forever", 0), "playtime_forever")
    playtime_2weeks = _optional_nonnegative_integer(value.get("playtime_2weeks"), "playtime_2weeks")
    last_played = _last_played_at(value.get("rtime_last_played"))
    platform_playtimes = {
        platform: _nonnegative_integer(value[field], field)
        for platform, field in _PLATFORM_FIELDS.items()
        if field in value and value[field] is not None
    }
    return _LibraryEntry(
        appid=appid,
        name_at_import=name,
        playtime_forever_minutes=playtime_forever,
        playtime_2weeks_minutes=playtime_2weeks,
        last_played_at=last_played,
        platform_playtime_json=json.dumps(
            platform_playtimes, ensure_ascii=False, separators=(",", ":")
        ),
        source_payload_json=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _optional_nonnegative_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value, field)


def _last_played_at(value: Any) -> str | None:
    if value in (None, 0):
        return None
    timestamp = _positive_integer(value, "rtime_last_played")
    return datetime.fromtimestamp(timestamp, UTC).isoformat()
