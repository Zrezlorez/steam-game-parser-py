"""Shared, dependency-free parsing helpers for provider payloads."""

import re

from steam_catalog.errors import SourceResponseError

_BASE_10_INTEGER = re.compile(r"^[0-9]+$")


def optional_string(value: object) -> str | None:
    """Return ``value`` when it is a string, otherwise ``None``."""
    return value if isinstance(value, str) else None


def optional_bool(value: object) -> bool | None:
    """Return ``value`` when it is a boolean, otherwise ``None``."""
    return value if isinstance(value, bool) else None


def optional_integer(value: object) -> int | None:
    """Parse a non-boolean integer or a base-10 integer string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _BASE_10_INTEGER.fullmatch(value):
        return int(value)
    return None


def optional_float(value: object) -> float | None:
    """Parse a finite-looking numeric value without accepting booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def positive_appid(value: object) -> int | None:
    """Parse a positive application identifier from an integer or numeric string."""
    appid = optional_integer(value)
    return appid if appid is not None and appid > 0 else None


def required_nonnegative_integer(value: object, *, error_message: str) -> int:
    """Parse a strictly validated non-negative integer or raise a provider error."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise SourceResponseError(error_message)
