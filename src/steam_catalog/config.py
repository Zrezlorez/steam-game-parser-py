"""Configuration loaded from the process environment."""

import os
from dataclasses import dataclass
from pathlib import Path

from steam_catalog.errors import ConfigurationError

DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "data" / "steam_catalog.sqlite3"


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the Steam catalog collector."""

    database: Path
    request_timeout_seconds: float
    steam_interval_seconds: float
    steamspy_interval_seconds: float
    steamspy_all_interval_seconds: float
    max_attempts: int
    steam_web_api_key: str | None

    @classmethod
    def from_env(cls, database: Path | None = None) -> "Settings":
        """Build settings from ``STEAM_CATALOG_*`` environment variables."""
        selected_database = _resolve_database(database)
        request_timeout_seconds = _float_env("REQUEST_TIMEOUT_SECONDS", 30.0)
        steam_interval_seconds = _float_env("STEAM_INTERVAL_SECONDS", 1.0)
        steamspy_interval_seconds = _float_env("STEAMSPY_INTERVAL_SECONDS", 1.0)
        steamspy_all_interval_seconds = _float_env("STEAMSPY_ALL_INTERVAL_SECONDS", 60.0)
        max_attempts = _int_env("MAX_ATTEMPTS", 3)
        steam_web_api_key = _optional_env("STEAM_WEB_API_KEY")

        for name, value in {
            "request_timeout_seconds": request_timeout_seconds,
            "steam_interval_seconds": steam_interval_seconds,
            "steamspy_interval_seconds": steamspy_interval_seconds,
            "steamspy_all_interval_seconds": steamspy_all_interval_seconds,
            "max_attempts": max_attempts,
        }.items():
            if value <= 0:
                raise ConfigurationError(f"{name} must be positive")

        return cls(
            database=selected_database,
            request_timeout_seconds=request_timeout_seconds,
            steam_interval_seconds=steam_interval_seconds,
            steamspy_interval_seconds=steamspy_interval_seconds,
            steamspy_all_interval_seconds=steamspy_all_interval_seconds,
            max_attempts=max_attempts,
            steam_web_api_key=steam_web_api_key,
        )


def _resolve_database(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    environment_database = os.environ.get("STEAM_CATALOG_DATABASE")
    return Path(environment_database).resolve() if environment_database else DEFAULT_DATABASE


def _optional_env(name: str) -> str | None:
    value = os.environ.get(f"STEAM_CATALOG_{name}")
    return value.strip() if value is not None and value.strip() else None


def _float_env(name: str, default: float) -> float:
    return float(os.environ.get(f"STEAM_CATALOG_{name}", default))


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(f"STEAM_CATALOG_{name}", default))
