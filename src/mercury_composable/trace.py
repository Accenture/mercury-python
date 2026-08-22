"""
Minimalist telemetry: distributed trace context for polyglot functions.

The engines carry trace_id / trace_path / cid inside the event envelope. The
server installs them into a context variable around each handler call, so a
handler (and anything it awaits) can read its trace and annotate it without
plumbing arguments. Annotations ride back on the reply envelope's
``annotations`` field, visible to the calling engine's tracing.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TraceInfo:
    trace_id: Optional[str] = None
    trace_path: Optional[str] = None
    cid: Optional[str] = None
    annotations: Dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[Optional[TraceInfo]] = contextvars.ContextVar(
    "mercury_trace", default=None
)


def get_trace() -> Optional[TraceInfo]:
    """The trace context of the event being handled, if any."""
    return _current.get()


def annotate_trace(key: str, value: Any) -> None:
    """Attach an annotation to the current trace (returned on the reply envelope)."""
    info = _current.get()
    if info is not None:
        info.annotations[str(key)] = value


def _set_trace(info: Optional[TraceInfo]) -> contextvars.Token:
    return _current.set(info)


def _reset_trace(token: contextvars.Token) -> None:
    _current.reset(token)
