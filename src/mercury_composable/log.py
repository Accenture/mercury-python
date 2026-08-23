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
- ``log.format=json`` switches to one JSON object per line with the same
  information (time, level, logger, message, and trace_id when a trace
  context is active).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from .config import app_config

_configured = False


class EngineTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        level = f"{record.levelname:<5}"
        return f"{ts}.{ms:03d} {level} {record.name}:{record.lineno} - {record.getMessage()}"


class EngineJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        entry = {
            "time": f"{ts}.{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": f"{record.name}:{record.lineno}",
            "message": record.getMessage(),
        }
        from .trace import get_trace  # late import to avoid a cycle

        info = get_trace()
        if info and info.trace_id:
            entry["trace_id"] = info.trace_id
        return json.dumps(entry, ensure_ascii=False)


def _setup() -> None:
    global _configured
    if _configured:
        return
    config = app_config()
    level_name = os.environ.get("LOG_LEVEL") or str(config.get("log.level", "INFO"))
    log_format = str(config.get("log.format", "text")).lower()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EngineJsonFormatter() if log_format == "json" else EngineTextFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    """A logger writing engine-consistent log lines."""
    _setup()
    return logging.getLogger(name or "mercury")
