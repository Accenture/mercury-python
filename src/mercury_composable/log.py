"""
Minimalist logging, presentation-consistent with the Mercury engines.

Polyglot installations aggregate logs from every runtime in one place, so the
line format matches the Java reference engine's log4j2 pattern::

    %d{yyyy-MM-dd HH:mm:ss.SSS} %-5level %logger:%line - %msg

Example::

    2026-08-22 10:15:30.123 INFO  my_app:42 - Function hello.python loaded

- The level comes from the ``LOG_LEVEL`` environment variable when set
  (mirroring the engines), else the ``log.level`` configuration key,
  else INFO.
- ``log.format`` carries the engines' three presentations: ``text``
  (default), ``json`` (pretty-printed) and ``compact`` (the same object on a
  single line - JSONL - for log aggregators). Inside a traced request, the
  JSON presentations add the application log ``context`` block (the engines'
  app-log-context feature - see :mod:`mercury_composable.log_context`).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from .config import app_config

_configured = False


def _message_of(record: logging.LogRecord) -> str | dict:
    """A structured (dict) message stays structural in the JSON presentations
    and renders as compact JSON in text mode - used by the distributed-trace
    dataset records, which stdout log-ingest agents parse."""
    if isinstance(record.msg, dict) and not record.args:
        return record.msg
    return record.getMessage()


class EngineTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        level = f"{record.levelname:<5}"
        message = _message_of(record)
        if isinstance(message, dict):
            message = json.dumps(message, ensure_ascii=False)
        line = f"{ts}.{ms:03d} {level} {record.name}:{record.lineno} - {message}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class EngineJsonFormatter(logging.Formatter):
    """Engine JSON presentations: json = pretty-printed, compact = one line (JSONL)."""

    def __init__(self, *, compact: bool = False):
        super().__init__()
        self._indent = None if compact else 2

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        entry = {
            "time": f"{ts}.{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": f"{record.name}:{record.lineno}",
            "message": _message_of(record),
        }
        # late imports to avoid a cycle (this module bootstraps logging)
        from .log_context import log_context_config
        from .trace import get_trace

        # the application log context (the engines' app-log-context feature):
        # a "context" block on every structured line inside a traced request,
        # correlating app logs with the distributed-trace telemetry stream
        info = get_trace()
        if info and info.trace_id:
            context_config = log_context_config()
            if context_config.enabled:
                entry["context"] = context_config.render(info)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, indent=self._indent)


def _setup() -> None:
    global _configured
    if _configured:
        return
    config = app_config()
    level_name = os.environ.get("LOG_LEVEL") or str(config.get("log.level", "INFO"))
    log_format = str(config.get("log.format", "text")).lower()
    handler = logging.StreamHandler(sys.stdout)
    if log_format in ("json", "compact"):
        handler.setFormatter(EngineJsonFormatter(compact=log_format == "compact"))
    else:
        handler.setFormatter(EngineTextFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    """A logger writing engine-consistent log lines."""
    _setup()
    return logging.getLogger(name or "mercury")
