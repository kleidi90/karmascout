"""KarmaScout — find, score, and draft replies to German noise-dispute Reddit threads.

The package is deliberately layered so that each concern lives in exactly one module:

* :mod:`karmascout.config` — all tunables and the single secret, from the environment.
* :mod:`karmascout.models` — typed models for every structure that crosses a boundary.
* :mod:`karmascout.reddit_client` — the *only* module that talks to Reddit.
* :mod:`karmascout.scoring` — the *only* module that talks to OpenRouter.
* :mod:`karmascout.pipeline` — orchestration, free of I/O details.
* :mod:`karmascout.report` — HTML rendering.
* :mod:`karmascout.cli` — the console entry point.

KarmaScout is read-only with respect to Reddit: it never posts, comments, or votes.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
