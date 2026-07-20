import json

import pytest

from steam_catalog.importers.library import import_library


def write_library(tmp_path, appid: int, playtime: int):
    source = tmp_path / "steam_info.json"
    source.write_text(
        json.dumps(
            [
                {
                    "appid": appid,
                    "name": f"Game {appid}",
                    "playtime_forever": playtime,
                }
            ]
        ),
        encoding="utf-8",
    )
    return source


def test_import_joins_by_appid_and_creates_placeholder(tmp_path, initialized_db):
    source = tmp_path / "steam_info.json"
    source.write_text(
        json.dumps(
            [
                {
                    "appid": 730,
                    "name": "Counter-Strike 2",
                    "playtime_forever": 123,
                    "rtime_last_played": 1_700_000_000,
                    "playtime_windows_forever": 120,
                }
            ]
        ),
        encoding="utf-8",
    )

    stats = import_library(initialized_db, source)

    row = initialized_db.connection.execute(
        "SELECT * FROM user_library WHERE appid = 730"
    ).fetchone()
    assert stats.imported == 1
    assert row["playtime_forever_minutes"] == 123
    assert (
        initialized_db.connection.execute("SELECT name FROM games WHERE appid = 730").fetchone()[0]
        == "Counter-Strike 2"
    )


def test_reimport_updates_activity_without_duplicate_rows(tmp_path, initialized_db):
    source = write_library(tmp_path, appid=10, playtime=5)
    import_library(initialized_db, source)
    source = write_library(tmp_path, appid=10, playtime=9)

    import_library(initialized_db, source)

    assert initialized_db.connection.execute("SELECT COUNT(*) FROM user_library").fetchone()[0] == 1
    assert (
        initialized_db.connection.execute(
            "SELECT playtime_forever_minutes FROM user_library WHERE appid = 10"
        ).fetchone()[0]
        == 9
    )
    assert (
        initialized_db.connection.execute(
            "SELECT platform_playtime_json FROM user_library WHERE appid = 10"
        ).fetchone()[0]
        == "{}"
    )


def test_import_preserves_authoritative_name_and_source_fields(tmp_path, initialized_db):
    initialized_db.upsert_game(
        appid=10,
        name="Authoritative catalog name",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    source = tmp_path / "steam_info.json"
    payload = {
        "appid": 10,
        "name": "Different library name",
        "playtime_forever": 42,
        "playtime_2weeks": 3,
        "rtime_last_played": 1_700_000_000,
        "playtime_windows_forever": 40,
        "playtime_linux_forever": 2,
    }
    source.write_text(json.dumps([payload]), encoding="utf-8")

    import_library(initialized_db, source)

    game = initialized_db.connection.execute("SELECT name FROM games WHERE appid = 10").fetchone()
    library = initialized_db.connection.execute(
        "SELECT * FROM user_library WHERE appid = 10"
    ).fetchone()
    assert game["name"] == "Authoritative catalog name"
    assert library["name_at_import"] == "Different library name"
    assert library["last_played_at"] == "2023-11-14T22:13:20+00:00"
    assert json.loads(library["platform_playtime_json"]) == {"linux": 2, "windows": 40}
    assert library["source_payload_json"] == json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


@pytest.mark.parametrize("payload", [{}, {"appid": 0}, {"appid": True}, {"appid": "10"}])
def test_import_rejects_invalid_appids_without_writing_rows(tmp_path, initialized_db, payload):
    source = tmp_path / "steam_info.json"
    source.write_text(json.dumps([{"appid": 10, "name": "Valid"}, payload]), encoding="utf-8")

    with pytest.raises(ValueError, match="appid"):
        import_library(initialized_db, source)

    assert initialized_db.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    assert initialized_db.connection.execute("SELECT COUNT(*) FROM user_library").fetchone()[0] == 0


def test_import_requires_a_top_level_array(tmp_path, initialized_db):
    source = tmp_path / "steam_info.json"
    source.write_text('{"appid": 10}', encoding="utf-8")

    with pytest.raises(ValueError, match="top-level JSON array"):
        import_library(initialized_db, source)
