"""Persistent cache of AI verdicts, keyed by thread permalink.

Deduplication inside :mod:`karmascout.pipeline` is per-run only, so consecutive
runs re-score every thread still inside the freshness window. With
``max_age_hours`` set to a week that is the same thread paid for again on every
run, which is the single largest avoidable OpenRouter cost in the project.

The cache stores the verdict rather than merely remembering that a thread was
seen. A cached thread therefore still appears in the report exactly as before -
same score, same drafted comments - it just costs nothing the second time. That
keeps the cache invisible in the output and makes it safe to leave on.

Entries expire once the thread they describe falls out of the freshness window,
since such a thread can never be collected again.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Final

from karmascout.logging_setup import get_logger
from karmascout.models import AiVerdict, RedditItem
from karmascout.scoring import ScoreSource

#: Version tag written into the file, so a future format change can be detected
#: and the old file discarded rather than misread.
CACHE_VERSION: Final[int] = 1


class VerdictCache:
    """A JSON-file cache mapping thread URL to the verdict last computed for it.

    The instance is shared across scoring threads, so every mutation is guarded by
    a lock.

    Args:
        path: File to read and write. Its parent is created on save.
        max_age_seconds: Freshness window; entries for threads older than this are
            dropped on save, because they can no longer be collected.
    """

    def __init__(self, path: Path, max_age_seconds: int) -> None:
        self._path = path
        self._max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._hits = 0
        self._log: logging.Logger = get_logger()

    @property
    def hits(self) -> int:
        """How many scoring calls were served from the cache this run."""
        return self._hits

    def load(self) -> None:
        """Read the cache file, tolerating a missing, unreadable, or stale one.

        A cache is an optimisation, never a source of truth, so every failure mode
        degrades to an empty cache and a warning rather than to an error.
        """
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log.warning("Ignoring unreadable verdict cache %s: %s", self._path, exc)
            return

        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            self._log.info("Discarding verdict cache %s: unrecognised format.", self._path)
            return

        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        self._entries = {url: entry for url, entry in entries.items() if isinstance(entry, dict)}
        self._log.info("Loaded %d cached verdicts from %s", len(self._entries), self._path)

    def get(self, item: RedditItem) -> AiVerdict | None:
        """Return the stored verdict for ``item``, or ``None`` when absent.

        A stored entry that no longer parses as an :class:`AiVerdict` is treated as
        a miss, so a format change cannot poison a run.

        Args:
            item: The thread to look up.

        Returns:
            The cached verdict, or ``None``.
        """
        with self._lock:
            entry = self._entries.get(item.url)
        if entry is None:
            return None
        try:
            verdict = AiVerdict.model_validate(entry["verdict"])
        except (KeyError, ValueError):
            return None
        with self._lock:
            self._hits += 1
        return verdict

    def put(self, item: RedditItem, verdict: AiVerdict) -> None:
        """Store ``verdict`` against ``item``.

        Args:
            item: The thread that was scored.
            verdict: The verdict to remember.
        """
        with self._lock:
            self._entries[item.url] = {
                "created_utc": item.created_utc,
                "verdict": verdict.model_dump(),
            }

    def save(self, now: float) -> None:
        """Write the cache out, dropping entries that can no longer be collected.

        Args:
            now: POSIX timestamp treated as the current time.
        """
        cutoff = now - self._max_age_seconds
        with self._lock:
            live = {
                url: entry
                for url, entry in self._entries.items()
                if float(entry.get("created_utc", 0.0)) >= cutoff
            }
        payload = {"version": CACHE_VERSION, "entries": live}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            # A cache that cannot be written must not fail a run that already
            # produced its report.
            self._log.warning("Could not write verdict cache %s: %s", self._path, exc)
            return
        self._log.info("Cached %d verdicts to %s", len(live), self._path)


class CachingScorer:
    """A :class:`~karmascout.scoring.ScoreSource` that consults a cache first.

    Wraps any other scorer, so the real and offline scorers are both cacheable and
    neither needs to know the cache exists.

    Args:
        inner: The scorer to fall back to on a miss.
        cache: The verdict store.
    """

    def __init__(self, inner: ScoreSource, cache: VerdictCache) -> None:
        self._inner = inner
        self._cache = cache

    def score(self, item: RedditItem, now: float | None = None) -> AiVerdict | None:
        """Return the cached verdict for ``item``, else score it and remember that.

        A ``None`` result is never cached: it means the model could not be reached
        or understood, which is a transient condition worth retrying next run.

        Args:
            item: The thread to score.
            now: POSIX timestamp used to compute the thread's age.

        Returns:
            The verdict, or ``None``.
        """
        cached = self._cache.get(item)
        if cached is not None:
            return cached
        verdict = self._inner.score(item, now)
        if verdict is not None:
            self._cache.put(item, verdict)
        return verdict
