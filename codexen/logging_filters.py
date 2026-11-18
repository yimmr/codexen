"""Application logging hygiene for expected tool-input failures."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from .exceptions import (
    ExpectedToolFailure,
    SnippetAmbiguousError,
    SnippetNotFoundError,
    UnsupportedNewlineModeError,
)

EXPECTED = (SnippetNotFoundError, SnippetAmbiguousError, UnsupportedNewlineModeError, ExpectedToolFailure)


class ExpectedFailureFilter(logging.Filter):
    """Collapse only explicitly user-correctable failures; preserve system tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None or not _expected_chain(exc):
            return True
        message = record.getMessage()
        record.exc_info = None
        record.exc_text = None
        record.args = ()
        record.msg = message.replace("\r", "\\r").replace("\n", "\\n")
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        return True


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for related in (current.__cause__, current.__context__):
            if related is not None:
                pending.append(related)


def _expected_chain(exc: BaseException) -> bool:
    return all(isinstance(item, EXPECTED) or isinstance(item, FileNotFoundError) for item in _chain(exc))


def install_expected_failure_filter(logger: logging.Logger | None = None) -> ExpectedFailureFilter:
    """Install the filter idempotently on handlers of the selected logger."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(item, ExpectedFailureFilter) for item in handler.filters):
            handler.addFilter(ExpectedFailureFilter())
    return ExpectedFailureFilter()
