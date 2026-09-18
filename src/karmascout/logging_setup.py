"""Logging configuration: one logger, one handler, level from config.

The handler neither owns nor mutates the stream: reconfiguring ``sys.stdout`` raises
``AttributeError`` whenever it is not a ``TextIOWrapper``. It resolves ``sys.stdout``
at write time instead, which stays correct when stdout is swapped out (pytest
capture, a redirect), and degrades gracefully when the console's code page cannot
encode a character rather than raising.
"""

from __future__ import annotations

import logging
import sys
import time

LOGGER_NAME = "karmascout"

_CONFIGURED = False


class _ElapsedFormatter(logging.Formatter):
    """Formatter that prefixes each line with wall time and elapsed run time."""

    def __init__(self, start_time: float) -> None:
        super().__init__("[%(asctime)s | +%(elapsed)s] %(message)s", datefmt="%H:%M:%S")
        self._start_time = start_time

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, injecting the ``elapsed`` field the format string uses.

        Args:
            record: The record to render.

        Returns:
            The formatted line.
        """
        elapsed = int(record.created - self._start_time)
        record.elapsed = f"{elapsed // 60}m{elapsed % 60:02d}s"
        return super().format(record)


class StdoutHandler(logging.Handler):
    """Writes to whatever ``sys.stdout`` is at the moment of the write.

    Never closes or reconfigures the stream, so it is safe to reconfigure logging
    repeatedly and safe to use under a capturing test runner.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted record to stdout, tolerating encoding limits.

        Args:
            record: The record to write.
        """
        message = self.format(record)
        stream = sys.stdout
        try:
            stream.write(message + "\n")
            stream.flush()
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            safe = message.encode(encoding, errors="replace").decode(encoding)
            stream.write(safe + "\n")
            stream.flush()
        except ValueError:
            # The stream was closed, typically during interpreter shutdown.
            return


def configure_logging(level: str = "INFO", start_time: float | None = None) -> logging.Logger:
    """Configure and return the single KarmaScout logger.

    Calling this more than once replaces the existing handler rather than stacking a
    second one, so repeated calls stay idempotent.

    Args:
        level: Logging level name, e.g. ``"INFO"`` or ``"DEBUG"``.
        start_time: POSIX timestamp used as the zero point for elapsed-time prefixes.
            Defaults to the moment this function is called.

    Returns:
        The configured ``karmascout`` logger.
    """
    global _CONFIGURED

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    # Do not propagate: a host application's root handler would otherwise duplicate
    # and reformat every line. Tests re-enable propagation to observe records.
    logger.propagate = False

    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    handler = StdoutHandler()
    handler.setFormatter(_ElapsedFormatter(start_time if start_time is not None else time.time()))
    logger.addHandler(handler)

    _CONFIGURED = True
    return logger


def get_logger() -> logging.Logger:
    """Return the KarmaScout logger, configuring it with defaults if needed.

    Returns:
        The ``karmascout`` logger.
    """
    if not _CONFIGURED:
        return configure_logging()
    return logging.getLogger(LOGGER_NAME)
