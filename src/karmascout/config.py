"""Application configuration, loaded from the environment.

Every tunable lives here, read from environment variables prefixed with
``KARMASCOUT_`` or from a ``.env`` file in the working directory. The defaults are
the tuned values the tool is meant to run with, so an API key is the only setting
that must be supplied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_KEYWORDS: tuple[str, ...] = (
    # Unambiguous noise-dispute terms — near-zero false positives
    "Lärmprotokoll",
    "Ruhestörung",
    "Lärmbelästigung",
    "Trittschall",
    "Zimmerlautstärke",
    # Noise + neighbor combos
    "laute Nachbarn",
    "Nachbarn zu laut",
    "Nachbarn",
    "Nachbar Lärm",
    "Nachbar laute Musik",
    "Nachtruhe Nachbarn",
    "Schlafstörung Nachbar",
    # Legal / escalation — noise-qualified
    "Mietminderung Lärm",
    "Lärm Ordnungsamt",
    "Vermieter Lärm",
    "Polizei Lärm Nachbarn",
)

DEFAULT_SUBREDDITS: tuple[str, ...] = (
    "de",
    "Austria",
    # Topic-specific: renting and neighbour disputes
    "mieten",
    # Advice and opinion subs where noise complaints surface
    "AskAGerman",
    "FragReddit",
    "Ratschlag",
    "Unbeliebtemeinung",
)

#: Feed sorts scanned in pass 2, in order.
DEFAULT_FEED_SORTS: tuple[str, ...] = ("new", "hot")


class Settings(BaseSettings):
    """Runtime configuration for a KarmaScout run.

    Attributes are populated from ``KARMASCOUT_``-prefixed environment variables.
    See ``.env.example`` for the full documented list.
    """

    model_config = SettingsConfigDict(
        env_prefix="KARMASCOUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Secrets ──────────────────────────────────────────────────────────────

    openrouter_api_key: SecretStr = Field(
        description="OpenRouter API key. Never logged, in whole or in part.",
    )

    # ── Reddit access ────────────────────────────────────────────────────────

    reddit_contact: str = Field(
        default="unknown",
        description="Contact handle embedded in the Reddit user agent.",
    )
    subreddits: tuple[str, ...] = Field(default=DEFAULT_SUBREDDITS)
    keywords: tuple[str, ...] = Field(default=DEFAULT_KEYWORDS)
    feed_sorts: tuple[str, ...] = Field(default=DEFAULT_FEED_SORTS)
    feed_limit: int = Field(default=100, ge=1, le=100)

    # ── Filtering ────────────────────────────────────────────────────────────

    max_age_hours: int = Field(default=24, ge=1)
    min_score: int = Field(default=7, ge=1, le=10)
    max_items_to_score: int = Field(
        default=150,
        ge=0,
        description=(
            "Ceiling on how many collected threads are sent to the LLM in one run, "
            "newest first. Every thread costs one paid call, so this bounds the cost "
            "of an unusually productive run. 0 disables the cap."
        ),
    )

    # ── Account context injected into the AI prompt ──────────────────────────

    account_karma: int = Field(default=818, ge=0)
    account_age_days: int = Field(default=30, ge=0)

    # ── Pacing and concurrency ───────────────────────────────────────────────

    fetch_delay: float = Field(
        default=0.8,
        ge=0.0,
        description="Seconds to sleep between Reddit requests.",
    )
    rate_limit_wait: int = Field(
        default=8,
        ge=0,
        description=(
            "Base seconds to wait after a Reddit 429. Used as the multiplier of an "
            "exponential backoff, capped at 120 seconds."
        ),
    )
    reddit_retries: int = Field(default=3, ge=1)
    reddit_timeout: float = Field(default=10.0, gt=0)
    ai_concurrency: int = Field(default=4, ge=1)
    ai_retries: int = Field(default=3, ge=1)
    ai_timeout: float = Field(default=30.0, gt=0)
    heartbeat_interval: float = Field(default=60.0, ge=0)

    # ── AI ───────────────────────────────────────────────────────────────────

    openrouter_model: str = Field(default="openrouter/auto")

    # ── Output ───────────────────────────────────────────────────────────────

    output_path: Path = Field(default=Path("karmascout_results.html"))
    log_level: LogLevel = Field(default="INFO")

    # ── Verdict cache ────────────────────────────────────────────────────────

    cache_enabled: bool = Field(
        default=True,
        description=(
            "Reuse the verdict already computed for a thread in an earlier run. "
            "The report is unchanged; only the repeated OpenRouter spend goes away."
        ),
    )
    cache_path: Path = Field(default=Path(".karmascout_cache.json"))

    @field_validator("openrouter_api_key")
    @classmethod
    def _reject_placeholder_key(cls, value: SecretStr) -> SecretStr:
        """Reject an unset or obviously-placeholder API key early and clearly."""
        raw = value.get_secret_value().strip()
        if not raw or raw.upper().startswith(("PASTE", "SK-OR-V1-REPLACE")):
            msg = (
                "KARMASCOUT_OPENROUTER_API_KEY is not set to a real key. "
                "Copy .env.example to .env and fill in your key from https://openrouter.ai."
            )
            raise ValueError(msg)
        return SecretStr(raw)

    @property
    def user_agent(self) -> str:
        """Reddit-format user agent: ``<platform>:<app id>:<version> (by /u/<contact>)``."""
        from karmascout import __version__

        return f"python:karmascout:v{__version__} (by /u/{self.reddit_contact})"

    @property
    def max_age_seconds(self) -> int:
        """The freshness window expressed in seconds."""
        return self.max_age_hours * 3600
