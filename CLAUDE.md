# CLAUDE.md — KarmaScout

KarmaScout reads public Reddit RSS feeds for German noise-dispute threads, scores each
with an LLM via OpenRouter, and writes an HTML page of ranked opportunities with two
drafted comments each. The user reads that page and posts comments **manually**.

## Hard rules

1. **Read-only with respect to Reddit.** No code path posts, comments, votes, or
   authenticates. Do not add one unless the user asks for it explicitly, in those words.
2. **Never run the tool against live Reddit.** Use `--dry-run` or the tests. A real run
   spends OpenRouter credit and burns the user's per-IP Reddit throttle budget, which is
   shared with their own browsing.
3. **Never change what the tool finds, or how it paces, without asking.** Keywords,
   subreddits, `min_score`, `max_age_hours`, `fetch_delay`, the prompt wording, and the
   two-pass collection strategy are all deliberate.
4. **Secrets only in `.env`.** Never hardcode a key, never log key material — not a
   prefix, not a length — never commit `.env`.
5. **No test may touch the network or need real credentials.** `tests/conftest.py`
   fails any test that tries. Do not disable that fixture.

## The trust boundary

Thread text is written by strangers and reaches the user's own Reddit account:

```
Reddit thread → prompt → LLM → drafted comment → user's clipboard → posted as the user
```

A thread that steers the model gets its payload posted under the user's name, so these
defences are load-bearing and must survive any change: `scoring.sanitize_field` plus
the `<<<THREAD>>>` fence (thread text is data, never instructions), `AiVerdict` coercing
the model's JSON rather than indexing it, and `report.draft_links` surfacing any URL in
a draft before the user copies it. Also `defusedxml` on remote XML, template
autoescaping, and the link-scheme allowlist.

## Architecture

Strictly layered; each concern lives in exactly one module.

| Module | Responsibility |
|---|---|
| `config.py` | Every tunable and the single secret, via pydantic-settings |
| `models.py` | `RedditItem`, `AiVerdict`, `ScoredItem` — the only structures crossing boundaries |
| `logging_setup.py` | The single configured logger |
| `reddit_client.py` | **The only module that talks to Reddit**; defines `RedditSource` |
| `scoring.py` | **The only module that talks to OpenRouter**; defines `ScoreSource` |
| `cache.py` | Persistent verdict cache; `CachingScorer` wraps any `ScoreSource` |
| `pipeline.py` | Orchestration against the two protocols, no I/O of its own |
| `report.py` | HTML rendering via the Jinja2 template |
| `dryrun.py` | Offline stand-ins, implementing the protocols directly |
| `cli.py` | Argument parsing, wiring, exit codes (`0` ok, `1` config, `2` no results) |

Data flows one way: `cli → pipeline → (reddit_client, scoring) → report`. Nothing below
`pipeline` imports anything above it.

New outbound call? It goes in `reddit_client.py` or `scoring.py` — do not import
`requests` anywhere else. And `dryrun.py` implements the protocols rather than
subclassing a live client, or an unoverridden method would reach the network mid-dry-run.

## Cost model

Every collected thread costs one paid LLM call. `max_items_to_score` caps a run; the
verdict cache stops a repeat thread being paid for twice. Treat anything that widens
collection as a spend increase, and say so.

## Conventions

Tooling config lives in `pyproject.toml` — read it rather than restating it here.

- Full type hints; `mypy --strict` must pass.
- Google-style docstrings on public modules, classes, and functions. Say *why*, not what
  the signature already says.
- `logging`, never `print` — except the config-error message in `cli.py`, which runs
  before logging exists.
- Specific exceptions only. No bare `except`; no blanket `except Exception` that hides a
  failure (the one in `pipeline.score_all` logs, and says why in a comment). Retryable
  failures raise `RedditFetchError` or `ScoringError`; tenacity backs off.
- `pathlib`, never `os.path`. Must work on Windows Git Bash and Linux alike.

## Commands

```bash
uv sync                                   # install
uv run karmascout --dry-run --no-browser  # safe, offline, end-to-end
uv run pytest                             # tests (--cov=karmascout for coverage)
uv run ruff check . && uv run ruff format .
uv run mypy
uv run karmascout                         # real run — do NOT run this as an agent
```

Lint, type check, and tests all pass before you move on. Report failures with their
output; never describe unverified work as done.

## Testing

`tests/` mirrors the package. Reddit responses come from `atom_feed()` / `atom_entry()`
in `conftest.py`; HTTP is replaced by `StubSession`. Time is pinned to `conftest.NOW` —
pass `now=NOW` rather than relying on the clock.

Many tests are **characterisation tests**, pinning behaviour that is deliberate rather
than incidental: query construction, the `search` keyword fallback, badge colour
thresholds, the `ca0`/`cb0` copy-button ids, the 300-character body cap. If one fails,
assume your change is wrong before you assume the test is.

## Known constraints

- Reddit's `search.rss` is throttled per IP hard enough that a rapid second run gets
  429s. Fewer requests is the only real lever — do not parallelise Reddit calls, and
  keep pacing on the failure paths, where it matters most.
- OpenRouter is not throttled the same way, so scoring is the one stage safe to run
  concurrently (`ai_concurrency`, default 4). Each thread gets its own HTTP session.
- Nothing schedules the bot. CI only lints, type-checks, tests, audits dependencies,
  and performs an offline dry run.
