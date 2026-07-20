import json
from collections.abc import Callable
from pathlib import Path

import pytest

from steam_catalog.db import Database


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
