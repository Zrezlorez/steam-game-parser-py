from dataclasses import replace
import sqlite3

import pytest

from steam_catalog.models import Review


class GameDatabaseFixture:
    def __init__(self, database) -> None:
        self.database = database

    def __getattr__(self, name):
        return getattr(self.database, name)

    def add_game(
        self,
        *,
        appid: int,
        name: str,
        detailed_description_text: str = "",
        short_description_text: str = "",
    ) -> None:
        self.database.upsert_game(
            appid=appid,
            name=name,
            eligibility_status="eligible",
            updated_at="2026-07-20T00:00:00Z",
            detailed_description_text=detailed_description_text,
            short_description_text=short_description_text,
        )


class ReviewDatabaseFixture(GameDatabaseFixture):
    def add_review(
        self,
        *,
        appid: int,
        recommendation_id: str,
        text: str,
        sample_kinds: tuple[str, ...] = ("helpful_negative",),
    ) -> None:
        self.add_game(appid=appid, name=f"Game {appid}")
        review = Review(
            appid=appid,
            recommendation_id=recommendation_id,
            review_text=text,
            voted_up=False,
        )
        samples = {sample_kind: [replace(review)] for sample_kind in sample_kinds}
        self.database.replace_review_samples(
            appid,
            samples,
            fetched_at="2026-07-20T00:01:00Z",
        )


@pytest.fixture
def db_with_game(initialized_db):
    return GameDatabaseFixture(initialized_db)


@pytest.fixture
def db_with_review(initialized_db):
    return ReviewDatabaseFixture(initialized_db)


def test_game_search_finds_detailed_description(db_with_game):
    db_with_game.add_game(
        appid=10,
        name="Miner",
        detailed_description_text="Build drills and conveyors",
    )

    results = db_with_game.search_games("conveyors", 10)

    assert [row["appid"] for row in results] == [10]
    assert results[0]["name"] == "Miner"
    assert results[0]["rank"] <= 0
    assert results[0]["snippet"] == "Build drills and <mark>conveyors</mark>"


def test_game_search_refresh_removes_old_text(db_with_game):
    db_with_game.add_game(
        appid=10,
        name="Miner",
        detailed_description_text="Build temporary conveyors",
    )
    db_with_game.add_game(
        appid=10,
        name="Miner",
        detailed_description_text="Build permanent railways",
    )

    assert db_with_game.search_games("conveyors", 10) == []
    assert [row["appid"] for row in db_with_game.search_games("railways", 10)] == [10]


def test_game_search_orders_by_bm25_rank(db_with_game):
    db_with_game.add_game(
        appid=10,
        name="Conveyors Architect",
        detailed_description_text="Build conveyors",
    )
    db_with_game.add_game(
        appid=20,
        name="Miner",
        detailed_description_text="Build conveyors",
    )

    results = db_with_game.search_games("conveyors", 10)

    assert [row["appid"] for row in results] == [10, 20]
    assert results[0]["rank"] < results[1]["rank"]


def test_game_search_snippet_uses_whole_token_match(db_with_game):
    db_with_game.add_game(
        appid=10,
        name="Drillmaster",
        detailed_description_text="Learn to drill deep underground",
    )

    result = db_with_game.search_games("drill", 10)

    assert result[0]["snippet"] == "Learn to <mark>drill</mark> deep underground"


def test_game_search_highlights_diacritic_folded_text_in_matching_field(db_with_game):
    db_with_game.add_game(
        appid=10,
        name="Career Builder",
        short_description_text="Manage schedules",
        detailed_description_text="Polish your résumé for every application",
    )

    result = db_with_game.search_games("resume", 10)

    assert result[0]["snippet"] == "Polish your <mark>résumé</mark> for every application"


def test_review_search_returns_game_and_sample_context(db_with_review):
    db_with_review.add_review(
        appid=10,
        recommendation_id="r1",
        text="late game becomes grindy",
        sample_kinds=("helpful_negative", "recent_negative"),
    )

    result = db_with_review.search_reviews("grindy", 10)

    assert result[0]["appid"] == 10
    assert result[0]["name"] == "Game 10"
    assert result[0]["recommendation_id"] == "r1"
    assert result[0]["snippet"] == "late game becomes <mark>grindy</mark>"
    assert result[0]["samples"] == [
        {"sample_kind": "helpful_negative", "sample_rank": 1},
        {"sample_kind": "recent_negative", "sample_rank": 1},
    ]


def test_review_search_refresh_removes_old_text(db_with_review):
    db_with_review.add_review(appid=10, recommendation_id="r1", text="initially grindy")
    db_with_review.add_review(appid=10, recommendation_id="r1", text="now finely balanced")

    assert db_with_review.search_reviews("grindy", 10) == []
    assert [row["recommendation_id"] for row in db_with_review.search_reviews("balanced", 10)] == [
        "r1"
    ]


def test_review_search_highlights_diacritic_folded_original_text(db_with_review):
    db_with_review.add_review(
        appid=10,
        recommendation_id="r1",
        text="A thoughtful RÉSUMÉ of the entire campaign",
    )

    result = db_with_review.search_reviews("resume", 10)

    assert result[0]["snippet"] == "A thoughtful <mark>RÉSUMÉ</mark> of the entire campaign"


def test_review_search_batches_sample_context_for_ranked_rows(db_with_review):
    db_with_review.add_review(
        appid=10,
        recommendation_id="r1",
        text="Shared search phrase one",
        sample_kinds=("helpful_negative", "recent_negative"),
    )
    db_with_review.add_review(
        appid=20,
        recommendation_id="r2",
        text="Shared search phrase two",
        sample_kinds=("helpful_negative",),
    )
    statements = []
    db_with_review.connection.set_trace_callback(statements.append)
    try:
        assert db_with_review.search_reviews("absent", 10) == []
        assert not [statement for statement in statements if "FROM review_samples" in statement]
        statements.clear()

        results = db_with_review.search_reviews("shared", 10)
    finally:
        db_with_review.connection.set_trace_callback(None)

    membership_queries = [
        statement for statement in statements if "FROM review_samples" in statement
    ]
    assert len(membership_queries) == 1
    assert {row["recommendation_id"]: row["samples"] for row in results} == {
        "r1": [
            {"sample_kind": "helpful_negative", "sample_rank": 1},
            {"sample_kind": "recent_negative", "sample_rank": 1},
        ],
        "r2": [{"sample_kind": "helpful_negative", "sample_rank": 1}],
    }


def test_review_search_chunks_sample_context_at_sqlite_parameter_limit(db_with_review):
    for appid in range(10, 13):
        db_with_review.add_review(
            appid=appid,
            recommendation_id=f"r{appid}",
            text=f"Bounded query review {appid}",
        )
    previous_limit = db_with_review.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
    try:
        results = db_with_review.search_reviews("bounded", 3)
    finally:
        db_with_review.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit)

    assert [row["recommendation_id"] for row in results] == ["r10", "r11", "r12"]
    assert all(row["samples"] for row in results)


@pytest.mark.parametrize("query", ["", "   ", "_", "!@#$%^&*()", '" OR name:* -'])
def test_search_treats_empty_or_operator_input_as_safe_tokens(db_with_game, query):
    db_with_game.add_game(appid=10, name="Miner", detailed_description_text="Build drills")

    assert db_with_game.search_games(query, 10) == []


def test_nonpositive_search_limit_returns_no_rows(db_with_game):
    db_with_game.add_game(appid=10, name="Miner", detailed_description_text="Build drills")

    assert db_with_game.search_games("drills", 0) == []
    assert db_with_game.search_games("drills", -1) == []
