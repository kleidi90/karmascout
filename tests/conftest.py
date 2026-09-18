"""Shared fixtures.

No test in this suite touches the network. The ``no_network`` autouse fixture makes
that a hard guarantee rather than a convention: any code path that reaches
``requests`` fails the test loudly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from karmascout.config import DEFAULT_KEYWORDS, DEFAULT_SUBREDDITS, Settings
from karmascout.logging_setup import configure_logging
from karmascout.models import AiVerdict, RedditItem, ScoredItem

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: Fixed "now" for every test, so ages and cutoffs are deterministic.
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC).timestamp()


class NetworkAccessError(AssertionError):
    """Raised when a test attempts a real network call."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to open a real connection."""

    def blocked(*args: object, **kwargs: object) -> None:
        msg = f"test attempted a network call: {args!r} {kwargs!r}"
        raise NetworkAccessError(msg)

    monkeypatch.setattr(requests.Session, "request", blocked)
    monkeypatch.setattr(requests.Session, "send", blocked)


@pytest.fixture(autouse=True)
def propagating_logger() -> Iterator[None]:
    """Let ``caplog`` see KarmaScout log records.

    The application logger deliberately does not propagate, so that a host
    application's root handler cannot duplicate or reformat its output. Tests
    re-enable propagation to observe it.
    """
    logger = configure_logging("DEBUG")
    logger.propagate = True
    yield
    logger.propagate = False


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide every KARMASCOUT_ variable from the tests.

    Without this, a developer's own ``.env`` or shell environment silently changes
    what the suite asserts - a real run of this project once set
    ``KARMASCOUT_MAX_AGE_HOURS`` and broke an unrelated fetching test.
    """
    for name in list(os.environ):
        if name.startswith("KARMASCOUT_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Settings with the project defaults and a dummy API key.

    ``_env_file=None`` keeps the developer's real ``.env`` out of the test run.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        openrouter_api_key=SecretStr("sk-or-v1-testkey"),
        subreddits=DEFAULT_SUBREDDITS,
        keywords=DEFAULT_KEYWORDS,
        max_age_hours=24,
        min_score=7,
        fetch_delay=0.0,
        heartbeat_interval=0.0,
        reddit_contact="tester",
    )


@pytest.fixture
def item() -> RedditItem:
    """One in-window thread, four hours old relative to :data:`NOW`."""
    return RedditItem(
        keyword="Ruhestörung",
        subreddit="de",
        title="Nachbarn feiern wieder bis 3 Uhr",
        body="Jede Woche dasselbe. Was kann ich machen?",
        url="https://www.reddit.com/r/de/comments/abc123/",
        created_utc=NOW - 4 * 3600,
    )


@pytest.fixture
def verdict() -> AiVerdict:
    """A plausible model verdict."""
    return AiVerdict(
        score=9,
        score_reason="Frisch, konversationell, kein politisches Risiko.",
        risk="none",
        comment_a="Führ am besten ein Lärmprotokoll, das hilft später enorm.",
        comment_b="Schon mal schriftlich beim Vermieter gemeldet?",
    )


@pytest.fixture
def scored(item: RedditItem, verdict: AiVerdict) -> ScoredItem:
    """A scored thread ready for rendering."""
    return ScoredItem(item=item, verdict=verdict)


def atom_entry(
    title: str,
    body: str,
    url: str,
    age_hours: float,
    *,
    timestamp: str | None = None,
) -> str:
    """Build one Atom ``<entry>`` in the shape Reddit emits.

    Args:
        title: Entry title.
        body: HTML content of the entry.
        url: Permalink.
        age_hours: Age relative to :data:`NOW`, used when ``timestamp`` is omitted.
        timestamp: Explicit timestamp string, for malformed-input tests.

    Returns:
        The XML fragment.
    """
    stamp = timestamp
    if stamp is None:
        moment = datetime.fromtimestamp(NOW, tz=UTC) - timedelta(hours=age_hours)
        stamp = moment.isoformat()
    return (
        "<entry>"
        f"<title>{title}</title>"
        f"<updated>{stamp}</updated>"
        f'<link href="{url}"/>'
        f'<content type="html">{body}</content>'
        "</entry>"
    )


def atom_feed(*entries: str) -> str:
    """Wrap ``entries`` in an Atom feed document."""
    joined = "".join(entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{joined}</feed>'


class StubResponse:
    """Minimal stand-in for :class:`requests.Response`."""

    def __init__(self, status_code: int = 200, text: str = "", payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        """Return the canned JSON payload."""
        return self._payload


class StubSession:
    """Records requests and replays queued responses, without any I/O."""

    def __init__(self, responses: list[StubResponse] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> StubResponse:
        if not self.responses:
            return StubResponse(200, atom_feed())
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        """Record a GET and return the next queued response."""
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._next()

    def post(self, url: str, **kwargs: Any) -> StubResponse:
        """Record a POST and return the next queued response."""
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next()


@pytest.fixture
def stub_session() -> Iterator[StubSession]:
    """An empty stub session; queue responses on ``.responses``."""
    yield StubSession()


def no_sleep(_seconds: float) -> None:
    """Sleep replacement that returns immediately."""
    return None
