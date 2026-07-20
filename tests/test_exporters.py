import csv
import json

import pytest

from steam_catalog.exporters import export_csv, export_jsonl


@pytest.fixture
def populated_db(initialized_db):
    for appid in (20, 10):
        initialized_db.upsert_game(
            appid=appid,
            name=f"Game {appid}" if appid == 10 else "Aether",
            eligibility_status="eligible",
            updated_at="2026-07-20T00:00:00+00:00",
            release_date=f"202{appid // 10}-01-02",
            short_description_html="<p>Raw short description</p>",
            short_description_text="Plain short description",
            detailed_description_html="<p>Raw detailed description</p>",
            detailed_description_text="Plain detailed description",
            about_the_game_html="<p>Raw about description</p>",
            about_the_game_text="Plain about description",
            developers_json=json.dumps([f"Studio {appid}"], ensure_ascii=False),
            platforms_json=json.dumps({"windows": True}),
            prices_json=json.dumps({"KZT": {"currency": "KZT", "final": appid}}),
            steam_total_reviews=appid + 5,
        )
    initialized_db.upsert_library_entry(
        appid=10,
        name_at_import="Game 10",
        playtime_forever_minutes=123,
        playtime_2weeks_minutes=5,
        last_played_at="2023-11-14T22:13:20+00:00",
        platform_playtime_json='{"windows":120,"linux":3}',
        source_payload_json='{"appid":10,"name":"Game 10"}',
        imported_at="2026-07-20T00:00:00+00:00",
    )
    initialized_db.upsert_review(
        recommendation_id="review-10",
        appid=10,
        review_text="Excellent game",
        voted_up=True,
        fetched_at="2026-07-20T00:00:00+00:00",
    )
    return initialized_db


def test_jsonl_export_is_one_valid_object_per_game(populated_db, tmp_path):
    output = tmp_path / "games.jsonl"

    assert export_jsonl(populated_db, output) == 2

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["appid"] for row in rows] == [10, 20]
    assert rows[0]["owned"] is True
    assert rows[1]["owned"] is False
    assert "reviews" not in rows[0]
    assert rows[0]["short_description_html"] == "<p>Raw short description</p>"
    assert rows[0]["short_description_text"] == "Plain short description"


def test_jsonl_export_decodes_metadata_and_includes_reviews_only_when_requested(
    populated_db, tmp_path
):
    output = tmp_path / "games.jsonl"

    export_jsonl(populated_db, output, include_reviews=True)

    first = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert first["developers"] == ["Studio 10"]
    assert first["platforms"] == {"windows": True}
    assert first["prices"] == {"KZT": {"currency": "KZT", "final": 10}}
    assert "price" not in first
    assert first["library"]["platform_playtimes"] == {"windows": 120, "linux": 3}
    assert first["reviews"][0]["recommendation_id"] == "review-10"
    assert first["reviews"][0]["review_text"] == "Excellent game"
    assert first["reviews"][0]["voted_up"] is True


def test_csv_export_uses_plain_text_and_stable_columns(populated_db, tmp_path):
    output = tmp_path / "games.csv"

    assert export_csv(populated_db, output) == 2

    text = output.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("appid,name,release_date,total_reviews,owned")
    rows = list(csv.DictReader(text.splitlines()))
    assert [row["appid"] for row in rows] == ["10", "20"]
    assert rows[0]["owned"] == "1"
    assert rows[1]["owned"] == "0"
    assert rows[0]["short_description"] == "Plain short description"
    assert json.loads(rows[0]["prices"]) == {"KZT": {"currency": "KZT", "final": 10}}
    assert "<p>" not in text


def test_export_keeps_existing_target_when_atomic_replace_fails(
    populated_db, tmp_path, monkeypatch
):
    output = tmp_path / "games.jsonl"
    output.write_text("previous export\n", encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr("steam_catalog.exporters.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        export_jsonl(populated_db, output)

    assert output.read_text(encoding="utf-8") == "previous export\n"
    assert not list(tmp_path.glob(".games.jsonl.*.tmp"))
