"""Offline stand-ins for the Reddit and OpenRouter clients.

``karmascout --dry-run`` swaps the real clients for these, which makes no network
calls at all. That exercises the entire pipeline - collection, deduplication,
scoring, filtering, ranking, rendering, writing - against deterministic fixture
data, so a change can be verified without spending OpenRouter credit or touching
Reddit's throttled endpoints.

KarmaScout has no write endpoints, so there is nothing here to guard against
double-posting; dry run exists purely to make a full run safe and free.

These classes implement the ``RedditSource`` and ``ScoreSource`` protocols rather
than subclassing the live clients. Inheriting them would have meant holding a real
``requests.Session`` and inheriting every method that was *not* overridden, so a
new fetch method added to the real client would quietly start reaching the network
during a dry run. Implementing the protocol instead makes mypy report the gap.
"""

from __future__ import annotations

import time
from typing import Final

from karmascout.config import Settings
from karmascout.logging_setup import get_logger
from karmascout.models import AiVerdict, RedditItem

#: Canned threads, one per fixture entry. Ages are relative to run time so that the
#: freshness cutoff always keeps them.
_FIXTURES: Final[tuple[dict[str, str | int], ...]] = (
    {
        "title": "Nachbarn jeden Abend laut - was kann ich tun?",
        "body": "Seit Wochen laute Musik nach 22 Uhr. Ich habe schon geklingelt, hilft nichts.",
        "keyword": "Ruhestörung",
        "age_hours": 4,
        "score": 9,
    },
    {
        "title": "Lärmprotokoll richtig führen?",
        "body": "Der Vermieter will ein Lärmprotokoll. Wie detailliert muss das sein?",
        "keyword": "Lärmprotokoll",
        "age_hours": 11,
        "score": 8,
    },
    {
        "title": "Trittschall in Altbauwohnung - normal?",
        "body": "Man hört jeden Schritt von oben. Ist das noch Zimmerlautstärke?",
        "keyword": "Trittschall",
        "age_hours": 19,
        "score": 6,
    },
)


def _fixture_items(settings: Settings, subreddit: str, now: float) -> list[RedditItem]:
    """Build the canned thread list for one subreddit.

    Args:
        settings: Run configuration, used only for the URL namespace.
        subreddit: Subreddit to attribute the threads to.
        now: POSIX timestamp treated as the current time.

    Returns:
        Deterministic fixture threads.
    """
    del settings
    items: list[RedditItem] = []
    for index, fixture in enumerate(_FIXTURES):
        age_hours = int(fixture["age_hours"])
        items.append(
            RedditItem(
                keyword=str(fixture["keyword"]),
                subreddit=subreddit,
                title=str(fixture["title"]),
                body=str(fixture["body"]),
                url=f"https://www.reddit.com/r/{subreddit}/comments/dryrun{index}/",
                created_utc=now - age_hours * 3600,
            )
        )
    return items


class OfflineRedditClient:
    """A ``RedditSource`` that returns fixtures and holds no HTTP session at all."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._now = time.time()
        get_logger().info("DRY RUN: Reddit access is stubbed; no requests will be made.")

    def search_subreddit(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        """Return fixture threads, once, without contacting Reddit.

        Only the first subreddit yields results, so a dry run produces a small,
        readable report rather than the same fixtures repeated per subreddit.
        """
        del cutoff
        if subreddit != self._settings.subreddits[0]:
            return []
        return _fixture_items(self._settings, subreddit, self._now)

    def scan_feeds(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        """Return nothing; pass 1 fixtures already cover the dry-run dataset."""
        del subreddit, cutoff
        return []


class OfflineScorer:
    """A ``ScoreSource`` that returns fixed verdicts and holds no HTTP session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        get_logger().info("DRY RUN: AI scoring is stubbed; no OpenRouter calls will be made.")

    def score(self, item: RedditItem, now: float | None = None) -> AiVerdict | None:
        """Return the canned verdict matching ``item`` by keyword."""
        del now
        fixture = next(
            (entry for entry in _FIXTURES if entry["keyword"] == item.keyword),
            _FIXTURES[0],
        )
        return AiVerdict(
            score=int(fixture["score"]),
            score_reason="Dry run: canned verdict, no model was called.",
            risk="none",
            comment_a=(
                "Klingt echt anstrengend. Ich würde erstmal ein Lärmprotokoll führen, "
                "das hilft später enorm."
            ),
            comment_b="Hast du das schon schriftlich beim Vermieter gemeldet?",
        )
