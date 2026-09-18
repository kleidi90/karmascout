"""Tests for HTML rendering, including the structure the copy buttons rely on."""

from __future__ import annotations

from pathlib import Path

import pytest

from karmascout.config import Settings
from karmascout.models import AiVerdict, RedditItem, ScoredItem
from karmascout.report import (
    MAX_COMMENT_CHARS,
    badge_colors,
    build_card,
    render_report,
    safe_link,
    truncate_body,
    write_report,
)
from tests.conftest import NOW

RUN_TIME = "Thursday, 17 September 2026 - 12:00"


def render(scored: list[ScoredItem], settings: Settings) -> str:
    """Render a report at the fixed test timestamp."""
    return render_report(scored, RUN_TIME, settings, NOW)


# ── Presentation helpers ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("score", "expected"),
    [(10, "#16a34a"), (8, "#16a34a"), (7, "#d97706"), (6, "#d97706"), (5, "#dc2626")],
)
def test_badge_colours_match_the_original_thresholds(score: int, expected: str) -> None:
    assert badge_colors(score)[0] == expected


def test_body_preview_is_truncated_with_an_ellipsis() -> None:
    assert truncate_body("x" * 250).endswith("...")
    assert len(truncate_body("x" * 250)) == 203


def test_short_body_is_left_alone() -> None:
    assert truncate_body("kurz") == "kurz"


@pytest.mark.parametrize(
    "url",
    ["https://www.reddit.com/r/de/comments/x/", "http://example.com"],
)
def test_safe_link_allows_http_schemes(url: str) -> None:
    assert safe_link(url) == url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,<script>", "", "ftp://x"])
def test_safe_link_rejects_everything_else(url: str) -> None:
    assert safe_link(url) == ""


def test_risk_none_is_not_displayed(scored: ScoredItem) -> None:
    assert build_card(scored, NOW).risk == ""


def test_real_risk_is_displayed(item: RedditItem) -> None:
    card = build_card(ScoredItem(item=item, verdict=AiVerdict(score=7, risk="politisch")), NOW)
    assert card.risk == "politisch"


# ── Rendering ────────────────────────────────────────────────────────────────


def test_report_contains_one_card_per_result(scored: ScoredItem, settings: Settings) -> None:
    html = render([scored, scored], settings)
    assert html.count('class="card"') == 2


def test_report_keeps_the_original_copy_button_ids(scored: ScoredItem, settings: Settings) -> None:
    html = render([scored, scored], settings)
    for expected in ('id="ca0"', 'id="cb0"', 'id="ca1"', 'id="cb1"'):
        assert expected in html
    assert "copyById('ca0', this)" in html


def test_report_shows_the_summary_strip(scored: ScoredItem, settings: Settings) -> None:
    html = render([scored], settings)
    assert f"{settings.min_score}/10" in html
    assert f"{settings.account_karma} karma" in html
    assert "r/germany" in html
    assert "Lärmprotokoll" in html


def test_report_shows_score_age_and_subreddit(scored: ScoredItem, settings: Settings) -> None:
    html = render([scored], settings)
    assert "9/10" in html
    assert "4h ago" in html
    assert "r/de" in html


def test_empty_report_still_renders(settings: Settings) -> None:
    html = render([], settings)
    assert "0 opportunities" in html
    assert 'class="card"' not in html


def test_html_in_model_output_is_escaped(item: RedditItem, settings: Settings) -> None:
    """Autoescaping must hold for every field, including LLM-authored text."""
    hostile = ScoredItem(
        item=item,
        verdict=AiVerdict(score=9, comment_a="<script>alert('x')</script>", score_reason="a & b"),
    )
    html = render([hostile], settings)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "a &amp; b" in html


def test_hostile_title_is_escaped(settings: Settings) -> None:
    hostile_item = RedditItem(
        subreddit="de",
        url="https://r.example/1",
        title="<img src=x onerror=alert(1)>",
        created_utc=NOW,
    )
    html = render([ScoredItem(item=hostile_item, verdict=AiVerdict(score=9))], settings)
    assert "<img src=x" not in html


def test_unsafe_url_is_not_rendered_as_a_link(settings: Settings) -> None:
    bad = RedditItem(subreddit="de", url="javascript:alert(1)", title="T", created_utc=NOW)
    html = render([ScoredItem(item=bad, verdict=AiVerdict(score=9))], settings)
    assert 'href="javascript:' not in html
    assert 'class="thread-link"' in html


# ── Writing ──────────────────────────────────────────────────────────────────


def test_write_report_creates_parent_directories(
    scored: ScoredItem, settings: Settings, tmp_path: Path
) -> None:
    destination = tmp_path / "nested" / "out.html"
    written = write_report([scored], RUN_TIME, settings, destination, NOW)
    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_write_report_uses_utf8(scored: ScoredItem, settings: Settings, tmp_path: Path) -> None:
    written = write_report([scored], RUN_TIME, settings, tmp_path / "out.html", NOW)
    assert "Lärmprotokoll" in written.read_text(encoding="utf-8")


# ── Drafted-comment safety ───────────────────────────────────────────────────


def test_links_in_a_drafted_comment_are_surfaced(item: RedditItem) -> None:
    """A link is what a thread that steered the model would try to get posted."""
    verdict = AiVerdict(
        score=9,
        comment_a="Schau mal bei https://evil.example vorbei!",
        comment_b="Nichts Verdächtiges hier.",
    )
    card = build_card(ScoredItem(item=item, verdict=verdict), NOW)
    assert card.draft_links == ("https://evil.example",)


def test_a_clean_draft_flags_nothing(scored: ScoredItem) -> None:
    assert build_card(scored, NOW).draft_links == ()


def test_duplicate_links_are_reported_once(item: RedditItem) -> None:
    verdict = AiVerdict(score=9, comment_a="www.x.example", comment_b="www.x.example")
    assert build_card(ScoredItem(item=item, verdict=verdict), NOW).draft_links == ("www.x.example",)


def test_link_warning_is_rendered(item: RedditItem, settings: Settings) -> None:
    verdict = AiVerdict(score=9, comment_a="Siehe https://evil.example")
    html = render_report([ScoredItem(item=item, verdict=verdict)], "now", settings, NOW)
    assert "Check before posting" in html
    assert "https://evil.example" in html


def test_no_warning_without_links(scored: ScoredItem, settings: Settings) -> None:
    assert "Check before posting" not in render_report([scored], "now", settings, NOW)


def test_an_overlong_draft_is_truncated(item: RedditItem) -> None:
    """The prompt asks for 2-4 sentences; anything past the cap ignored that."""
    verdict = AiVerdict(score=9, comment_a="x" * (MAX_COMMENT_CHARS + 500))
    card = build_card(ScoredItem(item=item, verdict=verdict), NOW)
    assert len(card.comment_a) == MAX_COMMENT_CHARS + 3
