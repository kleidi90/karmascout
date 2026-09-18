"""Tests for the persistent verdict cache.

The cache exists to stop consecutive runs paying OpenRouter twice for the same
thread, so these tests pin both halves of that bargain: a hit must cost no call,
and it must return exactly what the first run would have shown.
"""

from __future__ import annotations

import json
from pathlib import Path

from karmascout.cache import CACHE_VERSION, CachingScorer, VerdictCache
from karmascout.models import AiVerdict, RedditItem
from tests.conftest import NOW


class CountingScorer:
    """A ``ScoreSource`` that records how often it was actually consulted."""

    def __init__(self, verdict: AiVerdict | None) -> None:
        self.verdict = verdict
        self.calls = 0

    def score(self, item: RedditItem, now: float | None = None) -> AiVerdict | None:
        self.calls += 1
        return self.verdict


def make_cache(tmp_path: Path) -> VerdictCache:
    return VerdictCache(tmp_path / "cache.json", max_age_seconds=24 * 3600)


def test_first_score_calls_through_and_is_remembered(
    tmp_path: Path, item: RedditItem, verdict: AiVerdict
) -> None:
    cache = make_cache(tmp_path)
    inner = CountingScorer(verdict)
    scorer = CachingScorer(inner, cache)

    assert scorer.score(item, NOW) == verdict
    assert inner.calls == 1
    assert cache.get(item) == verdict


def test_second_score_is_served_without_a_call(
    tmp_path: Path, item: RedditItem, verdict: AiVerdict
) -> None:
    cache = make_cache(tmp_path)
    inner = CountingScorer(verdict)
    scorer = CachingScorer(inner, cache)

    first = scorer.score(item, NOW)
    second = scorer.score(item, NOW)

    assert inner.calls == 1
    assert second == first  # identical report, zero extra spend


def test_failed_scores_are_not_cached(tmp_path: Path, item: RedditItem) -> None:
    """A None verdict is a transient failure and must be retried next run."""
    cache = make_cache(tmp_path)
    inner = CountingScorer(None)
    scorer = CachingScorer(inner, cache)

    scorer.score(item, NOW)
    scorer.score(item, NOW)

    assert inner.calls == 2


def test_cache_survives_a_round_trip_to_disk(
    tmp_path: Path, item: RedditItem, verdict: AiVerdict
) -> None:
    written = make_cache(tmp_path)
    written.put(item, verdict)
    written.save(NOW)

    reloaded = make_cache(tmp_path)
    reloaded.load()
    assert reloaded.get(item) == verdict
    assert reloaded.hits == 1


def test_entries_outside_the_freshness_window_are_dropped(
    tmp_path: Path, verdict: AiVerdict
) -> None:
    """A thread too old to be collected again can never be a hit, so it is not kept."""
    stale = RedditItem(subreddit="de", url="stale", created_utc=NOW - 72 * 3600)
    cache = make_cache(tmp_path)
    cache.put(stale, verdict)
    cache.save(NOW)

    stored = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert stored["entries"] == {}


def test_a_corrupt_cache_file_is_ignored_not_fatal(tmp_path: Path, item: RedditItem) -> None:
    path = tmp_path / "cache.json"
    path.write_text("{ not json", encoding="utf-8")
    cache = VerdictCache(path, max_age_seconds=24 * 3600)
    cache.load()
    assert cache.get(item) is None


def test_a_cache_from_a_future_format_is_discarded(tmp_path: Path, item: RedditItem) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"version": CACHE_VERSION + 1, "entries": {}}), encoding="utf-8")
    cache = VerdictCache(path, max_age_seconds=24 * 3600)
    cache.load()
    assert cache.get(item) is None
