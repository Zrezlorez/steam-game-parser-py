from datetime import timedelta

import pytest

from steam_catalog.sources.steam import (
    SteamClient,
    normalize_html,
    parse_app_details,
    parse_review_page,
)


def test_normalize_html_decodes_entities_and_collapses_blocks():
    assert normalize_html("<p>Build &amp; improve</p><br><b>Fast</b>") == "Build & improve\nFast"


def test_normalize_html_removes_script_and_style_content():
    assert normalize_html(
        "<div>Mine</div><script>bad()</script><style>bad{}</style><li>Build</li>"
    ) == ("Mine\nBuild")


def test_normalize_html_collapses_unicode_whitespace():
    assert normalize_html("<p>Build&nbsp;and\u2003improve</p>") == "Build and improve"


@pytest.mark.parametrize("raw_whitespace", ["\n", "\r\n", "\f", "\v", "\u2028", "\u2029"])
def test_normalize_html_collapses_raw_text_whitespace_to_spaces(raw_whitespace):
    assert normalize_html(f"Build{raw_whitespace}and{raw_whitespace}improve") == "Build and improve"


def test_normalize_html_retains_structural_boundaries_as_newlines():
    assert (
        normalize_html(
            "<p>First</p><p>Second</p><ul><li>Third</li><li>Fourth</li></ul>"
            "<table><tr><td>One</td><td>Two</td></tr></table><br>Last"
        )
        == "First\nSecond\nThird\nFourth\nOne\nTwo\nLast"
    )


def test_normalize_html_preserves_boundaries_around_suppressed_content_and_table_cells():
    assert (
        normalize_html(
            "Build<script>bad()</script><style>bad{}</style>faster"
            "<table><tr><td>One</td><td>Two</td></tr></table>"
        )
        == "Build faster\nOne\nTwo"
    )


def test_parse_app_details_keeps_html_and_search_text(load_fixture):
    details = parse_app_details(10, load_fixture("steam_appdetails.json"))

    assert details is not None
    assert details.appid == 10
    assert details.type == "game"
    assert details.is_released is True
    assert details.coming_soon is False
    assert details.short_description_text == "Dig faster"
    assert details.detailed_description_html.startswith("<p>")
    assert details.detailed_description_text == "Build drills.\nImprove belts"
    assert details.about_the_game_text == "Mine & automate"
    assert details.developers_json == '["Example Studio"]'
    assert details.publishers_json == '["Example Publisher"]'
    assert details.languages_json == '"English, Russian"'
    assert details.platforms_json == '{"linux":true,"mac":false,"windows":true}'
    assert details.categories_json == '[{"description":"Single-player","id":2}]'
    assert details.genres_json == '[{"description":"Indie","id":"23"}]'
    assert details.header_image_url == "https://cdn.example.test/header.jpg"
    assert details.screenshots_json == '[{"id":1,"path_full":"https://cdn.example.test/one.jpg"}]'
    assert details.price_json == '{"currency":"USD","final":499,"initial":999}'


def test_parse_app_details_returns_none_for_missing_app_and_rejects_malformed_response():
    assert parse_app_details(10, {"10": {"success": False}}) is None

    with pytest.raises(ValueError, match="app details"):
        parse_app_details(10, {"10": {"success": True, "data": []}})

    with pytest.raises(ValueError, match="app details"):
        parse_app_details(10, {"10": {"success": True, "data": {}}})


def test_parse_review_page_reads_aggregate_and_author_fields(load_fixture):
    page = parse_review_page(10, load_fixture("steam_reviews.json"))

    assert (page.total_reviews, page.total_positive, page.total_negative) == (20, 12, 8)
    assert (page.review_score, page.review_score_desc, page.cursor) == (
        8,
        "Very Positive",
        "next-page",
    )
    assert page.reviews[0].recommendation_id == "review-1"
    assert page.reviews[0].author_playtime_at_review == 120
    assert page.reviews[0].author_steamid == "76561198000000000"
    assert page.reviews[0].weighted_vote_score == 0.9
    assert page.reviews[0].created_at == "2026-07-20T00:00:00+00:00"
    assert page.reviews[0].updated_at == "2026-07-21T00:00:00+00:00"


def test_targeted_review_fetch_accepts_page_local_summary_without_aggregate_totals():
    payload = {
        "success": 1,
        "query_summary": {"num_reviews": 1},
        "reviews": [
            {
                "recommendationid": "negative-1",
                "review": "Not for me",
                "voted_up": False,
            }
        ],
    }

    class TargetedReviewHttpClient:
        def get_json(self, url, *, params, cache_ttl, rate_bucket):
            return payload

    page = SteamClient(TargetedReviewHttpClient()).fetch_reviews(10, "all", "negative", 5)

    assert (page.total_reviews, page.total_positive, page.total_negative) == (1, 0, 1)
    assert [review.recommendation_id for review in page.reviews] == ["negative-1"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("total_reviews", "total_positive", "total_negative")
        for value in (None, True, 1.0, "1", -1)
    ],
)
def test_parse_review_page_rejects_corrupt_aggregate_fields(field, value):
    summary = {"total_reviews": 1, "total_positive": 1, "total_negative": 1}
    if value is None:
        del summary[field]
    else:
        summary[field] = value

    with pytest.raises(ValueError, match="Steam reviews"):
        parse_review_page(10, {"success": 1, "query_summary": summary, "reviews": []})


def test_parse_review_page_rejects_unsuccessful_response():
    with pytest.raises(ValueError, match="Steam reviews"):
        parse_review_page(
            10,
            {
                "success": 0,
                "query_summary": {"total_reviews": 0, "total_positive": 0, "total_negative": 0},
                "reviews": [],
            },
        )


@pytest.mark.parametrize("success", [True, 1.0, "1", None, 0])
def test_parse_review_page_rejects_non_exact_success_values(success):
    payload = {
        "query_summary": {"total_reviews": 0, "total_positive": 0, "total_negative": 0},
        "reviews": [],
    }
    if success is not None:
        payload["success"] = success

    with pytest.raises(ValueError, match="Steam reviews"):
        parse_review_page(10, payload)


def test_fetch_methods_use_steam_bucket_and_documented_parameters(load_fixture):
    class RecordingHttpClient:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, str], timedelta, str]] = []

        def get_json(self, url, *, params, cache_ttl, rate_bucket):
            self.calls.append((url, dict(params), cache_ttl, rate_bucket))
            if "appdetails" in url:
                return load_fixture("steam_appdetails.json")
            if "reviews" in url:
                return load_fixture("steam_reviews.json")
            return {"applist": {"apps": [{"appid": "10", "name": "Drill Builder"}, {"appid": 0}]}}

    http = RecordingHttpClient()
    client = SteamClient(http)

    assert [item.appid for item in client.fetch_app_list()] == [10]
    assert client.fetch_app_details(10) is not None
    assert client.fetch_reviews(10, "recent", "positive", 5).total_reviews == 20
    assert http.calls == [
        (
            "https://api.steampowered.com/ISteamApps/GetAppList/v2/",
            {},
            timedelta(days=7),
            "steam",
        ),
        (
            "https://store.steampowered.com/api/appdetails",
            {"appids": "10"},
            timedelta(days=7),
            "steam",
        ),
        (
            "https://store.steampowered.com/appreviews/10",
            {
                "filter": "recent",
                "review_type": "positive",
                "num_per_page": "5",
                "language": "all",
                "json": "1",
            },
            timedelta(days=1),
            "steam",
        ),
    ]
