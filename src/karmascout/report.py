"""HTML report rendering.

The markup lives in ``templates/report.html.j2`` and is rendered by Jinja2 with
autoescaping on, so escaping cannot be forgotten when a field is added.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from karmascout.config import Settings
from karmascout.models import ScoredItem

TEMPLATE_DIR: Final[Path] = Path(__file__).parent / "templates"
TEMPLATE_NAME: Final[str] = "report.html.j2"

#: Maximum characters of thread body shown on a card.
BODY_PREVIEW_CHARS: Final[int] = 200

#: URL schemes allowed in a rendered link. Anything else renders as plain text.
SAFE_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Longest drafted comment shown. The prompt asks for 2-4 sentences, so anything
#: past this is the model having ignored its instructions.
MAX_COMMENT_CHARS: Final[int] = 1000

#: Anything link-shaped inside a drafted comment.
_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:https?|ftp)://|www\.)[^\s<>\"']+", re.IGNORECASE
)


@dataclass(frozen=True)
class Card:
    """One rendered result card.

    All presentation decisions - badge colours, truncation, link safety - are made
    here rather than in the template, so they can be unit tested directly.
    """

    score: int
    badge_color: str
    badge_bg: str
    age_hours: int
    subreddit: str
    keyword: str
    title: str
    body: str
    score_reason: str
    risk: str
    url: str
    safe_url: str
    comment_a: str
    comment_b: str
    draft_links: tuple[str, ...]


def badge_colors(score: int) -> tuple[str, str]:
    """Return the ``(text, background)`` colours for a score badge.

    Args:
        score: The AI score, 0-10.

    Returns:
        A pair of CSS colour strings: green at 8+, amber at 6-7, red below.
    """
    if score >= 8:
        return "#16a34a", "#dcfce7"
    if score >= 6:
        return "#d97706", "#fef3c7"
    return "#dc2626", "#fee2e2"


def safe_link(url: str) -> str:
    """Return ``url`` if it is safe to place in an ``href``, else an empty string.

    Only ``http`` and ``https`` are allowed. This stops a ``javascript:`` URL from
    reaching the page should one ever appear in a feed.

    Args:
        url: The candidate URL.

    Returns:
        The URL, or ``""`` when its scheme is not allowed.
    """
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in SAFE_SCHEMES else ""


def draft_links(*comments: str) -> tuple[str, ...]:
    """Return the distinct links appearing in drafted comments, in order.

    A drafted comment is copied to the clipboard and posted manually under the
    user's own account, so its text is the one model output that leaves the
    machine. The thread text that produced it is attacker-controlled, which makes
    an unexpected link the clearest sign that a thread steered the model. Surfacing
    them on the card puts the decision in front of the user before they copy.

    Args:
        *comments: Drafted comment texts.

    Returns:
        Each distinct link, first occurrence first.
    """
    found = [link for comment in comments for link in _LINK_RE.findall(comment)]
    return tuple(dict.fromkeys(found))


def truncate_comment(comment: str) -> str:
    """Cap a drafted comment at :data:`MAX_COMMENT_CHARS`, adding an ellipsis.

    Args:
        comment: The drafted comment.

    Returns:
        The comment, truncated if it ran past the cap.
    """
    if len(comment) <= MAX_COMMENT_CHARS:
        return comment
    return comment[:MAX_COMMENT_CHARS] + "..."


def truncate_body(body: str) -> str:
    """Trim a thread body to the card preview length, adding an ellipsis if cut.

    Args:
        body: The full stored body text.

    Returns:
        The preview text.
    """
    if len(body) <= BODY_PREVIEW_CHARS:
        return body
    return body[:BODY_PREVIEW_CHARS] + "..."


def build_card(scored: ScoredItem, now: float | None = None) -> Card:
    """Build the presentation model for one scored thread.

    Args:
        scored: The thread and its verdict.
        now: POSIX timestamp used to compute the displayed age.

    Returns:
        The card ready for rendering.
    """
    item, verdict = scored.item, scored.verdict
    color, background = badge_colors(verdict.score)
    return Card(
        score=verdict.score,
        badge_color=color,
        badge_bg=background,
        age_hours=item.age_hours(now),
        subreddit=item.subreddit,
        keyword=item.keyword,
        title=item.title,
        body=truncate_body(item.body),
        score_reason=verdict.score_reason,
        risk="" if verdict.risk in {"", "none"} else verdict.risk,
        url=item.url,
        safe_url=safe_link(item.url),
        comment_a=truncate_comment(verdict.comment_a),
        comment_b=truncate_comment(verdict.comment_b),
        draft_links=draft_links(verdict.comment_a, verdict.comment_b),
    )


def _environment() -> Environment:
    """Build the Jinja2 environment used for rendering, with autoescaping on."""
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render_report(
    scored_items: Sequence[ScoredItem],
    run_time: str,
    settings: Settings,
    now: float | None = None,
) -> str:
    """Render the full results page.

    Args:
        scored_items: Ranked opportunities to display.
        run_time: Human-readable run timestamp shown in the top bar.
        settings: Configuration, for the summary strip.
        now: POSIX timestamp used to compute displayed ages.

    Returns:
        The complete HTML document.
    """
    template = _environment().get_template(TEMPLATE_NAME)
    return template.render(
        run_time=run_time,
        cards=[build_card(scored, now) for scored in scored_items],
        subreddit_labels=[f"r/{name}" for name in settings.subreddits],
        keywords=list(settings.keywords),
        min_score=settings.min_score,
        account_karma=settings.account_karma,
        account_age_days=settings.account_age_days,
    )


def write_report(
    scored_items: Sequence[ScoredItem],
    run_time: str,
    settings: Settings,
    output_path: Path,
    now: float | None = None,
) -> Path:
    """Render the report and write it to ``output_path``.

    Args:
        scored_items: Ranked opportunities to display.
        run_time: Human-readable run timestamp.
        settings: Configuration, for the summary strip.
        output_path: Destination file. Parent directories are created as needed.
        now: POSIX timestamp used to compute displayed ages.

    Returns:
        The resolved path that was written.
    """
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_report(scored_items, run_time, settings, now), encoding="utf-8")
    return resolved
