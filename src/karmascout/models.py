"""Typed models for every structure that crosses a module boundary.

Fetching, scoring, and rendering share these models rather than loose ``dict``
objects, so the schema between them is explicit and checked. They also validate the
one genuinely untrusted input: the JSON an LLM returns.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RedditItem(BaseModel):
    """A single Reddit thread discovered by the fetcher.

    Attributes:
        item_type: Kind of entity. Always ``"THREAD"`` today; reserved for comments.
        keyword: The configured keyword that matched this thread's text, or a
            placeholder such as ``"search"`` when the match came from the search
            index rather than from local text matching.
        subreddit: Subreddit name without the ``r/`` prefix.
        title: Thread title.
        body: First 300 characters of the thread body, HTML stripped.
        url: Permalink, used as the deduplication key.
        created_utc: Creation time as a POSIX timestamp.
    """

    model_config = ConfigDict(frozen=True)

    item_type: str = "THREAD"
    keyword: str = ""
    subreddit: str
    title: str = ""
    body: str = ""
    url: str
    created_utc: float

    @property
    def created_at(self) -> datetime:
        """Creation time as a timezone-aware UTC datetime."""
        return datetime.fromtimestamp(self.created_utc, tz=UTC)

    def age_hours(self, now: float | None = None) -> int:
        """Age of the thread in whole hours, rounded.

        Args:
            now: POSIX timestamp to measure against. Defaults to the current time.

        Returns:
            The thread's age in hours.
        """
        reference = time.time() if now is None else now
        return round((reference - self.created_utc) / 3600)


class AiVerdict(BaseModel):
    """The LLM's assessment of one thread.

    This is the only model built from untrusted input, so it coerces rather than
    trusts: models routinely return ``"7"`` where ``7`` was asked for, and routinely
    omit optional fields entirely. Indexing the keys directly would crash on a
    partial response, after all the fetching work had already been paid for.
    """

    score: int = Field(default=0, ge=0, le=10)
    score_reason: str = ""
    risk: str = "none"
    comment_a: str = ""
    comment_b: str = ""

    @field_validator("score", mode="before")
    @classmethod
    def _coerce_score(cls, value: object) -> int:
        """Accept ``7``, ``7.0``, ``"7"``, or ``"7/10"``; clamp to the 0-10 range."""
        if value is None:
            return 0
        if isinstance(value, bool):
            return 0
        if isinstance(value, int | float):
            number = float(value)
        else:
            text = str(value).strip().split("/")[0].strip()
            try:
                number = float(text)
            except ValueError:
                return 0
        return max(0, min(10, round(number)))

    @field_validator("score_reason", "risk", "comment_a", "comment_b", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        """Tolerate ``None`` and non-string scalars where text was expected."""
        if value is None:
            return ""
        return str(value)


class ScoredItem(BaseModel):
    """A Reddit thread paired with the LLM verdict for it."""

    model_config = ConfigDict(frozen=True)

    item: RedditItem
    verdict: AiVerdict

    @property
    def score(self) -> int:
        """Convenience accessor for the verdict score, used for sorting."""
        return self.verdict.score
