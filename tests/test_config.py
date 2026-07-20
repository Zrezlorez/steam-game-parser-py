from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from steam_catalog.config import Settings
from steam_catalog import pipeline


def test_settings_use_project_database_by_default(monkeypatch):
    monkeypatch.delenv("STEAM_CATALOG_DATABASE", raising=False)

    settings = Settings.from_env()

    assert settings.database.name == "steam_catalog.sqlite3"
    assert pipeline.MIN_REVIEWS == 20
    assert settings.steamspy_all_interval_seconds == 60.0


def test_default_database_is_independent_of_current_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("STEAM_CATALOG_DATABASE", raising=False)
    monkeypatch.chdir(tmp_path)

    settings = Settings.from_env()

    project_root = Path(__file__).resolve().parents[1]
    assert settings.database == (project_root / "data" / "steam_catalog.sqlite3").resolve()


def test_explicit_database_overrides_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("STEAM_CATALOG_DATABASE", str(tmp_path / "env.sqlite3"))
    explicit = tmp_path / "explicit.sqlite3"

    assert Settings.from_env(explicit).database == explicit.resolve()


def test_settings_parse_matching_environment_variables(monkeypatch, tmp_path: Path):
    database = tmp_path / "catalog.sqlite3"
    monkeypatch.setenv("STEAM_CATALOG_DATABASE", str(database))
    monkeypatch.setenv("STEAM_CATALOG_MIN_REVIEWS", "42")
    monkeypatch.setenv("STEAM_CATALOG_REQUEST_TIMEOUT_SECONDS", "15.5")
    monkeypatch.setenv("STEAM_CATALOG_STEAM_INTERVAL_SECONDS", "1.25")
    monkeypatch.setenv("STEAM_CATALOG_STEAMSPY_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("STEAM_CATALOG_STEAMSPY_ALL_INTERVAL_SECONDS", "75")
    monkeypatch.setenv("STEAM_CATALOG_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("STEAM_CATALOG_CACHE_TTL_HOURS", "72")

    settings = Settings.from_env()

    assert settings.database == database.resolve()
    assert not hasattr(settings, "min_reviews")
    assert settings.request_timeout_seconds == 15.5
    assert settings.steam_interval_seconds == 1.25
    assert settings.steamspy_interval_seconds == 2.5
    assert settings.steamspy_all_interval_seconds == 75.0
    assert settings.max_attempts == 4
    assert settings.cache_ttl_hours == 72


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("STEAM_CATALOG_REQUEST_TIMEOUT_SECONDS", "0"),
        ("STEAM_CATALOG_STEAM_INTERVAL_SECONDS", "-1"),
        ("STEAM_CATALOG_STEAMSPY_INTERVAL_SECONDS", "0"),
        ("STEAM_CATALOG_STEAMSPY_ALL_INTERVAL_SECONDS", "-0.5"),
        ("STEAM_CATALOG_MAX_ATTEMPTS", "0"),
    ],
)
def test_settings_reject_non_positive_intervals_and_attempts(
    monkeypatch, variable: str, value: str
):
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_env()


def test_settings_are_immutable():
    settings = Settings.from_env()

    with pytest.raises(FrozenInstanceError):
        settings.max_attempts = 10


def test_catalog_pipeline_does_not_accept_a_variable_review_threshold(initialized_db, monkeypatch):
    from steam_catalog.pipeline import CatalogPipeline

    monkeypatch.setenv("STEAM_CATALOG_MIN_REVIEWS", "42")

    with pytest.raises(TypeError, match="min_reviews"):
        CatalogPipeline(
            initialized_db,
            steam=object(),
            steamspy=object(),
            min_reviews=42,
        )
