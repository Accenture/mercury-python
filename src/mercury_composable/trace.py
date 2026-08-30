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

# The business correlation-id rides an engine-managed envelope tag - never an
# envelope header - and is injected into the receiving function's input header
# copy as a read-only view at delivery (the engines' WorkerHandler contract).
MY_CID_TAG = "my_cid"
MY_CORRELATION_ID = "my_correlation_id"
# The engines' RPC round-trip marker tag: an RPC leg emits no trace dataset
# (its metrics fold into the caller's view), so the clients stamp it on
# request() calls and the bus honors it at delivery.
RPC_TAG = "rpc"
# Reserved application log-context tokens (the engines' LogContext contract):
# resolved live per log line; a developer cannot override them via
# update_context. The output key names in app-log-context.yaml are the
# operator's choice - this set governs the template tokens and developer API.
RESERVED_CONTEXT_TOKENS = frozenset(
    {"cid", "traceId", "tracePath", "spanId", "parentSpanId", "service", "utc"})


@dataclass
class TraceInfo:
    # the executing function's route - outbound calls fill their sender ("from")
    # with it, the engines' PostOffice.touch parity
    route: str | None = None
    trace_id: str | None = None
    trace_path: str | None = None
    cid: str | None = None
    my_correlation_id: str | None = None
    # span lineage (the engines' model): span_id is THIS execution's span,
    # stamped onto outbound events so the receiver stores it as its
    # parent_span_id; parent_span_id is the caller's span from the inbound
    # envelope. 16-hex (W3C-shaped), so traceparent stamping fires when the
    # trace id is 32-hex.
    span_id: str | None = None
    parent_span_id: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    # developer-supplied application log-context key-values (update_context) -
    # a logging-only sink, rendered into the "context" block of structured log
    # lines; distinct from annotations, which feed the trace telemetry
    custom_context: dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[TraceInfo | None] = contextvars.ContextVar(
    "mercury_trace", default=None
)


def get_trace() -> TraceInfo | None:
    """The trace context of the event being handled, if any."""
    return _current.get()


@contextmanager
def trace_context(trace_id: str, trace_path: str, cid: str | None = None,
                  my_correlation_id: str | None = None,
                  span_id: str | None = None) -> Iterator[TraceInfo]:
    """Establish a trace context around a block - the node runWithTrace twin.

    Useful for callers outside a hosted function (batch jobs, tests) whose
    PostOffice calls should carry a trace: the client inherits the context
    into the outbound envelope, including the business correlation-id as the
    engine-managed my_cid tag. ``span_id`` declares the caller's CURRENT span
    (e.g. an edge span from an external OpenTelemetry context) so the next
    hop's span parents onto it.
    """
    info = TraceInfo(trace_id=trace_id, trace_path=trace_path, cid=cid,
                     my_correlation_id=my_correlation_id, span_id=span_id)
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


def update_context(key: str, value: Any) -> None:
    """Add (or remove, when value is None) a custom key-value in the
    application log context - the engines' PostOffice.updateContext twin.

    The key-value is rendered into the "context" block of structured log
    output (log.format json/compact) when the app-log-context feature is
    enabled. Unlike annotate_trace (which feeds the distributed-trace
    telemetry), this is a logging-only sink. No-op outside a hosted request.

    :raises ValueError: if key is one of the reserved context tokens
    """
    if key in RESERVED_CONTEXT_TOKENS:
        raise ValueError(f"Cannot override reserved log context key '{key}'"
                         f" - reserved keys are {sorted(RESERVED_CONTEXT_TOKENS)}")
    info = _current.get()
    if info is None:
        return
    if value is None:
        info.custom_context.pop(key, None)
    else:
        info.custom_context[key] = value


def _set_trace(info: TraceInfo | None) -> contextvars.Token[TraceInfo | None]:
    return _current.set(info)


def _reset_trace(token: contextvars.Token[TraceInfo | None]) -> None:
    _current.reset(token)
