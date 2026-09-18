"""Orchestration: collect threads, score them, filter, and rank.

This module owns the shape of a run but performs no I/O of its own. Reddit access
arrives as a :class:`~karmascout.reddit_client.RedditSource` and LLM access as a
:class:`~karmascout.scoring.ScoreSource`, both injected, so the whole pipeline can
be exercised in tests with stubs and no network.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from karmascout.config import Settings
from karmascout.logging_setup import get_logger
from karmascout.models import RedditItem, ScoredItem
from karmascout.reddit_client import RedditSource
from karmascout.scoring import ScoreSource


def format_elapsed(seconds: float) -> str:
    """Format a duration as ``"3m07s"``.

    Args:
        seconds: Duration in seconds.

    Returns:
        The formatted duration.
    """
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"


@dataclass
class Heartbeat:
    """Throttled progress reporter.

    Each phase is timed from its own start. Dividing total elapsed time by progress
    within the current phase would inflate the ETA of every phase after the first.

    Args:
        interval: Minimum seconds between printed heartbeats.
        logger: Logger to write to.
    """

    interval: float
    logger: logging.Logger
    _phase: str = field(default="", init=False)
    _phase_start: float = field(default=0.0, init=False)
    _last_emit: float = field(default=0.0, init=False)

    def start_phase(self, phase: str, now: float | None = None) -> None:
        """Begin timing a new phase.

        Args:
            phase: Human-readable phase name.
            now: POSIX timestamp to treat as the phase start. Defaults to now.
        """
        moment = time.time() if now is None else now
        self._phase = phase
        self._phase_start = moment
        self._last_emit = moment

    def tick(
        self,
        done: int,
        total: int,
        matches: int | Callable[[], int],
        *,
        force: bool = False,
        now: float | None = None,
    ) -> None:
        """Emit a progress line, at most once per :attr:`interval` seconds.

        Args:
            done: Items completed in this phase.
            total: Items expected in this phase.
            matches: Running count of useful results so far, or a callable returning
                it. Pass a callable when the count is expensive: it is only invoked
                for ticks that actually print.
            force: Emit regardless of the interval.
            now: POSIX timestamp. Defaults to now.
        """
        moment = time.time() if now is None else now
        if not force and moment - self._last_emit < self.interval:
            return
        self._last_emit = moment

        resolved_matches = matches() if callable(matches) else matches
        percent = round(done / total * 100) if total else 0
        phase_elapsed = moment - self._phase_start
        eta = format_elapsed(phase_elapsed / done * (total - done)) if done and total else "?"
        self.logger.info(
            "HEARTBEAT - %s: %d/%d (%d%%) - %d matches so far - elapsed %s - ETA ~%s",
            self._phase,
            done,
            total,
            percent,
            resolved_matches,
            format_elapsed(phase_elapsed),
            eta,
        )


def deduplicate(items: Sequence[RedditItem]) -> list[RedditItem]:
    """Drop threads whose permalink has already been seen, preserving order.

    Args:
        items: Threads in discovery order.

    Returns:
        The first occurrence of each distinct permalink.
    """
    seen: set[str] = set()
    unique: list[RedditItem] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        unique.append(item)
    return unique


class Pipeline:
    """Runs a full KarmaScout pass: collect, score, filter, rank.

    Args:
        settings: Run configuration.
        client: Reddit access, as a :class:`~karmascout.reddit_client.RedditSource`.
        scorer: LLM access, as a :class:`~karmascout.scoring.ScoreSource`.
    """

    def __init__(self, settings: Settings, client: RedditSource, scorer: ScoreSource) -> None:
        self._settings = settings
        self._client = client
        self._scorer = scorer
        self._log: logging.Logger = get_logger()
        self._heartbeat = Heartbeat(settings.heartbeat_interval, self._log)

    def collect(self, now: float | None = None) -> list[RedditItem]:
        """Gather candidate threads from both passes and deduplicate them.

        Pass 1 runs one OR-combined search per subreddit. Pass 2 scans each
        subreddit's configured feeds and matches keywords locally, catching threads
        the search index has not indexed yet.

        Args:
            now: POSIX timestamp used to compute the freshness cutoff.

        Returns:
            Unique candidate threads, newest-pass-first in discovery order.
        """
        moment = time.time() if now is None else now
        cutoff = moment - self._settings.max_age_seconds
        subreddits = self._settings.subreddits
        collected: list[RedditItem] = []

        self._log.info(
            "Pass 1: keyword search - %d combined queries (all %d keywords per sub)...",
            len(subreddits),
            len(self._settings.keywords),
        )
        self._heartbeat.start_phase("Pass 1 keyword search", moment)
        for index, subreddit in enumerate(subreddits, start=1):
            collected.extend(self._client.search_subreddit(subreddit, cutoff))
            self._heartbeat.tick(index, len(subreddits), lambda: len(deduplicate(collected)))

        self._log.info(
            "Pass 2: direct feed scan - %d subreddits (catches what search misses)...",
            len(subreddits),
        )
        self._heartbeat.start_phase("Pass 2 feed scan")
        for index, subreddit in enumerate(subreddits, start=1):
            collected.extend(self._client.scan_feeds(subreddit, cutoff))
            self._heartbeat.tick(index, len(subreddits), lambda: len(deduplicate(collected)))

        unique = deduplicate(collected)
        self._log.info("Fetching done. Found %d unique matches.", len(unique))
        return self._cap(unique)

    def _cap(self, items: list[RedditItem]) -> list[RedditItem]:
        """Limit how many threads reach the scorer, newest first.

        Every surviving thread costs one LLM call, so an unusually productive run -
        a wider keyword list, a busy day, a long ``max_age_hours`` - would otherwise
        spend without any ceiling. When the cap bites, the newest threads are kept,
        because the prompt rates 2-20 hours old as ideal and the oldest candidates
        are the least likely to be worth commenting on.

        Args:
            items: All unique candidates.

        Returns:
            At most ``max_items_to_score`` threads, or all of them when the cap is
            disabled.
        """
        cap = self._settings.max_items_to_score
        if cap <= 0 or len(items) <= cap:
            return items

        newest_first = sorted(items, key=lambda item: item.created_utc, reverse=True)
        self._log.warning(
            "Capping AI scoring at %d of %d candidates (newest first); %d skipped. "
            "Raise KARMASCOUT_MAX_ITEMS_TO_SCORE, or set it to 0, to score them all.",
            cap,
            len(items),
            len(items) - cap,
        )
        return newest_first[:cap]

    def score_all(self, items: Sequence[RedditItem], now: float | None = None) -> list[ScoredItem]:
        """Score every candidate concurrently and keep those above the threshold.

        OpenRouter is not IP-throttled the way Reddit's search endpoint is, so this
        is the one stage that is safe to parallelise.

        Args:
            items: Candidate threads.
            now: POSIX timestamp used to compute thread ages in the prompt.

        Returns:
            Threads scoring at or above ``min_score``, highest score first.
        """
        if not items:
            return []

        self._log.info(
            "Scoring %d matches with AI (%d in parallel)...",
            len(items),
            self._settings.ai_concurrency,
        )
        self._heartbeat.start_phase("AI scoring")

        kept: list[ScoredItem] = []
        done = 0
        with ThreadPoolExecutor(max_workers=self._settings.ai_concurrency) as pool:
            futures = {pool.submit(self._scorer.score, item, now): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                done += 1
                try:
                    verdict = future.result()
                except Exception:
                    # Scorer.score handles its own expected failures, so anything
                    # arriving here is unforeseen. It is logged in full and the
                    # thread dropped: losing one candidate beats discarding a whole
                    # run's collected work, which is already paid for in Reddit
                    # requests that cannot be cheaply repeated.
                    self._log.exception("Unexpected error scoring %s", item.url)
                    verdict = None

                if verdict is None:
                    self._log.warning(
                        "[%d/%d] AI call failed - r/%s | %s",
                        done,
                        len(items),
                        item.subreddit,
                        item.title[:45],
                    )
                elif verdict.score >= self._settings.min_score:
                    self._log.info(
                        "[%d/%d] KEPT %d/10 r/%s | %dh | %s - %s",
                        done,
                        len(items),
                        verdict.score,
                        item.subreddit,
                        item.age_hours(now),
                        item.title[:45],
                        verdict.score_reason[:60],
                    )
                    kept.append(ScoredItem(item=item, verdict=verdict))
                else:
                    self._log.info(
                        "[%d/%d] skip %d/10 r/%s | %s",
                        done,
                        len(items),
                        verdict.score,
                        item.subreddit,
                        item.title[:45],
                    )
                self._heartbeat.tick(done, len(items), len(kept))

        kept.sort(key=lambda scored: scored.score, reverse=True)
        return kept

    def run(self, now: float | None = None) -> list[ScoredItem]:
        """Execute a full pass.

        Args:
            now: POSIX timestamp treated as the current time. Defaults to now.

        Returns:
            Ranked opportunities meeting the score threshold; possibly empty.
        """
        moment = time.time() if now is None else now
        return self.score_all(self.collect(moment), moment)
