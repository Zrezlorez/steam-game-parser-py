import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from steam_catalog.config import Settings
from steam_catalog.db import Database
from steam_catalog.http import CachedHttpClient


@pytest.fixture
def load_fixture() -> Callable[[str], object]:
    def _load(name: str) -> object:
        path = Path(__file__).parent / "fixtures" / name
        return json.loads(path.read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def initialized_db(tmp_path: Path):
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def http_client_factory(initialized_db: Database):
    """Create HTTP clients backed by the temporary database."""
    clients: list[CachedHttpClient] = []

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        **settings_overrides: object,
    ) -> CachedHttpClient:
        settings = replace(
            Settings.from_env(initialized_db.path),
            **settings_overrides,
        )
        client = CachedHttpClient(
            database=initialized_db,
            settings=settings,
            transport=httpx.MockTransport(handler),
            sleep=sleep,
            clock=clock or (lambda: datetime(2026, 7, 20, tzinfo=UTC)),
        )
        clients.append(client)
        return client

    try:
        yield _factory
    finally:
        for client in clients:
            client.close()
