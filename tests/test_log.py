"""Formatter tests: engine pattern and exception rendering."""

import logging
import sys

from mercury_composable.log import EngineJsonFormatter, EngineTextFormatter


def _record_with_exception() -> logging.LogRecord:
    try:
        raise RuntimeError("boom-x")
    except RuntimeError:
        return logging.LogRecord(
            name="unit.test", level=logging.ERROR, pathname=__file__,
            lineno=42, msg="Async event %s failed", args=("demo.route",),
            exc_info=sys.exc_info())


def test_text_formatter_engine_pattern_and_traceback():
    line = EngineTextFormatter().format(_record_with_exception())
    first = line.splitlines()[0]
    # %d{yyyy-MM-dd HH:mm:ss.SSS} %-5level %logger:%line - %msg
    assert " ERROR unit.test:42 - Async event demo.route failed" in first
    assert "Traceback" in line
    assert "RuntimeError: boom-x" in line


def test_json_formatter_carries_exception():
    import json

    entry = json.loads(EngineJsonFormatter().format(_record_with_exception()))
    assert entry["level"] == "ERROR"
    assert entry["logger"] == "unit.test:42"
    assert entry["message"] == "Async event demo.route failed"
    assert "RuntimeError: boom-x" in entry["exception"]


def test_json_is_pretty_and_compact_is_jsonl():
    import json

    record = _record_with_exception()
    pretty = EngineJsonFormatter().format(record)
    compact = EngineJsonFormatter(compact=True).format(record)
    # engine presentations: json = pretty-printed, compact = single line (JSONL)
    assert "\n" in pretty
    assert "\n" not in compact
    assert json.loads(pretty) == json.loads(compact)  # same information, two renderings
