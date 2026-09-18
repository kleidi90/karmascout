"""Console entry point for KarmaScout.

Installed as the ``karmascout`` command. Running it with no arguments performs two
collection passes, scores the results concurrently, writes an HTML report in the
working directory, and opens it in the default browser.
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from karmascout import __version__
from karmascout.cache import CachingScorer, VerdictCache
from karmascout.config import Settings
from karmascout.dryrun import OfflineRedditClient, OfflineScorer
from karmascout.logging_setup import configure_logging
from karmascout.pipeline import Pipeline, format_elapsed
from karmascout.reddit_client import RedditClient, RedditSource
from karmascout.report import write_report
from karmascout.scoring import Scorer, ScoreSource

#: Placeholder key used in dry-run mode so that a real key is not required offline.
DRY_RUN_KEY = "dry-run-no-network"


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="karmascout",
        description=(
            "Find German noise-dispute threads on Reddit, score them with an LLM, and "
            "draft comments for you to post manually. Read-only: never posts or votes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"karmascout {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the full pipeline offline against fixture data. Makes no Reddit or "
            "OpenRouter requests, needs no API key, and does not open a browser."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write the HTML report (default: karmascout_results.html).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Write the report but do not open it in a browser.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Re-score every thread from scratch, ignoring verdicts cached by an "
            "earlier run. Costs OpenRouter credit for threads already paid for."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Logging verbosity (default: from KARMASCOUT_LOG_LEVEL, else INFO).",
    )
    return parser


def load_settings(args: argparse.Namespace) -> Settings:
    """Load configuration from the environment, applying command line overrides.

    Args:
        args: Parsed command line arguments.

    Returns:
        The effective settings for this run.

    Raises:
        ValidationError: If required configuration is missing or invalid.
    """
    overrides: dict[str, object] = {}
    if args.dry_run:
        # Dry run makes no API calls, so a real key must not be required.
        overrides["openrouter_api_key"] = DRY_RUN_KEY
    if args.output is not None:
        overrides["output_path"] = args.output
    if args.log_level is not None:
        overrides["log_level"] = args.log_level
    if args.no_cache:
        overrides["cache_enabled"] = False
    return Settings(**overrides)  # type: ignore[arg-type]


def main(argv: Sequence[str] | None = None) -> int:
    """Run KarmaScout.

    Args:
        argv: Command line arguments, excluding the program name. Defaults to
            :data:`sys.argv`.

    Returns:
        Process exit code: ``0`` on success, ``1`` on a configuration error, ``2``
        when the run completed but produced no results.
    """
    args = build_parser().parse_args(argv)
    started = time.time()

    try:
        settings = load_settings(args)
    except ValidationError as exc:
        # Printed rather than logged: logging is not configured yet, and the message
        # is a setup instruction, not run output.
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 1

    log = configure_logging(settings.log_level, start_time=started)
    log.info("KarmaScout %s - %s", __version__, datetime.now().strftime("%A, %d %B %Y - %H:%M"))
    log.info(
        "Karma: %d | Account age: %d days | Model: %s",
        settings.account_karma,
        settings.account_age_days,
        settings.openrouter_model,
    )
    # Deliberately says nothing about the key itself: an exact length is a small
    # but real oracle, and confirming presence is all this line is for.
    log.debug("OpenRouter key loaded.")

    if args.dry_run:
        client: RedditSource = OfflineRedditClient(settings)
        scorer: ScoreSource = OfflineScorer(settings)
    else:
        client = RedditClient(settings)
        scorer = Scorer(settings)

    # A dry run's verdicts are canned, so caching them would only write a file
    # that teaches a later real run the wrong answers.
    cache: VerdictCache | None = None
    if settings.cache_enabled and not args.dry_run:
        cache = VerdictCache(settings.cache_path, settings.max_age_seconds)
        cache.load()
        scorer = CachingScorer(scorer, cache)

    scored = Pipeline(settings, client, scorer).run()

    if cache is not None:
        log.info("Verdict cache answered %d threads without an OpenRouter call.", cache.hits)
        cache.save(time.time())

    if not scored:
        log.warning(
            "No results met the minimum score of %d. Try again later or adjust keywords.",
            settings.min_score,
        )
        return 2

    log.info("%d opportunities found (score >= %d):", len(scored), settings.min_score)
    for entry in scored:
        log.info(
            "  %d/10 | r/%s | %dh ago | %s | %s",
            entry.score,
            entry.item.subreddit,
            entry.item.age_hours(),
            entry.item.title,
            entry.item.url,
        )

    run_time = datetime.now().strftime("%A, %d %B %Y - %H:%M")
    output_path = write_report(scored, run_time, settings, settings.output_path)
    log.info("Report written to %s", output_path)

    if not args.no_browser and not args.dry_run:
        log.info("Opening results UI...")
        webbrowser.open(output_path.as_uri())

    log.info(
        "Done in %s! %d opportunities ready to comment on.",
        format_elapsed(time.time() - started),
        len(scored),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
