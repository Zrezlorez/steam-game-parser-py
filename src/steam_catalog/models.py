"""Shared typed domain values and persisted status values."""

from dataclasses import dataclass
from enum import Enum


class EligibilityStatus(str, Enum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class DiscoveryItem:
    """A candidate game returned by a discovery provider."""

    appid: int
    name: str = ""
    steamspy_positive: int | None = None
    steamspy_negative: int | None = None
    provisional_reviews: int | None = None
    payload_json: str = "{}"


@dataclass(frozen=True)
class GameDetails:
    """Store metadata shaped for the ``games`` repository fields."""

    appid: int
    name: str = ""
    type: str | None = None
    is_released: bool | None = None
    release_date: str | None = None
    coming_soon: bool | None = None
    short_description_html: str | None = None
    short_description_text: str | None = None
    detailed_description_html: str | None = None
    detailed_description_text: str | None = None
    about_the_game_html: str | None = None
    about_the_game_text: str | None = None
    developers_json: str | None = None
    publishers_json: str | None = None
    languages_json: str | None = None
    platforms_json: str | None = None
    categories_json: str | None = None
    genres_json: str | None = None
    header_image_url: str | None = None
    screenshots_json: str | None = None
    price_json: str | None = None


@dataclass(frozen=True)
class Review:
    """A Steam user review shaped for the ``reviews`` repository fields."""

    recommendation_id: str
    appid: int
    review_text: str
    voted_up: bool
    language: str | None = None
    votes_up: int | None = None
    votes_funny: int | None = None
    weighted_vote_score: float | None = None
    comment_count: int | None = None
    steam_purchase: bool | None = None
    received_for_free: bool | None = None
    written_during_early_access: bool | None = None
    author_steamid: str | None = None
    author_playtime_forever: int | None = None
    author_playtime_at_review: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ReviewPage:
    """Steam review aggregate data plus a page of individual reviews."""

    total_reviews: int
    total_positive: int = 0
    total_negative: int = 0
    review_score: int | None = None
    review_score_desc: str | None = None
    reviews: tuple[Review, ...] = ()
    cursor: str | None = None
