"""Full-text search index maintenance and ranked query helpers."""

import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

_SNIPPET_CONTEXT = 80


@dataclass(frozen=True)
class _SearchToken:
    folded: str
    start: int
    end: int


def search_games(connection: sqlite3.Connection, query: str, limit: int) -> list[dict[str, object]]:
    """Return BM25-ranked game matches with source-backed highlighted snippets."""
    match_query, tokens = _quoted_match_query(query)
    if not match_query or limit <= 0:
        return []
    rows = connection.execute(
        """
        SELECT
            games.appid, games.name, bm25(game_search) AS rank,
            games.short_description_text, games.detailed_description_text,
            games.about_the_game_text, games.developers_json, games.publishers_json,
            games.genres_json, games.categories_json
        FROM game_search
        JOIN games ON games.appid = game_search.rowid
        WHERE game_search MATCH ?
        ORDER BY rank, games.appid
        LIMIT ?
        """,
        (match_query, limit),
    ).fetchall()
    return [
        {
            "appid": row["appid"],
            "name": row["name"],
            "rank": row["rank"],
            "snippet": _highlighted_snippet(
                (
                    row["name"],
                    row["short_description_text"],
                    row["detailed_description_text"],
                    row["about_the_game_text"],
                    row["developers_json"],
                    row["publishers_json"],
                    row["genres_json"],
                    row["categories_json"],
                ),
                tokens,
            ),
        }
        for row in rows
    ]


def search_reviews(
    connection: sqlite3.Connection, query: str, limit: int
) -> list[dict[str, object]]:
    """Return BM25-ranked review matches with game and sample context."""
    match_query, tokens = _quoted_match_query(query)
    if not match_query or limit <= 0:
        return []
    rows = connection.execute(
        """
        SELECT
            reviews.appid, games.name, reviews.recommendation_id,
            reviews.review_text, bm25(review_search) AS rank
        FROM review_search
        JOIN reviews ON reviews.rowid = review_search.rowid
        JOIN games ON games.appid = reviews.appid
        WHERE review_search MATCH ?
        ORDER BY rank, reviews.recommendation_id
        LIMIT ?
        """,
        (match_query, limit),
    ).fetchall()
    memberships_by_review: dict[str, list[dict[str, object]]] = {
        row["recommendation_id"]: [] for row in rows
    }
    review_ids = tuple(memberships_by_review)
    parameter_limit = max(1, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    for offset in range(0, len(review_ids), parameter_limit):
        chunk = review_ids[offset : offset + parameter_limit]
        placeholders = ", ".join("?" for _ in chunk)
        memberships = connection.execute(
            f"""
            SELECT recommendation_id, sample_kind, sample_rank
            FROM review_samples
            WHERE recommendation_id IN ({placeholders})
            ORDER BY recommendation_id, sample_kind, sample_rank
            """,
            chunk,
        ).fetchall()
        for membership in memberships:
            memberships_by_review[membership["recommendation_id"]].append(
                {
                    "sample_kind": membership["sample_kind"],
                    "sample_rank": membership["sample_rank"],
                }
            )
    return [
        {
            "appid": row["appid"],
            "name": row["name"],
            "recommendation_id": row["recommendation_id"],
            "rank": row["rank"],
            "snippet": _highlighted_snippet((row["review_text"],), tokens),
            "samples": memberships_by_review[row["recommendation_id"]],
        }
        for row in rows
    ]


def delete_game_row(connection: sqlite3.Connection, appid: int) -> None:
    """Remove a game from the external-content FTS index."""
    connection.execute(
        """
        INSERT INTO game_search(
            game_search, rowid, name, short_description_text, detailed_description_text,
            about_the_game_text, developers, publishers, genres, categories
        )
        SELECT
            'delete', appid, name, COALESCE(short_description_text, ''),
            COALESCE(detailed_description_text, ''), COALESCE(about_the_game_text, ''),
            COALESCE(developers_json, ''), COALESCE(publishers_json, ''),
            COALESCE(genres_json, ''), COALESCE(categories_json, '')
        FROM games
        WHERE appid = ?
        """,
        (appid,),
    )


def insert_game_row(connection: sqlite3.Connection, appid: int) -> None:
    """Index one existing game row for full-text search."""
    connection.execute(
        """
        INSERT INTO game_search(
            rowid, name, short_description_text, detailed_description_text,
            about_the_game_text, developers, publishers, genres, categories
        )
        SELECT
            appid, name, COALESCE(short_description_text, ''),
            COALESCE(detailed_description_text, ''), COALESCE(about_the_game_text, ''),
            COALESCE(developers_json, ''), COALESCE(publishers_json, ''),
            COALESCE(genres_json, ''), COALESCE(categories_json, '')
        FROM games
        WHERE appid = ?
        """,
        (appid,),
    )


def delete_review_row(connection: sqlite3.Connection, recommendation_id: str) -> None:
    """Remove a review from the external-content FTS index."""
    connection.execute(
        """
        INSERT INTO review_search(
            review_search, rowid, appid, recommendation_id, review_text
        )
        SELECT 'delete', rowid, appid, recommendation_id, review_text
        FROM reviews
        WHERE recommendation_id = ?
        """,
        (recommendation_id,),
    )


def _quoted_match_query(query: str) -> tuple[str, tuple[_SearchToken, ...]]:
    tokens = _unicode61_tokens(query)
    match_query = " ".join(f'"{token.folded.replace(chr(34), chr(34) * 2)}"' for token in tokens)
    return match_query, tokens


def _highlighted_snippet(values: Sequence[str | None], query_tokens: Sequence[_SearchToken]) -> str:
    query_terms = frozenset(token.folded for token in query_tokens)
    if not query_terms:
        return next((value[:_SNIPPET_CONTEXT] for value in values if value), "")
    text = next((value for value in values if value), "")
    matches: tuple[_SearchToken, ...] = ()
    for value in values:
        if not value:
            continue
        candidate_matches = tuple(
            token for token in _unicode61_tokens(value) if token.folded in query_terms
        )
        if candidate_matches:
            text = value
            matches = candidate_matches
            break
    if not matches:
        return text[:_SNIPPET_CONTEXT]

    match_start = matches[0].start
    match_end = matches[0].end
    start = max(0, match_start - _SNIPPET_CONTEXT)
    end = min(len(text), match_end + _SNIPPET_CONTEXT)
    if start:
        next_space = text.find(" ", start)
        start = next_space + 1 if next_space >= 0 and next_space < match_start else start
    if end < len(text):
        previous_space = text.rfind(" ", match_end, end)
        end = previous_space if previous_space >= match_start else end
    highlighted_parts = []
    cursor = start
    for token in matches:
        highlight_start, highlight_end = token.start, token.end
        if highlight_start >= end or highlight_end <= start:
            continue
        if highlight_start < cursor:
            continue
        highlighted_parts.extend(
            (text[cursor:highlight_start], "<mark>", text[highlight_start:highlight_end], "</mark>")
        )
        cursor = highlight_end
    highlighted_parts.append(text[cursor:end])
    highlighted = "".join(highlighted_parts)
    return f"{'... ' if start else ''}{highlighted}{' ...' if end < len(text) else ''}"


def _unicode61_tokens(text: str) -> tuple[_SearchToken, ...]:
    tokens: list[_SearchToken] = []
    folded_parts: list[str] = []
    token_start: int | None = None
    token_end = 0

    def append_token() -> None:
        nonlocal token_start
        if token_start is not None and (folded := "".join(folded_parts)):
            tokens.append(_SearchToken(folded, token_start, token_end))
        folded_parts.clear()
        token_start = None

    for index, character in enumerate(text):
        category = unicodedata.category(character)
        is_token_character = category[0] in {"L", "N"} or category == "Co"
        if is_token_character:
            if token_start is None:
                token_start = index
            folded_parts.extend(_unicode61_fold(character))
            token_end = index + 1
        elif category[0] == "M" and token_start is not None:
            token_end = index + 1
        else:
            append_token()
    append_token()
    return tuple(tokens)


def _unicode61_fold(character: str) -> str:
    decomposed = unicodedata.normalize("NFD", character.lower())
    return "".join(part for part in decomposed if unicodedata.category(part)[0] != "M")
