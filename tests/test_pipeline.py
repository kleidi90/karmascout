"""Tests for orchestration: collection order, deduplication, filtering, ranking."""

from __future__ import annotations

import logging

import pytest

from karmascout.config import Settings
from karmascout.models import AiVerdict, RedditItem
from karmascout.pipeline import Heartbeat, Pipeline, deduplicate, format_elapsed
from karmascout.reddit_client import RedditClient
from karmascout.scoring import Scorer
from tests.conftest import NOW


def thread(url: str, subreddit: str = "de", age_hours: float = 2, title: str = "T") -> RedditItem:
    """Build an in-window thread for pipeline tests."""
    return RedditItem(
        subreddit=subreddit,
        url=url,
        title=title,
        body="",
        keyword="Ruhestörung",
        created_utc=NOW - age_hours * 3600,
    )


class FakeClient(RedditClient):
    """Reddit client returning canned results per subreddit, with no I/O."""

    def __init__(
        self,
        settings: Settings,
        search: dict[str, list[RedditItem]] | None = None,
        feeds: dict[str, list[RedditItem]] | None = None,
    ) -> None:
        super().__init__(settings, sleep=lambda _s: None)
        self.search_results = search or {}
        self.feed_results = feeds or {}
        self.cutoffs: list[float] = []

    def search_subreddit(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        self.cutoffs.append(cutoff)
        return list(self.search_results.get(subreddit, []))

    def scan_feeds(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        return list(self.feed_results.get(subreddit, []))


class FakeScorer(Scorer):
    """Scorer returning a canned verdict per thread URL, with no I/O."""

    def __init__(self, settings: Settings, verdicts: dict[str, AiVerdict | None]) -> None:
        super().__init__(settings, sleep=lambda _s: None)
        self.verdicts = verdicts
        self.scored_urls: list[str] = []

    def score(self, item: RedditItem, now: float | None = None) -> AiVerdict | None:
        self.scored_urls.append(item.url)
        return self.verdicts.get(item.url)


def one_sub(settings: Settings) -> Settings:
    """Narrow the settings to a single subreddit so tests stay readable."""
    return settings.model_copy(update={"subreddits": ("de",)})


# ── Helpers ──────────────────────────────────────────────────────────────────


def test_format_elapsed() -> None:
    assert format_elapsed(0) == "0m00s"
    assert format_elapsed(67) == "1m07s"
    assert format_elapsed(3600) == "60m00s"


def test_deduplicate_keeps_first_occurrence() -> None:
    first, second = thread("https://a"), thread("https://a", title="dup")
    assert deduplicate([first, second, thread("https://b")]) == [first, thread("https://b")]


def test_heartbeat_respects_its_interval(caplog: pytest.LogCaptureFixture) -> None:
    beat = Heartbeat(interval=60.0, logger=logging.getLogger("karmascout"))
    beat.start_phase("phase", now=0.0)
    with caplog.at_level(logging.INFO):
        beat.tick(1, 10, 1, now=5.0)
        assert "HEARTBEAT" not in caplog.text
        beat.tick(2, 10, 2, now=90.0)
        assert "HEARTBEAT" in caplog.text


def test_heartbeat_can_be_forced(caplog: pytest.LogCaptureFixture) -> None:
    beat = Heartbeat(interval=60.0, logger=logging.getLogger("karmascout"))
    beat.start_phase("phase", now=0.0)
    with caplog.at_level(logging.INFO):
        beat.tick(1, 10, 1, force=True, now=5.0)
    assert "1/10 (10%)" in caplog.text


def test_heartbeat_eta_is_phase_local(caplog: pytest.LogCaptureFixture) -> None:
    """An ETA uses its own phase's elapsed time, not the whole run's."""
    beat = Heartbeat(interval=0.0, logger=logging.getLogger("karmascout"))
    beat.start_phase("Pass 2", now=1000.0)
    with caplog.at_level(logging.INFO):
        beat.tick(1, 2, 1, now=1010.0)
    # 10s for 1 of 2 items => ETA ~10s, not the ~1010s the old formula produced.
    assert "ETA ~0m10s" in caplog.text


# ── Collection ───────────────────────────────────────────────────────────────


def test_collect_runs_both_passes_and_deduplicates(settings: Settings) -> None:
    config = one_sub(settings)
    shared = thread("https://dup")
    client = FakeClient(
        config,
        search={"de": [shared, thread("https://only-search")]},
        feeds={"de": [shared, thread("https://only-feed")]},
    )
    collected = Pipeline(config, client, FakeScorer(config, {})).collect(NOW)
    assert [entry.url for entry in collected] == [
        "https://dup",
        "https://only-search",
        "https://only-feed",
    ]


def test_collect_passes_the_freshness_cutoff(settings: Settings) -> None:
    config = one_sub(settings)
    client = FakeClient(config)
    Pipeline(config, client, FakeScorer(config, {})).collect(NOW)
    assert client.cutoffs[0] == NOW - config.max_age_seconds


# ── Scoring, filtering, ranking ──────────────────────────────────────────────


def test_score_all_filters_below_the_threshold(settings: Settings) -> None:
    config = one_sub(settings)
    keep, drop = thread("https://keep"), thread("https://drop")
    scorer = FakeScorer(
        config,
        {
            "https://keep": AiVerdict(score=config.min_score),
            "https://drop": AiVerdict(score=config.min_score - 1),
        },
    )
    result = Pipeline(config, FakeClient(config), scorer).score_all([keep, drop], NOW)
    assert [entry.item.url for entry in result] == ["https://keep"]


def test_score_all_ranks_highest_first(settings: Settings) -> None:
    config = one_sub(settings)
    items = [thread("https://a"), thread("https://b"), thread("https://c")]
    scorer = FakeScorer(
        config,
        {
            "https://a": AiVerdict(score=7),
            "https://b": AiVerdict(score=10),
            "https://c": AiVerdict(score=8),
        },
    )
    result = Pipeline(config, FakeClient(config), scorer).score_all(items, NOW)
    assert [entry.score for entry in result] == [10, 8, 7]


def test_failed_scores_are_dropped_not_fatal(settings: Settings) -> None:
    config = one_sub(settings)
    items = [thread("https://ok"), thread("https://broken")]
    scorer = FakeScorer(config, {"https://ok": AiVerdict(score=9), "https://broken": None})
    result = Pipeline(config, FakeClient(config), scorer).score_all(items, NOW)
    assert [entry.item.url for entry in result] == ["https://ok"]


def test_score_all_on_empty_input(settings: Settings) -> None:
    config = one_sub(settings)
    assert Pipeline(config, FakeClient(config), FakeScorer(config, {})).score_all([], NOW) == []


def test_every_candidate_is_scored_exactly_once(settings: Settings) -> None:
    config = one_sub(settings)
    items = [thread(f"https://{index}") for index in range(10)]
    scorer = FakeScorer(config, {entry.url: AiVerdict(score=9) for entry in items})
    Pipeline(config, FakeClient(config), scorer).score_all(items, NOW)
    assert sorted(scorer.scored_urls) == sorted(entry.url for entry in items)


def test_run_collects_then_scores(settings: Settings) -> None:
    config = one_sub(settings)
    client = FakeClient(config, search={"de": [thread("https://a")]})
    scorer = FakeScorer(config, {"https://a": AiVerdict(score=10)})
    result = Pipeline(config, client, scorer).run(NOW)
    assert len(result) == 1
    assert result[0].score == 10


# ── Cost ceiling ─────────────────────────────────────────────────────────────


def test_scoring_is_capped_at_the_configured_ceiling(settings: Settings) -> None:
    """Each candidate costs one paid call, so an unbounded run must not be possible."""
    many = [thread(f"https://t/{index}", age_hours=index + 1) for index in range(10)]
    capped = one_sub(settings).model_copy(update={"max_items_to_score": 3})
    client = FakeClient(capped, search={"de": many})
    assert len(Pipeline(capped, client, FakeScorer(capped, {})).collect(NOW)) == 3


def test_the_cap_keeps_the_newest_threads(settings: Settings) -> None:
    """The prompt rates 2-20 hours old as ideal, so the oldest are the ones to drop."""
    old = thread("https://old", age_hours=20)
    new = thread("https://new", age_hours=1)
    capped = one_sub(settings).model_copy(update={"max_items_to_score": 1})
    client = FakeClient(capped, search={"de": [old, new]})
    assert Pipeline(capped, client, FakeScorer(capped, {})).collect(NOW) == [new]


def test_a_zero_cap_means_unlimited(settings: Settings) -> None:
    many = [thread(f"https://t/{index}") for index in range(10)]
    uncapped = one_sub(settings).model_copy(update={"max_items_to_score": 0})
    client = FakeClient(uncapped, search={"de": many})
    assert len(Pipeline(uncapped, client, FakeScorer(uncapped, {})).collect(NOW)) == 10


# ── Resilience ───────────────────────────────────────────────────────────────


def test_one_exploding_scorer_call_does_not_lose_the_run(settings: Settings) -> None:
    """A whole run's Reddit work is already spent by this point; do not discard it."""

    class ExplodingScorer(FakeScorer):
        def score(self, item: RedditItem, now: float | None = None) -> AiVerdict | None:
            if item.url == "https://boom":
                msg = "unforeseen"
                raise RuntimeError(msg)
            return AiVerdict(score=9, comment_a="A", comment_b="B")

    config = one_sub(settings)
    items = [thread("https://boom"), thread("https://fine")]
    kept = Pipeline(config, FakeClient(config), ExplodingScorer(config, {})).score_all(items, NOW)
    assert [scored.item.url for scored in kept] == ["https://fine"]
