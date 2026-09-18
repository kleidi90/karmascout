"""Characterisation tests for Reddit access.

These pin what the program finds - URL shape, query construction, keyword matching,
and the freshness cutoff - so no change can quietly alter it.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from karmascout.config import Settings
from karmascout.models import RedditItem
from karmascout.reddit_client import (
    BODY_CHARS,
    RedditClient,
    build_combined_query,
    clean_text,
    match_keyword,
    parse_rss_time,
    strip_html,
)
from tests.conftest import NOW, StubResponse, StubSession, atom_entry, atom_feed, no_sleep


def make_client(settings: Settings, session: StubSession) -> RedditClient:
    """Build a client backed by a stub session and an instant sleep."""
    return RedditClient(settings, session=cast(requests.Session, session), sleep=no_sleep)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_strip_html_removes_tags_and_trims() -> None:
    assert strip_html("<p>Hallo <b>Welt</b></p>") == "Hallo  Welt"


def test_strip_html_handles_none() -> None:
    assert strip_html(None) == ""


def test_parse_rss_time_accepts_offset_aware_timestamps() -> None:
    assert parse_rss_time("2026-09-17T12:00:00+00:00") == NOW


def test_parse_rss_time_rejects_naive_timestamps() -> None:
    """A naive stamp would be read as local time, silently corrupting every age."""
    with pytest.raises(ValueError, match="no UTC offset"):
        parse_rss_time("2026-09-17T12:00:00")


def test_combined_query_matches_original_semantics() -> None:
    """Single words stay bare; multi-word keywords become AND groups joined by OR."""
    query = build_combined_query(["Ruhestörung", "laute Nachbarn", "Polizei Lärm Nachbarn"])
    assert query == "Ruhestörung OR (laute AND Nachbarn) OR (Polizei AND Lärm AND Nachbarn)"


def test_combined_query_covers_every_default_keyword(settings: Settings) -> None:
    query = build_combined_query(settings.keywords)
    assert query.count(" OR ") == len(settings.keywords) - 1


def test_match_keyword_is_case_insensitive(settings: Settings) -> None:
    thread = RedditItem(
        subreddit="de", url="u", created_utc=NOW, title="RUHESTÖRUNG im Haus", body=""
    )
    assert match_keyword(thread, settings.keywords) == "Ruhestörung"


def test_match_keyword_returns_none_when_nothing_matches(settings: Settings) -> None:
    thread = RedditItem(subreddit="de", url="u", created_utc=NOW, title="Kuchenrezept", body="")
    assert match_keyword(thread, settings.keywords) is None


# ── URL construction ─────────────────────────────────────────────────────────


def test_search_url_shape(settings: Settings, stub_session: StubSession) -> None:
    url = make_client(settings, stub_session).search_url("de")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "www.reddit.com"
    assert parsed.path == "/r/de/search.rss"
    assert params["restrict_sr"] == ["1"]
    assert params["sort"] == ["new"]
    assert params["limit"] == ["100"]
    assert params["q"] == [build_combined_query(settings.keywords)]


def test_feed_url_shape(settings: Settings, stub_session: StubSession) -> None:
    client = make_client(settings, stub_session)
    assert client.feed_url("de", "new") == "https://www.reddit.com/r/de/new.rss?limit=100"


def test_user_agent_identifies_the_app_and_a_contact(settings: Settings) -> None:
    assert settings.user_agent.startswith("python:karmascout:v")
    assert "by /u/tester" in settings.user_agent


# ── Fetching ─────────────────────────────────────────────────────────────────


def test_search_keeps_only_fresh_threads(settings: Settings) -> None:
    feed = atom_feed(
        atom_entry("Ruhestörung nebenan", "Hilfe", "https://r.example/1", age_hours=2),
        atom_entry("Ruhestörung letzte Woche", "alt", "https://r.example/2", age_hours=40),
    )
    session = StubSession([StubResponse(200, feed)])
    results = make_client(settings, session).search_subreddit("de", NOW - settings.max_age_seconds)
    assert [entry.url for entry in results] == ["https://r.example/1"]


def test_search_records_the_matching_keyword(settings: Settings) -> None:
    feed = atom_feed(atom_entry("Trittschall im Altbau", "", "https://r.example/1", age_hours=3))
    session = StubSession([StubResponse(200, feed)])
    results = make_client(settings, session).search_subreddit("de", NOW - settings.max_age_seconds)
    assert results[0].keyword == "Trittschall"


def test_search_falls_back_to_the_search_placeholder(settings: Settings) -> None:
    """A hit whose keyword is not visible in the text is marked, not dropped."""
    feed = atom_feed(atom_entry("Etwas anderes", "", "https://r.example/1", age_hours=3))
    session = StubSession([StubResponse(200, feed)])
    results = make_client(settings, session).search_subreddit("de", NOW - settings.max_age_seconds)
    assert results[0].keyword == "search"


def test_feed_scan_requires_a_local_keyword_match(settings: Settings) -> None:
    feed = atom_feed(
        atom_entry("Ruhestörung nebenan", "", "https://r.example/1", age_hours=2),
        atom_entry("Bester Döner in Berlin", "", "https://r.example/2", age_hours=2),
    )
    session = StubSession([StubResponse(200, feed)])
    results = make_client(settings, session).scan_feeds("de", NOW - settings.max_age_seconds)
    assert {entry.url for entry in results} == {"https://r.example/1"}


def test_feed_scan_visits_every_configured_sort(settings: Settings) -> None:
    session = StubSession([StubResponse(200, atom_feed())])
    make_client(settings, session).scan_feeds("de", NOW - settings.max_age_seconds)
    paths = [urlparse(call["url"]).path for call in session.calls]
    assert paths == ["/r/de/new.rss", "/r/de/hot.rss"]


def test_body_is_truncated_at_the_original_limit(settings: Settings) -> None:
    long_body = "Ruhestörung " + "x" * 500
    feed = atom_feed(atom_entry("Titel", long_body, "https://r.example/1", age_hours=1))
    session = StubSession([StubResponse(200, feed)])
    results = make_client(settings, session).scan_feeds("de", NOW - settings.max_age_seconds)
    assert len(results[0].body) == BODY_CHARS


def test_entry_without_timestamp_is_skipped(settings: Settings) -> None:
    feed = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>Ruhestörung</title><link href="https://r.example/1"/></entry></feed>'
    )
    session = StubSession([StubResponse(200, feed)])
    assert make_client(settings, session).scan_feeds("de", 0) == []


def test_entry_with_naive_timestamp_is_skipped_not_fatal(settings: Settings) -> None:
    feed = atom_feed(
        atom_entry("Ruhestörung", "", "https://r.example/1", 0, timestamp="2026-09-17T12:00:00")
    )
    session = StubSession([StubResponse(200, feed)])
    assert make_client(settings, session).scan_feeds("de", 0) == []


# ── Retry behaviour ──────────────────────────────────────────────────────────


def test_rate_limit_is_retried(settings: Settings) -> None:
    feed = atom_feed(atom_entry("Ruhestörung", "", "https://r.example/1", age_hours=1))
    session = StubSession([StubResponse(429), StubResponse(200, feed)])
    results = make_client(settings, session).search_subreddit("de", NOW - settings.max_age_seconds)
    assert len(session.calls) == 2
    assert len(results) == 1


def test_server_error_is_retried(settings: Settings) -> None:
    """A 503 must cost one request, not a whole subreddit."""
    feed = atom_feed(atom_entry("Ruhestörung", "", "https://r.example/1", age_hours=1))
    session = StubSession([StubResponse(503), StubResponse(200, feed)])
    results = make_client(settings, session).search_subreddit("de", NOW - settings.max_age_seconds)
    assert len(results) == 1


def test_network_error_is_retried(settings: Settings) -> None:
    calls: list[int] = []
    feed = atom_feed(atom_entry("Ruhestörung", "", "https://r.example/1", age_hours=1))

    class FlakySession(StubSession):
        def get(self, url: str, **kwargs: Any) -> StubResponse:
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("boom")
            return StubResponse(200, feed)

    results = make_client(settings, FlakySession()).search_subreddit("de", 0)
    assert len(calls) == 2
    assert len(results) == 1


def test_permanent_client_error_is_not_retried(settings: Settings) -> None:
    session = StubSession([StubResponse(404)])
    assert make_client(settings, session).search_subreddit("de", 0) == []
    assert len(session.calls) == 1


def test_retries_are_capped(settings: Settings) -> None:
    session = StubSession([StubResponse(429), StubResponse(429), StubResponse(429)])
    assert make_client(settings, session).search_subreddit("de", 0) == []
    assert len(session.calls) == settings.reddit_retries


def test_malformed_xml_returns_no_results(settings: Settings) -> None:
    session = StubSession([StubResponse(200, "<not-xml")])
    assert make_client(settings, session).search_subreddit("de", 0) == []


# ── HTML entity decoding ─────────────────────────────────────────────────────


def test_body_entities_are_decoded() -> None:
    """Reddit's content is double-escaped; ElementTree undoes only one layer."""
    assert strip_html("<div>Preis &amp; Kosten &quot;laut&quot;</div>") == 'Preis & Kosten "laut"'


def test_title_entities_are_decoded() -> None:
    assert clean_text("Test &amp; more") == "Test & more"


def test_parsed_entry_carries_decoded_text(settings: Settings, stub_session: StubSession) -> None:
    """End to end: neither the title nor the body may reach a model full of &amp;."""
    # Written as Reddit actually emits it: the HTML inside <content> is escaped, so
    # entities within it are escaped twice.
    feed = atom_feed(
        atom_entry(
            "Ruhestörung &amp; Lärm",
            "&lt;div&gt;Nachbar &amp;quot;feiert&amp;quot; &amp;amp; tobt&lt;/div&gt;",
            "https://r.example/1",
            age_hours=1,
        )
    )
    session = StubSession([StubResponse(200, feed)])
    results = make_client(settings, session).scan_feeds("de", NOW - settings.max_age_seconds)
    assert results[0].title == "Ruhestörung & Lärm"
    assert '"feiert"' in results[0].body
    assert "&amp;" not in results[0].body


# ── Pacing on failure paths ──────────────────────────────────────────────────


def test_failed_search_still_paces(settings: Settings) -> None:
    """A 429 must be followed by more delay, not less: the failure path paced too."""
    slept: list[float] = []
    session = StubSession([StubResponse(429), StubResponse(429), StubResponse(429)])
    paced = settings.model_copy(update={"fetch_delay": 0.8})
    client = RedditClient(
        paced, session=cast(requests.Session, session), sleep=lambda seconds: slept.append(seconds)
    )
    client.search_subreddit("de", 0)
    assert paced.fetch_delay in slept


def test_failed_feed_still_paces(settings: Settings) -> None:
    slept: list[float] = []
    session = StubSession([StubResponse(404)])
    paced = settings.model_copy(update={"fetch_delay": 0.8})
    client = RedditClient(
        paced, session=cast(requests.Session, session), sleep=lambda seconds: slept.append(seconds)
    )
    client.scan_feeds("de", 0)
    assert slept.count(paced.fetch_delay) == len(paced.feed_sorts)
