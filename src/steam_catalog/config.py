"""Configuration loaded from the process environment."""

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "data" / "steam_catalog.sqlite3"


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the Steam catalog collector."""

    database: Path
    min_reviews: int
    request_timeout_seconds: float
    steam_interval_seconds: float
    steamspy_interval_seconds: float
    steamspy_all_interval_seconds: float
    max_attempts: int
    cache_ttl_hours: int

    @classmethod
    def from_env(cls, database: Path | None = None) -> "Settings":
        """Build settings from ``STEAM_CATALOG_*`` environment variables."""
        environment_database = os.environ.get("STEAM_CATALOG_DATABASE")
        selected_database = (
            database
            if database is not None
            else Path(environment_database)
            if environment_database is not None
            else DEFAULT_DATABASE
        )
        request_timeout_seconds = _float_env("REQUEST_TIMEOUT_SECONDS", 30.0)
        steam_interval_seconds = _float_env("STEAM_INTERVAL_SECONDS", 1.0)
        steamspy_interval_seconds = _float_env("STEAMSPY_INTERVAL_SECONDS", 1.0)
        steamspy_all_interval_seconds = _float_env("STEAMSPY_ALL_INTERVAL_SECONDS", 60.0)
        max_attempts = _int_env("MAX_ATTEMPTS", 3)

        for name, value in {
            "request_timeout_seconds": request_timeout_seconds,
            "steam_interval_seconds": steam_interval_seconds,
            "steamspy_interval_seconds": steamspy_interval_seconds,
            "steamspy_all_interval_seconds": steamspy_all_interval_seconds,
            "max_attempts": max_attempts,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        return cls(
            database=selected_database.resolve(),
            min_reviews=_int_env("MIN_REVIEWS", 20),
            request_timeout_seconds=request_timeout_seconds,
            steam_interval_seconds=steam_interval_seconds,
            steamspy_interval_seconds=steamspy_interval_seconds,
            steamspy_all_interval_seconds=steamspy_all_interval_seconds,
            max_attempts=max_attempts,
            cache_ttl_hours=_int_env("CACHE_TTL_HOURS", 24),
        )


def _float_env(name: str, default: float) -> float:
    return float(os.environ.get(f"STEAM_CATALOG_{name}", default))


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(f"STEAM_CATALOG_{name}", default))
