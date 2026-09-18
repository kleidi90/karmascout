"""Tests for configuration loading and the defaults that pin original behaviour."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from karmascout.config import DEFAULT_KEYWORDS, DEFAULT_SUBREDDITS, Settings


def test_defaults_match_the_original_script(settings: Settings) -> None:
    """These decide what the tool finds and how Reddit sees it; they must not drift."""
    assert settings.max_age_hours == 24
    assert settings.min_score == 7
    assert settings.account_karma == 818
    assert settings.account_age_days == 30
    assert settings.ai_concurrency == 4
    assert settings.openrouter_model == "openrouter/auto"
    assert settings.feed_sorts == ("new", "hot")
    assert settings.keywords[0] == "Lärmprotokoll"
    # The lists themselves are meant to be edited; assert the wiring, not a count,
    # so tuning the search does not fail an unrelated test.
    assert settings.keywords == DEFAULT_KEYWORDS
    assert settings.subreddits == DEFAULT_SUBREDDITS
    assert len(settings.subreddits) >= 1


def test_api_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KARMASCOUT_OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_placeholder_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not set to a real key"):
        Settings(openrouter_api_key=SecretStr("PASTE-YOUR-KEY-HERE"), _env_file=None)  # type: ignore[call-arg]


def test_example_placeholder_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not set to a real key"):
        Settings(openrouter_api_key=SecretStr("sk-or-v1-replace-me"), _env_file=None)  # type: ignore[call-arg]


def test_key_is_not_exposed_by_repr(settings: Settings) -> None:
    """A SecretStr keeps the key out of tracebacks and log lines."""
    assert "testkey" not in repr(settings)
    assert settings.openrouter_api_key.get_secret_value() == "sk-or-v1-testkey"


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KARMASCOUT_OPENROUTER_API_KEY", "sk-or-v1-fromenv")
    monkeypatch.setenv("KARMASCOUT_MIN_SCORE", "9")
    monkeypatch.setenv("KARMASCOUT_MAX_AGE_HOURS", "48")
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.min_score == 9
    assert loaded.max_age_hours == 48


def test_out_of_range_min_score_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openrouter_api_key=SecretStr("sk-or-v1-x"), min_score=11, _env_file=None)  # type: ignore[call-arg]


def test_max_age_seconds_derives_from_hours(settings: Settings) -> None:
    assert settings.max_age_seconds == settings.max_age_hours * 3600
