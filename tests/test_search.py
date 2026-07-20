from dataclasses import replace

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


@pytest.mark.parametrize("query", ["", "   ", "_", "!@#$%^&*()", '" OR name:* -'])
def test_search_treats_empty_or_operator_input_as_safe_tokens(db_with_game, query):
    db_with_game.add_game(appid=10, name="Miner", detailed_description_text="Build drills")

    assert db_with_game.search_games(query, 10) == []


def test_nonpositive_search_limit_returns_no_rows(db_with_game):
    db_with_game.add_game(appid=10, name="Miner", detailed_description_text="Build drills")

    assert db_with_game.search_games("drills", 0) == []
    assert db_with_game.search_games("drills", -1) == []
