from dataclasses import replace

import pytest

from steam_catalog.db import Database
from steam_catalog.models import GameDetails, Review, ReviewPage
from steam_catalog.pipeline import CatalogPipeline, select_reviews


def mixed(positive: int, negative: int, *, prefix: str = "review") -> tuple[Review, ...]:
    reviews = []
    for index in range(max(positive, negative)):
        if index < positive:
            reviews.append(
                Review(
                    recommendation_id=f"{prefix}-positive-{index}",
                    appid=10,
                    review_text=f"Positive review {index}",
                    voted_up=True,
                )
            )
        if index < negative:
            reviews.append(
                Review(
                    recommendation_id=f"{prefix}-negative-{index}",
                    appid=10,
                    review_text=f"Negative review {index}",
                    voted_up=False,
                )
            )
    return tuple(reviews)


def summary(positive: int, negative: int) -> ReviewPage:
    return ReviewPage(
        total_reviews=positive + negative,
        total_positive=positive,
        total_negative=negative,
    )


class RecordingSteamClient:
    def __init__(
        self,
        *,
        helpful: tuple[Review, ...],
        recent: tuple[Review, ...],
        targeted: dict[tuple[str, str], tuple[Review, ...]] | None = None,
        aggregate: ReviewPage | None = None,
    ) -> None:
        self.helpful = helpful
        self.recent = recent
        self.targeted = targeted or {}
        self.aggregate = aggregate or summary(10, 10)
        self.targeted_calls: list[tuple[int, str, str, int]] = []
        self.calls: list[tuple[int, str, str, int]] = []

    def fetch_app_pages(self):
        return iter(())

    def fetch_app_details(self, appid: int) -> GameDetails:
        return GameDetails(
            appid=appid,
            name="Sample Game",
            type="game",
            is_released=True,
            coming_soon=False,
        )

    def fetch_reviews(self, appid: int, filter: str, review_type: str, limit: int) -> ReviewPage:
        self.calls.append((appid, filter, review_type, limit))
        if limit == 1:
            return self.aggregate
        if review_type != "all":
            self.targeted_calls.append((appid, filter, review_type, limit))
            rows = self.targeted.get((filter, review_type), ())
        else:
            rows = self.helpful if filter == "all" else self.recent
        return replace(self.aggregate, reviews=rows)


class EmptySteamSpyClient:
    def fetch_page(self, page: int):
        return []


@pytest.fixture
def review_pipeline_factory(tmp_path):
    databases: list[Database] = []

    def factory(
        *,
        helpful: tuple[Review, ...] | None = None,
        recent: tuple[Review, ...] | None = None,
        targeted: dict[tuple[str, str], tuple[Review, ...]] | None = None,
        aggregate: ReviewPage | None = None,
        shared_review: bool = False,
    ):
        helpful_rows = helpful or mixed(5, 5, prefix="helpful")
        recent_rows = recent or mixed(5, 5, prefix="recent")
        if shared_review:
            recent_rows = (replace(recent_rows[0], recommendation_id="same"), *recent_rows[1:])
            helpful_rows = (replace(helpful_rows[0], recommendation_id="same"), *helpful_rows[1:])

        database = Database(tmp_path / f"catalog-{len(databases)}.sqlite3")
        database.initialize()
        databases.append(database)
        steam = RecordingSteamClient(
            helpful=helpful_rows,
            recent=recent_rows,
            targeted=targeted,
            aggregate=aggregate,
        )
        pipeline = CatalogPipeline(
            database,
            steam=steam,
            steamspy=EmptySteamSpyClient(),
        )
        return pipeline, database if shared_review else steam

    yield factory

    for database in databases:
        database.close()


def test_select_reviews_preserves_order_and_deduplicates_by_recommendation_id():
    rows = mixed(3, 2)
    rows = (rows[0], rows[1], rows[0], *rows[2:])

    selected = select_reviews(rows, "positive", limit=2)

    assert [review.recommendation_id for review in selected] == [
        "review-positive-0",
        "review-positive-1",
    ]


def test_select_reviews_with_zero_limit_returns_no_rows():
    assert select_reviews(mixed(2, 2), "positive", limit=0) == []


def test_broad_pages_fill_all_four_groups_without_fallback(review_pipeline_factory):
    pipeline, steam = review_pipeline_factory(
        helpful=mixed(5, 5, prefix="helpful"),
        recent=mixed(5, 5, prefix="recent"),
    )

    samples = pipeline.collect_review_samples(10, summary(positive=10, negative=10))

    assert {kind: len(rows) for kind, rows in samples.items()} == {
        "helpful_positive": 5,
        "helpful_negative": 5,
        "recent_positive": 5,
        "recent_negative": 5,
    }
    assert steam.calls == [
        (10, "all", "all", 100),
        (10, "recent", "all", 100),
    ]
    assert steam.targeted_calls == []


def test_missing_negative_group_triggers_only_needed_fallback(review_pipeline_factory):
    pipeline, steam = review_pipeline_factory(
        helpful=mixed(10, 0, prefix="helpful"),
        recent=mixed(5, 5, prefix="recent"),
        targeted={
            ("all", "negative"): mixed(0, 5, prefix="fallback-helpful"),
        },
    )

    samples = pipeline.collect_review_samples(10, summary(positive=15, negative=5))

    assert len(samples["helpful_negative"]) == 5
    assert steam.targeted_calls == [(10, "all", "negative", 5)]


def test_aggregate_count_prevents_fallback_for_nonexistent_polarity(review_pipeline_factory):
    pipeline, steam = review_pipeline_factory(
        helpful=mixed(5, 0, prefix="helpful"),
        recent=mixed(5, 0, prefix="recent"),
    )

    samples = pipeline.collect_review_samples(10, summary(positive=10, negative=0))

    assert samples["helpful_negative"] == []
    assert samples["recent_negative"] == []
    assert steam.targeted_calls == []


def test_same_review_is_stored_once_but_can_have_multiple_sample_kinds(
    review_pipeline_factory,
):
    pipeline, db = review_pipeline_factory(shared_review=True)

    pipeline.collect_one(10)

    assert db.review_count(10) == 19
    memberships = db.connection.execute(
        """
        SELECT sample_kind, sample_rank
        FROM review_samples
        WHERE appid = 10 AND recommendation_id = 'same'
        ORDER BY sample_kind
        """
    ).fetchall()
    assert [tuple(row) for row in memberships] == [
        ("helpful_positive", 1),
        ("recent_positive", 1),
    ]


def test_replace_review_samples_removes_unreferenced_reviews_and_search_rows(initialized_db):
    initialized_db.upsert_game(
        appid=10,
        name="Sample Game",
        eligibility_status="eligible",
        updated_at="2026-07-20T00:00:00Z",
    )
    old_review = Review(
        recommendation_id="old",
        appid=10,
        review_text="obsolete sample text",
        voted_up=True,
    )
    new_review = replace(
        old_review,
        recommendation_id="new",
        review_text="replacement sample text",
    )
    initialized_db.replace_review_samples(
        10,
        {"helpful_positive": [old_review]},
        fetched_at="2026-07-20T00:01:00Z",
    )

    initialized_db.replace_review_samples(
        10,
        {"helpful_positive": [new_review]},
        fetched_at="2026-07-20T00:02:00Z",
    )

    review_ids = initialized_db.connection.execute(
        "SELECT recommendation_id FROM reviews ORDER BY recommendation_id"
    ).fetchall()
    assert [row["recommendation_id"] for row in review_ids] == ["new"]
    assert initialized_db.search_reviews("obsolete", 10) == []
    assert (
        initialized_db.connection.execute(
            "SELECT COUNT(*) FROM review_search WHERE review_search MATCH 'obsolete'"
        ).fetchone()[0]
        == 0
    )
    assert (
        initialized_db.connection.execute(
            """
            SELECT COUNT(*)
            FROM reviews
            LEFT JOIN review_samples USING(recommendation_id)
            WHERE review_samples.recommendation_id IS NULL
            """
        ).fetchone()[0]
        == 0
    )
