"""The only module in KarmaScout that talks to Reddit.

Reddit closed self-serve API app creation under its Responsible Builder Policy, so
OAuth is not available to this project. Access is therefore limited to the public
Atom (``.rss``) endpoints, fetched with plain HTTP and parsed by ``defusedxml``.
The standard library ``ElementTree`` is imported for its element *types* only;
every parse of remote content goes through ``defusedxml``, which rejects the entity
expansion and external-entity tricks a hostile feed could otherwise use.

Everything network-facing is confined here so that the rest of the package can be
tested without touching the network: tests construct a :class:`RedditClient` with a
stub session, or patch its fetch methods directly.

This client is strictly read-only. It has no code path that posts, comments, or votes.
"""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from typing import Final, Protocol
from urllib.parse import quote

import defusedxml.ElementTree as DefusedET
import requests
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from karmascout.config import Settings
from karmascout.logging_setup import get_logger
from karmascout.models import RedditItem

#: Atom namespace map used for every ``findtext`` / ``find`` call below.
NS: Final[dict[str, str]] = {"atom": "http://www.w3.org/2005/Atom"}

#: Maximum characters of thread body kept.
BODY_CHARS: Final[int] = 300

#: Base URL for every Reddit request.
BASE_URL: Final[str] = "https://www.reddit.com"

_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")


class RedditFetchError(RuntimeError):
    """Raised when a Reddit request fails in a way that is worth retrying."""


def strip_html(html_text: str | None) -> str:
    """Remove HTML tags from ``html_text``, unescape entities, and trim whitespace.

    Reddit's Atom ``<content>`` is double-escaped: ElementTree undoes one layer, so
    without the second unescape here an ampersand arrives as a literal ``&amp;`` and
    a quotation mark as ``&quot;``. Those then reach the model as noise and reach
    the report as visible junk, because Jinja escapes the bare ``&`` a second time.

    Unescaping last can reveal text shaped like a tag, which is why the report
    relies on Jinja's autoescaping rather than on this function for its safety.

    Args:
        html_text: Raw HTML, or ``None``.

    Returns:
        The tag-free, entity-decoded text, or an empty string when the input was
        empty.
    """
    return html.unescape(_TAG_RE.sub(" ", html_text or "")).strip()


def clean_text(text: str | None) -> str:
    """Unescape entities in a plain-text Atom field such as a title.

    Args:
        text: The field value, or ``None``.

    Returns:
        The entity-decoded, trimmed text.
    """
    return html.unescape(text or "").strip()


class RedditSource(Protocol):
    """The collection capability :class:`~karmascout.pipeline.Pipeline` depends on.

    Both :class:`RedditClient` and the offline stand-in implement this
    independently, so the offline one cannot silently inherit a live network method
    that is added here later.
    """

    def search_subreddit(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        """Return fresh threads matching the configured keywords."""
        ...

    def scan_feeds(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        """Return fresh threads from the configured feeds, matched locally."""
        ...


def parse_rss_time(timestamp: str) -> float:
    """Parse an Atom timestamp into a POSIX timestamp.

    Args:
        timestamp: An ISO 8601 string as Reddit emits it, e.g.
            ``"2026-09-17T08:14:22+00:00"``.

    Returns:
        The instant as a POSIX timestamp.

    Raises:
        ValueError: If the string is unparseable, or carries no UTC offset. A naive
            timestamp would otherwise be silently read as local time, corrupting
            every age and freshness calculation.
    """
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        msg = f"Reddit timestamp {timestamp!r} has no UTC offset; refusing to guess a timezone"
        raise ValueError(msg)
    return parsed.timestamp()


def build_combined_query(keywords: Sequence[str]) -> str:
    """Combine every keyword into a single Reddit search query.

    Multi-word keywords keep loose "both words present" (``AND``) semantics rather
    than exact-phrase semantics, so recall matches a keyword-at-a-time search.

    Args:
        keywords: The configured keyword list.

    Returns:
        A query string such as ``"Ruhestoerung OR (laute AND Nachbarn)"``.
    """

    def clause(keyword: str) -> str:
        words = keyword.split()
        return words[0] if len(words) == 1 else "(" + " AND ".join(words) + ")"

    return " OR ".join(clause(keyword) for keyword in keywords)


def entry_to_item(
    entry: ET.Element,
    keyword: str,
    subreddit: str,
    item_type: str = "THREAD",
) -> RedditItem | None:
    """Convert one Atom ``<entry>`` element into a :class:`RedditItem`.

    Args:
        entry: The Atom entry element.
        keyword: Keyword to record against the item.
        subreddit: Subreddit the entry came from.
        item_type: Entity kind; always ``"THREAD"`` today.

    Returns:
        The parsed item, or ``None`` if the entry carries no usable timestamp.
    """
    raw_timestamp = entry.findtext("atom:updated", namespaces=NS) or entry.findtext(
        "atom:published", namespaces=NS
    )
    if not raw_timestamp:
        return None
    try:
        created = parse_rss_time(raw_timestamp)
    except ValueError:
        get_logger().warning("Skipping entry with unparseable timestamp %r", raw_timestamp)
        return None

    content_element = entry.find("atom:content", NS)
    link_element = entry.find("atom:link", NS)

    return RedditItem(
        item_type=item_type,
        keyword=keyword,
        subreddit=subreddit,
        title=clean_text(entry.findtext("atom:title", namespaces=NS)),
        body=strip_html(content_element.text if content_element is not None else "")[:BODY_CHARS],
        url=link_element.get("href", "") if link_element is not None else "",
        created_utc=created,
    )


def match_keyword(item: RedditItem, keywords: Sequence[str]) -> str | None:
    """Return the first configured keyword present in the item's title or body.

    Matching is case-insensitive substring matching.

    Args:
        item: The thread to test.
        keywords: Keywords to look for, in priority order.

    Returns:
        The matching keyword, or ``None`` when nothing matched.
    """
    text = f"{item.title} {item.body}".lower()
    return next((keyword for keyword in keywords if keyword.lower() in text), None)


class RedditClient:
    """Read-only client for Reddit's public Atom feeds.

    Args:
        settings: Configuration supplying pacing, retries, and the user agent.
        session: HTTP session to use. A fresh :class:`requests.Session` is created
            when omitted; tests pass a stub.
        sleep: Callable used for inter-request pacing. Tests pass a no-op.
    """

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings
        self._session = session if session is not None else requests.Session()
        self._sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._log: logging.Logger = get_logger()

    # ── HTTP ─────────────────────────────────────────────────────────────────

    @property
    def headers(self) -> dict[str, str]:
        """Headers sent with every Reddit request."""
        return {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/atom+xml, application/xml, */*",
        }

    def _log_retry(self, state: RetryCallState) -> None:
        """Log each retry attempt with the delay tenacity has chosen."""
        delay = getattr(state.next_action, "sleep", 0.0)
        self._log.warning(
            "Reddit request failed (attempt %d/%d), retrying in %.1fs",
            state.attempt_number,
            self._settings.reddit_retries,
            delay,
        )

    def fetch_xml(self, url: str) -> ET.Element | None:
        """Fetch ``url`` and parse the response as XML, retrying transient failures.

        Retries with exponential backoff on HTTP 429, on 5xx responses, and on
        network errors. A 4xx other than 429 is permanent and is not retried.

        Args:
            url: Absolute URL of an Atom feed.

        Returns:
            The parsed root element, or ``None`` when every attempt failed or the
            server returned a permanent error.
        """
        retrying = Retrying(
            stop=stop_after_attempt(self._settings.reddit_retries),
            wait=wait_exponential(multiplier=self._settings.rate_limit_wait, max=120),
            retry=retry_if_exception_type(RedditFetchError),
            before_sleep=self._log_retry,
            reraise=True,
            sleep=self._sleep,
        )
        root: ET.Element | None = None
        try:
            for attempt in retrying:
                with attempt:
                    root = self._fetch_xml_once(url)
        except RedditFetchError as exc:
            self._log.error("Giving up on %s: %s", url, exc)
            return None
        except ET.ParseError as exc:
            self._log.error("Malformed XML from %s: %s", url, exc)
            return None
        return root

    def _fetch_xml_once(self, url: str) -> ET.Element | None:
        """Perform a single fetch attempt, raising :class:`RedditFetchError` to retry.

        Args:
            url: Absolute URL of an Atom feed.

        Returns:
            The parsed root element, or ``None`` on a permanent client error.

        Raises:
            RedditFetchError: On a network error, HTTP 429, or a 5xx response.
        """
        try:
            response = self._session.get(
                url, headers=self.headers, timeout=self._settings.reddit_timeout
            )
        except requests.RequestException as exc:
            raise RedditFetchError(f"network error: {exc}") from exc

        if response.status_code == 200:
            # defusedxml guards against entity-expansion and external-entity attacks
            # in feed content we do not control.
            parsed: ET.Element = DefusedET.fromstring(response.text)
            return parsed
        if response.status_code == 429:
            raise RedditFetchError("rate limited (HTTP 429)")
        if response.status_code >= 500:
            raise RedditFetchError(f"server error (HTTP {response.status_code})")

        self._log.warning("HTTP %s for %s", response.status_code, url)
        return None

    def _pace(self) -> None:
        """Sleep between Reddit requests to stay under the per-IP throttle."""
        if self._settings.fetch_delay > 0:
            self._sleep(self._settings.fetch_delay)

    # ── Pass 1: keyword search ───────────────────────────────────────────────

    def search_url(self, subreddit: str) -> str:
        """Build the ``search.rss`` URL used for ``subreddit``.

        Exposed separately so tests can assert on the exact query string without
        performing a request.

        Args:
            subreddit: Subreddit name without the ``r/`` prefix.

        Returns:
            The fully-qualified search URL.
        """
        query = build_combined_query(self._settings.keywords)
        return (
            f"{BASE_URL}/r/{subreddit}/search.rss"
            f"?q={quote(query)}&restrict_sr=1&sort=new&limit={self._settings.feed_limit}"
        )

    def search_subreddit(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        """Search one subreddit for every configured keyword in a single request.

        One OR-combined query per subreddit replaces one request per
        subreddit-keyword pair. Reddit throttles ``search.rss`` per IP, so making
        fewer requests is the only effective speed lever.

        Args:
            subreddit: Subreddit name without the ``r/`` prefix.
            cutoff: POSIX timestamp; threads older than this are discarded.

        Returns:
            Fresh matching threads, with the matching keyword recorded on each.
        """
        keywords = self._settings.keywords
        self._log.info("Searching r/%s (all %d keywords in one query)...", subreddit, len(keywords))

        # Pacing is in a finally block because the failure paths are exactly the ones
        # that must not skip it: a request that 429s has to be followed by *more*
        # delay, not less.
        try:
            root = self.fetch_xml(self.search_url(subreddit))
            if root is None:
                self._log.warning("Search failed for r/%s", subreddit)
                return []

            entries = root.findall(".//atom:entry", NS)
            results = [
                item.model_copy(update={"keyword": match_keyword(item, keywords) or "search"})
                for item in self._parse_entries(entries, subreddit, cutoff)
            ]
            self._log.info(
                "r/%s: %d fetched, %d within %dh",
                subreddit,
                len(entries),
                len(results),
                self._settings.max_age_hours,
            )
            return results
        finally:
            self._pace()

    # ── Pass 2: direct feed scan ─────────────────────────────────────────────

    def feed_url(self, subreddit: str, feed_sort: str) -> str:
        """Build the feed URL for ``subreddit`` and ``feed_sort``.

        Args:
            subreddit: Subreddit name without the ``r/`` prefix.
            feed_sort: Feed sort, e.g. ``"new"`` or ``"hot"``.

        Returns:
            The fully-qualified feed URL.
        """
        return f"{BASE_URL}/r/{subreddit}/{feed_sort}.rss?limit={self._settings.feed_limit}"

    def scan_feeds(self, subreddit: str, cutoff: float) -> list[RedditItem]:
        """Scan a subreddit's configured feeds, matching keywords locally.

        This catches brand-new threads that the search index has not picked up yet.

        Args:
            subreddit: Subreddit name without the ``r/`` prefix.
            cutoff: POSIX timestamp; threads older than this are discarded.

        Returns:
            Fresh threads whose title or body contains a configured keyword.
        """
        keywords = self._settings.keywords
        results: list[RedditItem] = []

        for feed_sort in self._settings.feed_sorts:
            # As in search_subreddit: a failed fetch must still pay the delay before
            # the next request, otherwise a rate-limited run speeds up.
            try:
                root = self.fetch_xml(self.feed_url(subreddit, feed_sort))
                if root is None:
                    self._log.warning("Could not fetch %s feed for r/%s", feed_sort, subreddit)
                    continue

                entries = root.findall(".//atom:entry", NS)
                self._log.info("Feed %s r/%s: %d posts fetched", feed_sort, subreddit, len(entries))

                for item in self._parse_entries(entries, subreddit, cutoff):
                    matched = match_keyword(item, keywords)
                    if matched is None:
                        continue
                    matched_item = item.model_copy(update={"keyword": matched})
                    self._log.info(
                        "MATCH [%dh] %r - %s",
                        matched_item.age_hours(),
                        matched,
                        matched_item.title[:60],
                    )
                    results.append(matched_item)
            finally:
                self._pace()
        return results

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _parse_entries(
        self, entries: Sequence[ET.Element], subreddit: str, cutoff: float
    ) -> Iterator[RedditItem]:
        """Yield fresh items from ``entries``, dropping anything older than ``cutoff``.

        Args:
            entries: Atom entry elements.
            subreddit: Subreddit the entries came from.
            cutoff: POSIX timestamp below which items are discarded.

        Yields:
            Each parsed, in-window item.
        """
        for entry in entries:
            item = entry_to_item(entry, "", subreddit)
            if item is None or item.created_utc < cutoff:
                continue
            yield item
