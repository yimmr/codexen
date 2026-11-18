import logging
import io

from codexen.exceptions import SnippetNotFoundError
from codexen.logging_filters import ExpectedFailureFilter

def test_expected_failure_is_single_line_warning():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad\nvalue", (), None)
    record.exc_info = (SnippetNotFoundError, SnippetNotFoundError("bad\nvalue"), None)
    ExpectedFailureFilter().filter(record)
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "\\n" in str(record.msg)

def test_unexpected_cause_is_preserved():
    try:
        try:
            raise PermissionError("denied")
        except PermissionError as inner:
            raise SnippetNotFoundError("user text") from inner
    except SnippetNotFoundError as exc:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad", (), None)
        record.exc_info = (SnippetNotFoundError, exc, None)
        assert ExpectedFailureFilter().filter(record)
        assert record.exc_info is not None
        assert record.levelno == logging.ERROR


def test_interpolated_message_survives_filter():
    exc = SnippetNotFoundError("missing")
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failure: %s", ("missing\nvalue",), None)
    record.exc_info = (type(exc), exc, None)
    ExpectedFailureFilter().filter(record)
    assert record.getMessage() == "failure: missing\\nvalue"


def test_real_handler_preserves_system_traceback_and_collapses_expected():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ExpectedFailureFilter())
    logger = logging.Logger("hygiene-test")
    logger.addHandler(handler)
    try:
        raise SnippetNotFoundError("missing\n[ERROR] forged")
    except SnippetNotFoundError:
        logger.exception("tool failure: %s", "missing\n[ERROR] forged")
    assert len(stream.getvalue().splitlines()) == 1
    assert "Traceback" not in stream.getvalue()
    stream.seek(0)
    stream.truncate(0)
    try:
        raise OSError("disk full")
    except OSError:
        logger.exception("tool system failure")
    assert "Traceback" in stream.getvalue()
    assert "disk full" in stream.getvalue()
