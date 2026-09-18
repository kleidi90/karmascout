"""End-to-end tests for the console entry point, including offline dry run."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from karmascout.cli import build_parser, main
from karmascout.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each CLI test in a temp directory with no inherited configuration."""
    for variable in (
        "KARMASCOUT_OPENROUTER_API_KEY",
        "KARMASCOUT_MIN_SCORE",
        "KARMASCOUT_MAX_AGE_HOURS",
        "KARMASCOUT_LOG_LEVEL",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _observable_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable log propagation after ``main`` configures logging, so caplog sees it."""

    def configure_and_propagate(
        level: str = "INFO", start_time: float | None = None
    ) -> logging.Logger:
        logger = configure_logging(level, start_time)
        logger.propagate = True
        return logger

    monkeypatch.setattr("karmascout.cli.configure_logging", configure_and_propagate)


def test_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.dry_run is False
    assert args.no_browser is False
    assert args.output is None


def test_missing_api_key_is_a_clean_failure(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    assert "Configuration error" in capsys.readouterr().err


def test_dry_run_needs_no_api_key(tmp_path: Path) -> None:
    assert main(["--dry-run", "--output", str(tmp_path / "out.html")]) == 0


def test_dry_run_makes_no_network_calls(tmp_path: Path) -> None:
    """The autouse no_network fixture fails the test if any request is attempted."""
    output = tmp_path / "out.html"
    assert main(["--dry-run", "--output", str(output)]) == 0
    assert output.exists()


def test_dry_run_never_opens_a_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("karmascout.cli.webbrowser.open", lambda url: opened.append(url))
    main(["--dry-run", "--output", str(tmp_path / "out.html")])
    assert opened == []


def test_dry_run_writes_a_usable_report(tmp_path: Path) -> None:
    output = tmp_path / "out.html"
    main(["--dry-run", "--output", str(output)])
    html = output.read_text(encoding="utf-8")
    assert "KarmaScout" in html
    assert "Lärmprotokoll" in html
    assert 'id="ca0"' in html


def test_dry_run_applies_the_score_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 6-scoring fixture must be dropped at the default threshold of 7."""
    output = tmp_path / "out.html"
    main(["--dry-run", "--output", str(output)])
    html = output.read_text(encoding="utf-8")
    assert "6/10" not in html

    monkeypatch.setenv("KARMASCOUT_MIN_SCORE", "6")
    second = tmp_path / "out2.html"
    main(["--dry-run", "--output", str(second)])
    assert "6/10" in second.read_text(encoding="utf-8")


def test_no_results_exits_with_code_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KARMASCOUT_MIN_SCORE", "10")
    assert main(["--dry-run", "--output", str(tmp_path / "out.html")]) == 2


def test_log_level_override_is_applied(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"):
        main(["--dry-run", "--log-level", "DEBUG", "--output", str(tmp_path / "out.html")])
    assert any("key loaded" in record.message for record in caplog.records)


def test_api_key_never_appears_in_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KARMASCOUT_OPENROUTER_API_KEY", "sk-or-v1-supersecret")
    with caplog.at_level("DEBUG"):
        main(["--dry-run", "--output", str(tmp_path / "out.html")])
    assert "supersecret" not in caplog.text


# ── Verdict cache wiring ─────────────────────────────────────────────────────


def test_no_cache_flag_disables_the_cache() -> None:
    from karmascout.cli import load_settings

    args = build_parser().parse_args(["--dry-run", "--no-cache"])
    assert load_settings(args).cache_enabled is False


def test_cache_is_on_by_default() -> None:
    from karmascout.cli import load_settings

    args = build_parser().parse_args(["--dry-run"])
    assert load_settings(args).cache_enabled is True


def test_dry_run_writes_no_cache_file(tmp_path: Path) -> None:
    """Canned verdicts must never be persisted where a real run would read them."""
    main(["--dry-run", "--no-browser", "--output", str(tmp_path / "out.html")])
    assert not (tmp_path / ".karmascout_cache.json").exists()


def test_a_real_run_reuses_cached_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Second run over the same thread must cost no OpenRouter call."""
    from karmascout.models import AiVerdict, RedditItem

    calls: list[str] = []
    thread = RedditItem(
        subreddit="de",
        url="https://www.reddit.com/r/de/comments/x/",
        title="Ruhestörung",
        body="laut",
        keyword="Ruhestörung",
        created_utc=__import__("time").time() - 3600,
    )

    class StubClient:
        def __init__(self, settings: object) -> None: ...

        def search_subreddit(self, subreddit: str, cutoff: float) -> list[RedditItem]:
            return [thread]

        def scan_feeds(self, subreddit: str, cutoff: float) -> list[RedditItem]:
            return []

    class StubScorer:
        def __init__(self, settings: object) -> None: ...

        def score(self, item: RedditItem, now: float | None = None) -> AiVerdict | None:
            calls.append(item.url)
            return AiVerdict(score=9, comment_a="A", comment_b="B")

    monkeypatch.setenv("KARMASCOUT_OPENROUTER_API_KEY", "sk-or-v1-testkey")
    monkeypatch.setattr("karmascout.cli.RedditClient", StubClient)
    monkeypatch.setattr("karmascout.cli.Scorer", StubScorer)

    argv = ["--no-browser", "--output", str(tmp_path / "out.html")]
    assert main(argv) == 0
    assert len(calls) == 1
    assert Path(".karmascout_cache.json").exists()

    assert main(argv) == 0
    assert len(calls) == 1  # the second run paid nothing
