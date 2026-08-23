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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceInfo:
    trace_id: str | None = None
    trace_path: str | None = None
    cid: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[TraceInfo | None] = contextvars.ContextVar(
    "mercury_trace", default=None
)


def get_trace() -> TraceInfo | None:
    """The trace context of the event being handled, if any."""
    return _current.get()


@contextmanager
def trace_context(trace_id: str, trace_path: str,
                  cid: str | None = None) -> Iterator[TraceInfo]:
    """Establish a trace context around a block - the node runWithTrace twin.

    Useful for callers outside a hosted function (batch jobs, tests) whose
    PostOffice calls should carry a trace: the client inherits the context
    into the outbound envelope.
    """
    info = TraceInfo(trace_id=trace_id, trace_path=trace_path, cid=cid)
    token = _set_trace(info)
    try:
        yield info
    finally:
        _reset_trace(token)


def annotate_trace(key: str, value: Any) -> None:
    """Attach an annotation to the current trace (returned on the reply envelope)."""
    info = _current.get()
    if info is not None:
        info.annotations[str(key)] = value


def _set_trace(info: TraceInfo | None) -> contextvars.Token[TraceInfo | None]:
    return _current.set(info)


def _reset_trace(token: contextvars.Token[TraceInfo | None]) -> None:
    _current.reset(token)
