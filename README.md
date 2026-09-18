# KarmaScout — Reddit Keyword Alert

[![CI](https://img.shields.io/github/actions/workflow/status/kleidi90/karmascout/ci.yml?branch=main&logo=github&label=CI)](https://github.com/kleidi90/karmascout/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-strict-2a6db2)](https://mypy-lang.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Finds Reddit threads where people are complaining about noisy neighbours or noise
disturbances, scores each one with an LLM, and drafts two casual German comments for
you to post **manually**.

KarmaScout is **read-only** with respect to Reddit. It never posts, comments, votes,
or logs in. It only reads public RSS feeds and writes an HTML file of suggestions.

## Responsible use

- KarmaScout never posts, comments, votes, or logs in. It reads public RSS and writes
  a local HTML file.
- The AI drafts are starting points. A human reviews and edits each one before
  anything is posted, one at a time.
- Comments that mention the Ruhestörer Logger app disclose the connection to it, and
  follow each subreddit's self-promotion rules.
- No mass posting, no scheduling, no multiple accounts. The tool caps what it surfaces
  on purpose: `max_items_to_score` bounds a run and `min_score` filters the rest.

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

### What the report looks like

![KarmaScout report: ranked opportunity cards, each with an AI score badge, the thread
title and excerpt, and two drafted German comments with copy buttons](docs/report-screenshot.png)

*Each card carries the 1-10 score and the reason for it, the thread's age and subreddit,
a link to the thread, and the two drafted comments. Drafts containing a link are flagged
before you copy them.*

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
--no-cache        re-score every thread, ignoring verdicts cached by earlier runs
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
├── cache.py           persistent verdict cache, so a repeat thread is not paid for twice
├── pipeline.py        orchestration: collect → dedupe → score → filter → rank
├── report.py          HTML rendering
├── dryrun.py          offline stand-ins for both clients
├── cli.py             console entry point
└── templates/
    └── report.html.j2 the report page (Jinja2, autoescaped)

tests/                 mirrors the package; all network calls mocked
```

---

## Design decisions

**Public RSS instead of the official API.** Reddit closed self-serve API app creation
under its Responsible Builder Policy, so OAuth is not obtainable for a project like
this one. The alternative was not building it. Public Atom endpoints need no account
and no key, at the cost of a hard per-IP throttle and no access to anything private —
which is also why the tool can be honestly described as read-only.

**One combined `OR` query per subreddit, not one per keyword.** The obvious approach
issues one `search.rss` request per subreddit-keyword pair, which is 15 requests per
subreddit for the default keyword list. Since `search.rss` is throttled per IP,
request count is the only lever that matters, so `build_combined_query` folds every
keyword into a single query. Multi-word keywords keep loose `AND` semantics rather
than exact-phrase, so recall matches the per-keyword version it replaced.

**Bounded concurrency for scoring, strictly sequential for Reddit.** These look
inconsistent and are deliberate: the two services fail differently. OpenRouter is not
IP-throttled the way `search.rss` is, so scoring runs on a thread pool of
`ai_concurrency` workers, each with its own HTTP session. Reddit requests stay
sequential with a `fetch_delay` between them, and the delay is paid in a `finally` —
a request that just got a 429 must be followed by more delay, not less.

**A verdict cache, not a seen-set.** Deduplication is per-run, so consecutive runs
re-score every thread still inside the freshness window; at `max_age_hours=168` that
is the same week of threads paid for repeatedly. Remembering only that a thread was
seen would drop it from the report. Caching the verdict instead means a repeat thread
renders identically for zero spend. Failed scores are never cached, since those are
transient.

**A cap on paid calls, with the newest kept.** Every collected thread costs one LLM
call, so a wider keyword list or a busy day spends without a ceiling.
`max_items_to_score` bounds a run and logs exactly how many it dropped. When the cap
bites it keeps the newest threads, because the prompt rates 2–20 hours old as ideal
and the oldest candidates are worth the least.

**Offline dry run as a first-class mode.** `--dry-run` swaps both clients for stand-ins
that make no requests at all, so the whole pipeline — collection, deduplication,
scoring, ranking, rendering — can be exercised for free and CI can run it end to end
on every push. The stand-ins implement the `RedditSource` and `ScoreSource` protocols
rather than subclassing the live clients; subclassing would leave any un-overridden
method reaching the network.

**Read-only by design, not by discipline.** There is no posting code path to disable,
no Reddit credentials to leak, and no OAuth scope to get wrong. Drafts are rendered to
a local HTML file with copy buttons, and a human decides what to do with them. The
narrow capability is the point: the tool cannot misbehave on Reddit because it has no
mechanism to.

---

## App context — Ruhestörer Logger

KarmaScout surfaces threads where people are frustrated with noisy neighbours so you
can reply helpfully — building karma while reaching the audience for the **Ruhestörer
Logger** app (a noise-disturbance logger with PDF export for Germany, Austria, and
Switzerland).
