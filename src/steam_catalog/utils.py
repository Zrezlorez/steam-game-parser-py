"""Small shared helpers used across the package."""

import json
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def compact_json(value: object) -> str:
    """Serialize a value as deterministic, compact, UTF-8-preserving JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode_json(value: str | None) -> Any:
    """Decode a JSON column value, preserving ``None`` as ``None``."""
    return None if value is None else json.loads(value)
