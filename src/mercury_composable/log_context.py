"""
Application log context - the engines' app-log-context feature.

When enabled (``app.log.context``, default true), the structured log
presentations (``log.format`` json/compact) add a ``context`` block to every
log line written inside a traced request, so application logs and the
distributed-trace telemetry stream correlate end to end in one aggregation.

The context template mirrors the engines' contract exactly:

- The built-in default template carries the standard trace context
  (cid, traceId, tracePath, spanId, parentSpanId, service, timestamp).
- An application may replace it entirely with its own ``app-log-context.yaml``
  in the resources folder (next to application.yml), mapping each output key
  to a reserved ``$token`` - resolved live per log line - or a constant
  (a literal, or ``${ENV:default}`` resolved once at load).
- ``app.log.context=false`` opts out.
- The ``cid`` token is the BUSINESS correlation-id only (the engine-managed
  my_cid tag); an internal routing id under the ``cid`` label would mislead
  log aggregation.
- Developer-supplied key-values (:func:`mercury_composable.update_context`)
  merge into the block; keys resolving to None are omitted, never "null".
"""

from __future__ import annotations

import importlib.resources
import os
import threading
from typing import Any

import yaml

from .config import app_config
from .envelope import iso_utc
from .log import get_logger
from .trace import RESERVED_CONTEXT_TOKENS, TraceInfo

FEATURE_FLAG = "app.log.context"
CONFIG_FILE = "app-log-context.yaml"
# the built-in default template ships as a packaged resource, exactly like the
# engines' classpath:/default-log-context.yaml
DEFAULT_FILE = "default-log-context.yaml"

log = get_logger("mercury.log")


def _context_section(data: Any) -> dict[str, Any] | None:
    """The template's ``context:`` section, or None when absent/malformed."""
    section = data.get("context") if isinstance(data, dict) else None
    if isinstance(section, dict):
        return {str(k): v for k, v in section.items()}
    return None


def default_template() -> dict[str, Any] | None:
    """The built-in default template from the packaged default-log-context.yaml."""
    try:
        resource = importlib.resources.files("mercury_composable").joinpath(DEFAULT_FILE)
        data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    return _context_section(data)


def _token_value(info: TraceInfo, token: str) -> Any:
    """Resolve a reserved token to its live value (None when absent)."""
    if token == "utc":
        return iso_utc()
    return {
        "cid": info.my_correlation_id,
        "traceId": info.trace_id,
        "tracePath": info.trace_path,
        "spanId": info.span_id,
        "parentSpanId": info.parent_span_id,
        "service": info.route,
    }.get(token)


class LogContextConfig:
    """Parsed context template: output key -> reserved token or constant."""

    def __init__(self, template: dict[str, Any] | None):
        self.tokens: dict[str, str] = {}
        self.constants: dict[str, Any] = {}
        for output_key, raw in (template or {}).items():
            self._parse_entry(output_key, raw if isinstance(raw, str) else str(raw))
        self.enabled = bool(self.tokens or self.constants)

    def _parse_entry(self, output_key: str, value: str) -> None:
        """One template entry: a reserved $token, or a constant (env-resolved
        value or literal; an unset ${VAR} with no default resolves to None and
        is dropped) - the engines' parseEntry."""
        if value.startswith("$") and not value.startswith("${"):
            token = value[1:]
            if token not in RESERVED_CONTEXT_TOKENS:
                raise ValueError(
                    f"Invalid log context token '{value}' for key "
                    f"'{output_key}' - allowed tokens: "
                    f"{sorted(RESERVED_CONTEXT_TOKENS)}")
            self.tokens[output_key] = token
            return
        resolved = app_config().resolve_text(value)
        if resolved is not None:
            self.constants[output_key] = resolved

    def render(self, info: TraceInfo) -> dict[str, Any]:
        """The context block for one log line: template tokens resolved live,
        constants, and the developer's custom key-values. Keys resolving to
        None are omitted."""
        out: dict[str, Any] = {}
        for output_key, token in self.tokens.items():
            value = _token_value(info, token)
            if value is not None:
                out[output_key] = value
        out.update(self.constants)
        for key, value in info.custom_context.items():
            if value is not None:
                out[key] = value
        return out


_lock = threading.Lock()
_instance: LogContextConfig | None = None


def _load() -> tuple[LogContextConfig, str | None]:
    """Resolve the active template; returns (config, warning-or-None). Never
    logs itself - the caller emits the warning AFTER installing the config, so
    the log line (which renders through this feature) cannot re-enter."""
    config = app_config()
    if (config.get_property(FEATURE_FLAG, "true") or "true").lower() == "false":
        return LogContextConfig(None), None
    # an application override replaces the default entirely - same resources
    # convention as application.yml
    source = config.source
    folder = os.path.dirname(source) if source != "none" else "resources"
    candidate = os.path.join(folder or "resources", CONFIG_FILE)
    if os.path.isfile(candidate):
        with open(candidate, "r", encoding="utf-8") as f:
            section = _context_section(yaml.safe_load(f.read()) or {})
        if section is None:
            # the engines log a warning and disable; mirror the outcome
            return LogContextConfig(None), \
                f"Log context config has no 'context' section - feature disabled ({candidate})"
        return LogContextConfig(section), None
    template = default_template()
    if template is None:
        return LogContextConfig(None), \
            f"Built-in {DEFAULT_FILE} missing - log context feature disabled"
    return LogContextConfig(template), None


def log_context_config() -> LogContextConfig:
    """The shared context template (loaded on first structured log line)."""
    global _instance
    warning = None
    with _lock:
        instance = _instance
        if instance is None:
            instance, warning = _load()
            _instance = instance
    if warning:
        log.warning(warning)
    return instance


def reset_for_test() -> None:
    """Test seam: reset so the next structured log line reloads the template."""
    global _instance
    with _lock:
        _instance = None
