"""Tests for the typed models, especially coercion of untrusted LLM output."""

from __future__ import annotations

import pytest

from karmascout.models import AiVerdict, RedditItem, ScoredItem
from tests.conftest import NOW


def test_age_hours_rounds_to_whole_hours(item: RedditItem) -> None:
    assert item.age_hours(NOW) == 4


def test_created_at_is_timezone_aware(item: RedditItem) -> None:
    assert item.created_at.tzinfo is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (7, 7),
        (7.4, 7),
        ("7", 7),
        ("7/10", 7),
        ("  8 ", 8),
        (None, 0),
        ("not a number", 0),
        (True, 0),
        (99, 10),
        (-3, 0),
    ],
)
def test_score_is_coerced_and_clamped(raw: object, expected: int) -> None:
    """A model that answers with a quoted score must not take the run down."""
    assert AiVerdict.model_validate({"score": raw}).score == expected


def test_missing_fields_default_instead_of_raising() -> None:
    """A partial model response must not abort a run after all the fetching work."""
    verdict = AiVerdict.model_validate({"score": 8})
    assert verdict.comment_a == ""
    assert verdict.comment_b == ""
    assert verdict.score_reason == ""
    assert verdict.risk == "none"


def test_non_string_text_fields_are_coerced() -> None:
    verdict = AiVerdict.model_validate({"score": 5, "score_reason": 42, "risk": None})
    assert verdict.score_reason == "42"
    assert verdict.risk == ""


def test_scored_item_exposes_score(scored: ScoredItem) -> None:
    assert scored.score == scored.verdict.score
