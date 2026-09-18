# KarmaScout — Reddit Keyword Alert

Finds Reddit threads where people are complaining about noisy neighbours or noise
disturbances, scores each one with an LLM, and drafts two casual German comments for
you to post **manually**.

KarmaScout is **read-only** with respect to Reddit. It never posts, comments, votes,
or logs in. It only reads public RSS feeds and writes an HTML file of suggestions.

---

## What it does

1. **Keyword search (pass 1)** — one `search.rss` request per subreddit, with all
   `keywords` combined into a single `OR` query. Reddit throttles the search endpoint
   per IP, so one combined query per subreddit is much cheaper than one per keyword.
2. **Feed scan (pass 2)** — reads each subreddit's `new.rss` and `hot.rss` and matches
   keywords locally. This catches brand-new threads the search index has not picked up.
3. **Filter** — keeps threads newer than `max_age_hours` whose title or body contains
   one of the configured keywords. Duplicates are dropped by permalink.
4. **AI scoring** — sends each candidate to OpenRouter, which scores it 1–10 as a
   commenting opportunity and drafts two German comments.
5. **Report** — writes `karmascout_results.html` (ranked cards, copy buttons, thread
   links) and opens it. Only threads scoring `min_score` or above appear.

No Reddit account or Reddit API key is needed.

> **Why RSS and not the official API?** Reddit closed self-serve API app creation under
> its Responsible Builder Policy, so OAuth is not obtainable for this project. Public
> RSS works, but it is rate-limited — see [Rate limits](#rate-limits).

---

## Setup

Requires **Python 3.11+** and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # creates .venv and installs everything from uv.lock
cp .env.example .env     # then edit .env and add your OpenRouter key
```

Get a key at [openrouter.ai](https://openrouter.ai).

On Windows Git Bash the same commands work unchanged. In PowerShell use
`Copy-Item .env.example .env`.

---

## Environment variables

Every setting is read from the environment, or from a `.env` file in the working
directory. All are prefixed `KARMASCOUT_`. Only the first is required.

| Variable | Default | Purpose |
|---|---|---|
| `KARMASCOUT_OPENROUTER_API_KEY` | *(required)* | OpenRouter API key |
| `KARMASCOUT_REDDIT_CONTACT` | `unknown` | Your handle, embedded in the Reddit user agent |
| `KARMASCOUT_OPENROUTER_MODEL` | `openrouter/auto` | Model used for scoring |
| `KARMASCOUT_MAX_AGE_HOURS` | `24` | Only consider threads newer than this |
| `KARMASCOUT_MIN_SCORE` | `7` | Only report threads scored at or above this (1–10) |
| `KARMASCOUT_MAX_ITEMS_TO_SCORE` | `150` | Cap on paid LLM calls per run, newest first; `0` disables |
| `KARMASCOUT_ACCOUNT_KARMA` | `818` | Your karma; used in the AI prompt |
| `KARMASCOUT_ACCOUNT_AGE_DAYS` | `30` | Your account age; used in the AI prompt |
| `KARMASCOUT_FETCH_DELAY` | `0.8` | Seconds between Reddit requests |
| `KARMASCOUT_RATE_LIMIT_WAIT` | `8` | Base backoff seconds after a Reddit 429 |
| `KARMASCOUT_REDDIT_RETRIES` | `3` | Attempts per Reddit request |
| `KARMASCOUT_AI_CONCURRENCY` | `4` | Parallel OpenRouter scoring calls |
| `KARMASCOUT_AI_RETRIES` | `3` | Attempts per OpenRouter request |
| `KARMASCOUT_HEARTBEAT_INTERVAL` | `60` | Seconds between progress heartbeats |
| `KARMASCOUT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `KARMASCOUT_CACHE_ENABLED` | `true` | Reuse verdicts from earlier runs instead of re-paying |
| `KARMASCOUT_CACHE_PATH` | `.karmascout_cache.json` | Where those verdicts are stored |
| `KARMASCOUT_FEED_LIMIT` | `100` | Posts requested per feed |
| `KARMASCOUT_KEYWORDS` | 15 German terms | JSON array; overrides the built-in list |
| `KARMASCOUT_SUBREDDITS` | `["germany"]` | JSON array; overrides the built-in list |
| `KARMASCOUT_FEED_SORTS` | `["new","hot"]` | JSON array of feed sorts for pass 2 |

`.env` is gitignored. Never commit it.

---

## Run

```bash
uv run karmascout
```

Takes a few minutes. Progress is logged with a heartbeat so you can see it is alive.
The browser opens on the finished report.

Exit codes: `0` success, `1` configuration error, `2` ran fine but nothing met
`min_score`.

### Dry run

```bash
uv run karmascout --dry-run --no-browser --output out.html
```

Runs the entire pipeline offline against fixture data: **no Reddit requests, no
OpenRouter requests, no API key required, no browser**. Use this to verify a change
end to end without spending credit or hitting Reddit's throttle.

### Other options

```
--output PATH     where to write the report (default: karmascout_results.html)
--no-browser      write the report but do not open it
--log-level LEVEL DEBUG | INFO | WARNING | ERROR | CRITICAL
--version
```

### Rate limits

Reddit throttles `search.rss` aggressively **per IP**. Run KarmaScout at most once an
hour. Parallelising Reddit requests makes throttling worse, not better — the only
useful lever is making fewer requests, which is why pass 1 combines all keywords into
one query per subreddit. Failed requests retry with exponential backoff on 429 and 5xx.

---

## Testing

```bash
uv run pytest                                    # full suite
uv run pytest --cov=karmascout --cov-report=term-missing
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

No test touches the network — a fixture fails any test that tries.

Install the pre-commit hooks once with `uv run pre-commit install`.

---

## Project structure

```
src/karmascout/
├── config.py          all settings and the single secret, via pydantic-settings
├── models.py          RedditItem, AiVerdict, ScoredItem
├── logging_setup.py   the one configured logger
├── reddit_client.py   the ONLY module that talks to Reddit
├── scoring.py         the ONLY module that talks to OpenRouter
├── pipeline.py        orchestration: collect → dedupe → score → filter → rank
├── report.py          HTML rendering
├── dryrun.py          offline stand-ins for both clients
├── cli.py             console entry point
└── templates/
    └── report.html.j2 the report page (Jinja2, autoescaped)

tests/                 mirrors the package; all network calls mocked
```

---

## App context — Ruhestörer Logger

KarmaScout surfaces threads where people are frustrated with noisy neighbours so you
can reply helpfully — building karma while reaching the audience for the **Ruhestörer
Logger** app (a noise-disturbance logger with PDF export for Germany, Austria, and
Switzerland).

You write and post every comment yourself. KarmaScout only suggests.
