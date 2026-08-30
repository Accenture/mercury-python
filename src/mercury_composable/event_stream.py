"""
Event streaming: the multi-shot reply contract and the envelope-mode SSE dialect.

The platform's native streaming pattern (all four runtimes): *the caller provides
a reply address; the callee streams events to it until a terminal signal*. Each
segment is one event to the caller's ``reply_to``, marked with the reserved
envelope header ``x-event-stream: data | eof | exception``. On the Event-over-HTTP
wire, the peer answers the one POST with a Server-Sent Events response in a hybrid
dialect: **envelope frames** (the reserved SSE event name ``envelope``, one
base64-encoded serialized envelope per frame) wherever envelope semantics matter -
the head, the terminals and non-text segments - and **raw SSE frames** for plain
text segments, so token relays stay near-zero overhead.

:class:`EventStreamWriter` is the producer helper - the engines' exact API::

    out = EventStreamWriter.from_request(event)   # an interceptor's raw envelope
    out.first(200, "text/event-stream")
    out.write("hello")                            # data segment
    out.write_named("tokens", {"n": 2})           # named (typed) SSE event
    out.close({"usage": usage})                   # end of transmission + metadata
    # or out.fail(e)                              # in-band failure

Writes after close/fail are dropped (debug log), mirroring the engines. An
in-band failure body carries the standard error key-values
``'{"type": "error", "status": n, "message": text}'``.
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any

from .config import app_config
from .envelope import EventEnvelope
from .exceptions import AppException
from .log import get_logger
from .trace import get_trace

if TYPE_CHECKING:
    from .registry import FunctionRegistry

# reserved envelope header (internal protocol, never on the HTTP wire)
X_EVENT_STREAM = "x-event-stream"
# optional companion on a data event: maps to the SSE "event:" field
X_EVENT_NAME = "x-event-name"
# marker vocabulary - deliberately the engines' ObjectStream vocabulary
DATA = "data"
EOF = "eof"
EXCEPTION = "exception"
# reserved SSE event name of the envelope-mode wire dialect: a frame with this
# name carries one base64-encoded serialized EventEnvelope
ENVELOPE = "envelope"

X_TTL = "x-ttl"
TEXT_EVENT_STREAM = "text/event-stream"
STREAM_CALLER_REQUIRED = "Streaming function requires a caller that accepts text/event-stream"

# reserved envelope headers a raw SSE frame may carry without loss
_RESERVED_HEADERS = {X_EVENT_STREAM, X_EVENT_NAME, X_TTL}

log = get_logger("mercury.stream")


def stream_signal(event: EventEnvelope) -> str | None:
    """The x-event-stream marker (lowercased), or None for an unmarked envelope."""
    for key, value in event.headers.items():
        if key.lower() == X_EVENT_STREAM:
            return value.lower()
    return None


def stream_event_name(event: EventEnvelope) -> str | None:
    """The x-event-name companion header (the SSE ``event:`` field), if any."""
    for key, value in event.headers.items():
        if key.lower() == X_EVENT_NAME:
            return value
    return None


def error_body(status: int, message: str) -> dict[str, Any]:
    """The standard error key-values: '{"type": "error", "status": n, "message": text}'"""
    return {"type": "error", "status": status, "message": message}


def exception_envelope(status: int, message: str) -> EventEnvelope:
    """An in-band exception envelope with the standard error body."""
    return (EventEnvelope()
            .set_header(X_EVENT_STREAM, EXCEPTION)
            .set_status(status)
            .set_body(error_body(status, message)))


def sse_frame(event_name: str | None, text: str) -> bytes:
    """One SSE frame: optional ``event:`` line, one ``data:`` line per text line."""
    lines = []
    if event_name:
        lines.append(f"event: {event_name}\n")
    for line in text.split("\n"):
        lines.append(f"data: {line}\n")
    lines.append("\n")
    return "".join(lines).encode("utf-8")


def envelope_frame(event: EventEnvelope) -> bytes:
    """One envelope-mode wire frame: the envelope serialized verbatim - with the
    host-internal addressing cleared, because the consuming relay rewrites
    addressing to the original caller - as base64 under the reserved name."""
    clone = EventEnvelope.from_map(event.to_map())
    clone.to = None
    clone.reply_to = None
    encoded = base64.b64encode(clone.to_bytes()).decode("ascii")
    return sse_frame(ENVELOPE, encoded)


def raw_streamable(event: EventEnvelope) -> bool:
    """A data segment may ride a raw SSE frame only when the frame carries it
    losslessly: a 200 status, no custom envelope headers, a user event name
    clear of the reserved word, and a text (or empty) body without a carriage
    return - SSE normalizes line endings. Everything else takes the
    envelope-frame escape hatch."""
    if event.get_status() != 200:
        return False
    for key, value in event.headers.items():
        lowered = key.lower()
        if lowered not in _RESERVED_HEADERS:
            return False
        if lowered == X_EVENT_NAME and value == ENVELOPE:
            return False
    body = event.body
    return body is None or (isinstance(body, str) and "\r" not in body)


def data_frame(event: EventEnvelope, first_frame: bool) -> bytes:
    """One envelope-mode data frame: the first event always rides an envelope
    frame (it carries the head control); a losslessly raw-able text segment
    rides a raw frame; a bare no-op segment carries nothing."""
    if first_frame or not raw_streamable(event):
        return envelope_frame(event)
    if event.body is None:
        return b""
    return sse_frame(stream_event_name(event), event.body)


def keep_alive_ms() -> int:
    """SSE keep-alive comment interval in ms (``event.stream.keep.alive``,
    default 30s; 0 disables - the engines' config key)."""
    raw = str(app_config().get_property("event.stream.keep.alive", "30s") or "30s")
    raw = raw.strip().lower()
    if raw in ("0", "0s", "0ms", "0m"):
        return 0
    try:
        if raw.endswith("ms"):
            return int(raw[:-2])
        if raw.endswith("s"):
            return int(raw[:-1]) * 1000
        if raw.endswith("m"):
            return int(raw[:-1]) * 60_000
        return int(raw) * 1000
    except ValueError:
        return 30_000


class SseParser:
    """Incremental SSE frame parser: byte-level line split (a newline is a
    single byte, so this is UTF-8 safe), one-leading-space value strip,
    comment/id/retry suppression, multi-line data joined per the SSE
    specification. Mirrors the engines' parsers."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._data_lines: list[str] = []
        self._event_name: str | None = None

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        """Feed one body chunk; return the completed (event_name, data) events."""
        self._pending.extend(chunk)
        events: list[tuple[str | None, str]] = []
        buffer = bytes(self._pending)
        start = 0
        for i, byte in enumerate(buffer):
            if byte != 0x0A:  # '\n'
                continue
            end = i - 1 if i > start and buffer[i - 1] == 0x0D else i
            self._on_line(buffer[start:end].decode("utf-8", errors="replace"), events)
            start = i + 1
        self._pending = bytearray(buffer[start:])
        return events

    def _on_line(self, line: str, events: list[tuple[str | None, str]]) -> None:
        """One SSE line: a blank line dispatches the pending event; a comment
        line (leading colon) is consumed, never forwarded; id, retry and
        unknown fields are ignored (SSE specification)."""
        if not line:
            if self._data_lines:
                events.append((self._event_name, "\n".join(self._data_lines)))
            self._data_lines = []
            self._event_name = None
            return
        if line.startswith(":"):
            return
        colon = line.find(":")
        field = line if colon == -1 else line[:colon]
        value = "" if colon == -1 else line[colon + 1:]
        value = value.removeprefix(" ")
        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            self._event_name = value


class EventStreamWriter:
    """Producer helper for a multi-shot reply - the engines' exact API.

    Only an interceptor function can stream: it receives the raw envelope, so
    the caller-provided reply address travels the engines' way
    (``EventStreamWriter.from_request(event)`` reads ``reply_to`` and the
    correlation id). Segments route to the LOCAL reply address through the
    primitive event bus - simple routing to a local function or reply sink,
    never across the wire (cross-wire replies ride the Event-over-HTTP SSE
    response, exactly as on the engines).
    """

    def __init__(self, reply_to: str | None, correlation_id: str | None = None, *,
                 registry: FunctionRegistry | None = None):
        if not reply_to:
            raise AppException(400, "Streaming producer requires a reply_to address")
        from .registry import default_registry
        self._registry = registry or default_registry
        self._reply_to = reply_to
        self._cid = correlation_id
        self._first_status = 200
        self._first_content_type: str | None = None
        self._first_ttl_seconds = 0
        self._head_sent = False
        self._closed = False

    @classmethod
    def from_request(cls, event: EventEnvelope, *,
                     registry: FunctionRegistry | None = None) -> EventStreamWriter:
        """Create a writer from the incoming request envelope (the usual form
        for an interceptor function)."""
        return cls(event.reply_to, event.cid, registry=registry)

    def first(self, status: int, content_type: str,
              ttl_seconds: int | None = None) -> EventStreamWriter:
        """Optional head control carried by the first outgoing event: response
        status, content type, and an optional idle-allowance override in
        seconds between segments."""
        self._first_status = int(status)
        self._first_content_type = content_type
        if ttl_seconds is not None:
            self._first_ttl_seconds = int(ttl_seconds)
        return self

    def write(self, segment: Any) -> None:
        """Send one ``data`` segment (text, bytes, dict, list - any payload)."""
        self._send(DATA, segment, None)

    def write_named(self, event_name: str, segment: Any) -> None:
        """Send one named segment - the name maps to the SSE ``event:`` field."""
        self._send(DATA, segment, event_name)

    def close(self, trailing_metadata: Any = None) -> None:
        """Declare end of transmission, with optional trailing metadata."""
        if self._closed:
            return
        self._closed = True
        self._send(EOF, trailing_metadata, None, unchecked=True)

    def fail(self, error: Exception) -> None:
        """Declare an in-band failure and end the stream."""
        if self._closed:
            return
        self._closed = True
        status = error.status if isinstance(error, AppException) else 500
        status = status if status >= 400 else 500
        message = str(error) or type(error).__name__
        event = self._envelope(EXCEPTION, error_body(status, message), None)
        event.set_status(status)
        self._emit(event)

    @property
    def closed(self) -> bool:
        """True when the stream has been closed or failed."""
        return self._closed

    def _send(self, marker: str, body: Any, event_name: str | None, *,
              unchecked: bool = False) -> None:
        if self._closed and not unchecked:
            log.debug("Segment to %s dropped - stream already closed", self._reply_to)
            return
        self._emit(self._envelope(marker, body, event_name))

    def _envelope(self, marker: str, body: Any, event_name: str | None) -> EventEnvelope:
        event = EventEnvelope(to=self._reply_to, body=body)
        event.set_header(X_EVENT_STREAM, marker)
        if self._cid:
            event.set_correlation_id(self._cid)
        if event_name:
            event.set_header(X_EVENT_NAME, event_name)
        # segments inherit the producer's identity, trace and span, so a
        # consuming engine's per-segment delivery spans parent onto this
        # function (the engines' po.send/touch parity)
        info = get_trace()
        if info and info.route:
            event.set_from(info.route)
        if info and info.trace_id:
            event.set_trace(info.trace_id, info.trace_path or self._reply_to)
            if info.span_id:
                event.set_span_id(info.span_id)
        if not self._head_sent:
            self._head_sent = True
            event.set_status(self._first_status)
            if self._first_content_type:
                event.set_header("content-type", self._first_content_type)
            if self._first_ttl_seconds > 0:
                event.set_header(X_TTL, str(self._first_ttl_seconds))
        return event

    def _emit(self, event: EventEnvelope) -> None:
        """Deliver to the local reply address - safe from async handlers and
        from plain-def handlers on executor threads (the sync bridge's host
        loop carries the delivery back to the event loop)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            from .bus import get_host_loop
            loop = get_host_loop()
            if loop is None:
                raise RuntimeError(
                    "No Mercury host event loop in context - the stream writer works "
                    "inside a hosted function") from None
            loop.call_soon_threadsafe(self._deliver, event)
        else:
            self._deliver(event)

    def _deliver(self, event: EventEnvelope) -> None:
        if not self._registry.send_event(event):
            log.warning("Event dropped - route %s not found", self._reply_to)
