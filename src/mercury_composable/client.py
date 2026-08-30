"""
The PostOffice — local and remote function calls with one envelope contract.

**Remote** (an ``endpoint`` is given, on the constructor or per call): sends
the event envelope to a peer's ``/api/event`` — a Java or Rust engine
application, or another polyglot function host — with the same HTTP contract
as the engines' relay: ``content-type: application/octet-stream``,
``accept: */*``, ``x-no-stream: true``, ``x-ttl`` (ms), ``x-async: true`` for
drop-n-forget, optional security headers, and trace headers (``X-Trace-Id``
plus a W3C ``traceparent`` when the trace id is W3C-shaped). The remote
target must be public (its host answers 403 for private routes).

**Local** (no endpoint): the call goes through this application's primitive
event bus to a locally registered function — private OR public, the engines'
semantics (``private`` means in-app only). Headers are delivered verbatim
(ingress hygiene applies to the wire, not to in-app calls), the ttl bounds
the wait with the standard 408 envelope, and the reply shape is identical to
the remote path. Local eventing is for simple leaf-side composition; workflow
processing belongs in Event Script and Knowledge Graph on the engines.

The decoded reply envelope is authoritative in both modes: an error rides
back as a normal envelope with status >= 400 — inspect ``reply.get_status()``.

**Sync bridge**: a plain ``def`` handler runs on an executor thread with no
event loop of its own, so it cannot ``await``. :meth:`PostOffice.request_sync`
and :meth:`PostOffice.send_sync` submit the same calls onto the host loop and
block only the handler's worker thread — never the event loop. The caller's
trace context rides across the bridge, so the trace chain is unbroken. Calling
the sync bridge from async code is refused with a teaching error (``await
request()`` is the async way).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import aiohttp

from .bus import DeliveryTimeout, get_host_loop
from .envelope import EventEnvelope
from .event_stream import (
    DATA,
    ENVELOPE,
    EOF,
    EXCEPTION,
    STREAM_CALLER_REQUIRED,
    TEXT_EVENT_STREAM,
    X_EVENT_NAME,
    X_EVENT_STREAM,
    SseParser,
    exception_envelope,
    stream_signal,
)
from .exceptions import AppException
from .registry import FunctionRegistry, default_registry
from .trace import MY_CID_TAG, RPC_TAG, _reset_trace, _set_trace, get_trace

_W3C_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_W3C_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def _build_event(route: str, body: Any, headers: dict[str, str] | None,
                 from_route: str | None, cid: str | None) -> EventEnvelope:
    """Build the outbound envelope, inheriting the current trace context."""
    event = EventEnvelope(to=route, body=body, headers=headers or {})
    if from_route:
        event.set_from(from_route)
    info = get_trace()
    # fill the sender with the executing function's route (touch parity)
    if info and info.route and not event.sender:
        event.set_from(info.route)
    if info and info.trace_id:
        event.set_trace(info.trace_id, info.trace_path or route)
    effective_cid = cid or (info.cid if info else None)
    if effective_cid:
        event.set_correlation_id(effective_cid)
    # propagate the business correlation-id to the next touch point as the
    # engine-managed my_cid tag (the engines' PostOffice.touch parity) - the
    # receiving host injects it as the read-only my_correlation_id header
    if info and info.my_correlation_id and MY_CID_TAG not in event.tags:
        event.tags[MY_CID_TAG] = info.my_correlation_id
    # carry this execution's span so the receiver stores it as its
    # parent_span_id (touch parity) - also lights up the traceparent header
    if info and info.span_id:
        event.set_span_id(info.span_id)
    return event


class PostOffice:
    """Event-over-HTTP client for calling functions on peer applications."""

    def __init__(self, endpoint: str | None = None,
                 security_headers: dict[str, str] | None = None,
                 registry: FunctionRegistry | None = None):
        self.endpoint = endpoint
        self.security_headers = dict(security_headers or {})
        self._registry = registry or default_registry
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        # called from the running event loop only (inside request/send)
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
            self._session = session
        return session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # PYI034 wants '-> Self', which needs python >= 3.11; switch when the
    # floor moves past 3.10
    async def __aenter__(self) -> PostOffice:  # noqa: PYI034
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _http_headers(self, timeout_ms: int, is_async: bool,
                      event: EventEnvelope) -> dict[str, str]:
        headers = {
            "content-type": "application/octet-stream",
            "accept": "*/*",
            "x-no-stream": "true",
            "x-ttl": str(max(100, timeout_ms)),
        }
        if is_async:
            headers["x-async"] = "true"
        for key, value in self.security_headers.items():
            if key.lower() != "x-event-format":
                headers[key] = value
        if event.trace_id:
            headers["X-Trace-Id"] = event.trace_id
            if _W3C_TRACE_ID.fullmatch(event.trace_id) and event.span_id \
                    and _W3C_SPAN_ID.fullmatch(event.span_id):
                headers["traceparent"] = f"00-{event.trace_id}-{event.span_id}-01"
        return headers

    async def _call_local(self, route: str, body: Any, headers: dict[str, str] | None,
                          timeout_ms: int, is_async: bool, from_route: str | None,
                          cid: str | None) -> EventEnvelope:
        """In-app delivery through the primitive event bus (private OR public)."""
        service = self._registry.get(route)
        if service is None:
            return EventEnvelope().set_status(404).set_body(f"Route {route} not found")
        event = _build_event(route, body, headers, from_route, cid)
        if not is_async:
            # the engines' RPC round-trip marker: an RPC leg emits no trace
            # dataset - its metrics fold into the caller's view
            event.tags.setdefault(RPC_TAG, str(timeout_ms))
        bus = self._registry.bus
        if service.interceptor:
            if is_async:
                bus.publish_envelope(service, event)
                from .bus import async_ack
                return async_ack()
            # RPC to an interceptor: a per-request reply sink is the reply
            # address; the first envelope classifies exactly like the engines -
            # unmarked = the reply; marked = a streaming target refusing a
            # single-shot caller (the pinned 406)
            sink_route, queue = bus.open_sink()
            try:
                bus.publish_envelope(service, event.set_reply_to(sink_route))
                try:
                    first = await asyncio.wait_for(queue.get(), max(100, timeout_ms) / 1000)
                except asyncio.TimeoutError:
                    return EventEnvelope().set_status(408).set_body(
                        f"Timeout for {timeout_ms} ms")
                if stream_signal(first) is not None:
                    return EventEnvelope().set_status(406).set_body(STREAM_CALLER_REQUIRED)
                return first
            finally:
                bus.close_sink(sink_route)
        if is_async:
            return bus.publish(service, event.headers, event.body, trace_id=event.trace_id,
                               trace_path=event.trace_path, cid=event.cid, envelope=event)
        try:
            return await bus.deliver(service, event.headers, event.body, timeout_ms,
                                     trace_id=event.trace_id, trace_path=event.trace_path,
                                     cid=event.cid, envelope=event)
        except DeliveryTimeout:
            return EventEnvelope().set_status(408).set_body(f"Timeout for {timeout_ms} ms")

    async def _call(self, route: str, body: Any, headers: dict[str, str] | None,
                    timeout_ms: int, endpoint: str | None, is_async: bool,
                    from_route: str | None, cid: str | None) -> EventEnvelope:
        url = endpoint or self.endpoint
        if not url:
            # no endpoint = local: the engines' semantics for an in-app po call
            return await self._call_local(route, body, headers, timeout_ms,
                                          is_async, from_route, cid)
        event = _build_event(route, body, headers, from_route, cid)
        if not is_async:
            # the engines' RPC round-trip marker (see _call_local)
            event.tags.setdefault(RPC_TAG, str(timeout_ms))
        session = self._get_session()
        # +100 ms cushion so the HTTP client does not time out before the target
        client_timeout = aiohttp.ClientTimeout(total=(max(100, timeout_ms) + 100) / 1000)
        async with session.post(url, data=event.to_bytes(),
                                headers=self._http_headers(timeout_ms, is_async, event),
                                timeout=client_timeout) as response:
            payload = await response.read()
            try:
                return EventEnvelope.from_bytes(payload)
            except ValueError as e:
                raise AppException(response.status,
                                   f"Invalid event-over-http response - {e}") from e

    async def request(self, route: str, body: Any = None, *,
                      headers: dict[str, str] | None = None,
                      timeout_ms: int = 30000,
                      endpoint: str | None = None,
                      from_route: str | None = None,
                      cid: str | None = None) -> EventEnvelope:
        """RPC call: returns the target function's reply envelope."""
        return await self._call(route, body, headers, timeout_ms, endpoint,
                                False, from_route, cid)

    async def send(self, route: str, body: Any = None, *,
                   headers: dict[str, str] | None = None,
                   timeout_ms: int = 30000,
                   endpoint: str | None = None,
                   from_route: str | None = None,
                   cid: str | None = None) -> EventEnvelope:
        """Drop-n-forget: returns the peer's 202 delivery acknowledgement envelope."""
        return await self._call(route, body, headers, timeout_ms, endpoint,
                                True, from_route, cid)

    async def stream(self, route: str, body: Any = None, *,
                     headers: dict[str, str] | None = None,
                     timeout_ms: int = 30000,
                     endpoint: str | None = None,
                     from_route: str | None = None,
                     cid: str | None = None) -> AsyncIterator[EventEnvelope]:
        """Consume a streaming function progressively - the same decoded
        envelopes an engine reply route receives: ``data`` segments, then the
        ``eof`` or ``exception`` terminal. A non-streaming target yields its
        one classic reply (opting in is always safe). ``timeout_ms`` is the
        idle allowance between segments; expiry, a truncated stream and a
        malformed dialect yield the in-band exception envelope, then end.

        Remote (an endpoint is given, or set on the constructor): the peer's
        ``/api/event`` answers the one POST with the envelope-mode SSE dialect.
        Local (no endpoint): the same first-envelope classification through a
        per-request reply sink on the primitive bus.
        """
        url = endpoint or self.endpoint
        event = _build_event(route, body, headers, from_route, cid)
        if not url:
            async for reply in self._stream_local(route, event, timeout_ms):
                yield reply
            return
        async for reply in self._stream_remote(url, event, timeout_ms):
            yield reply

    async def stream_to(self, route: str, body: Any = None, *,
                        reply_to: str,
                        headers: dict[str, str] | None = None,
                        timeout_ms: int = 30000,
                        endpoint: str | None = None,
                        from_route: str | None = None,
                        cid: str | None = None) -> EventEnvelope:
        """The relay form of :meth:`stream` for composition: every decoded
        envelope forwards verbatim to the LOCAL ``reply_to`` route (typically
        the caller's own reply address, handed through by an interceptor), so
        segments flow remote peer -> this application -> the original caller
        with no buffering. Awaits and returns the last envelope (normally the
        terminal)."""
        last = EventEnvelope().set_status(500).set_body("Stream produced no events")
        async for segment in self.stream(route, body, headers=headers,
                                         timeout_ms=timeout_ms, endpoint=endpoint,
                                         from_route=from_route, cid=cid):
            last = segment
            forward = EventEnvelope.from_map(segment.to_map()).set_to(reply_to)
            if not self._registry.send_event(forward):
                # the local consumer is gone - late segments are no-op drops
                break
        return last

    async def _stream_local(self, route: str, event: EventEnvelope,
                            timeout_ms: int) -> AsyncIterator[EventEnvelope]:
        service = self._registry.get(route)
        if service is None:
            yield EventEnvelope().set_status(404).set_body(f"Route {route} not found")
            return
        if not service.interceptor:
            # a plain function cannot stream - its single reply is the stream
            yield await self._call_local(route, event.body, event.headers,
                                         timeout_ms, False, event.sender, event.cid)
            return
        bus = self._registry.bus
        sink_route, queue = bus.open_sink()
        try:
            bus.publish_envelope(service, event.set_reply_to(sink_route))
            idle = max(100, timeout_ms) / 1000
            streaming = False
            while True:
                try:
                    reply = await asyncio.wait_for(queue.get(), idle)
                except asyncio.TimeoutError:
                    seconds = max(100, timeout_ms) // 1000
                    yield exception_envelope(408, f"Timeout for {seconds} seconds")
                    return
                out, done = _classify_sink_reply(reply, streaming)
                streaming = True
                yield out
                if done:
                    return
        finally:
            bus.close_sink(sink_route)

    async def _stream_remote(self, url: str, event: EventEnvelope,
                             timeout_ms: int) -> AsyncIterator[EventEnvelope]:
        effective_cid = event.cid
        http_headers = self._http_headers(timeout_ms, False, event)
        http_headers["accept"] = TEXT_EVENT_STREAM
        idle_seconds = max(1.0, timeout_ms / 1000)
        # no total limit - a healthy stream may outlive any fixed total; the
        # per-read socket timeout is the idle allowance between segments
        client_timeout = aiohttp.ClientTimeout(total=None, sock_connect=10,
                                               sock_read=idle_seconds)
        session = self._get_session()
        async with session.post(url, data=event.to_bytes(), headers=http_headers,
                                timeout=client_timeout) as response:
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith(TEXT_EVENT_STREAM):
                # the peer answered single-shot (a non-streaming target, or an
                # edge error) - the classic reply, decoded tolerantly
                yield _decode_single_shot(await response.read(), response.status)
                return
            parser = SseParser()
            head_seen = False
            try:
                async for chunk in response.content.iter_any():
                    for name, text in parser.feed(chunk):
                        reply, terminal = _decode_frame(name, text, head_seen,
                                                        effective_cid)
                        if reply is None:
                            continue
                        head_seen = True
                        yield reply
                        if terminal:
                            return  # frames after the terminal are discarded
                # the dialect ends with a decoded terminal - a bare transport
                # end is a truncation
                yield _relay_guard(500, "Event stream ended without eof", effective_cid)
            except asyncio.TimeoutError:
                yield _relay_guard(408, f"Timeout for {int(idle_seconds)} seconds",
                                   effective_cid)
            except aiohttp.ClientError as e:
                yield _relay_guard(500, str(e) or type(e).__name__, effective_cid)

    @staticmethod
    def _run_sync(factory: Callable[[], Coroutine[Any, Any, EventEnvelope]],
                  timeout_ms: int) -> EventEnvelope:
        """Run a PostOffice coroutine from a sync handler's thread on the host loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no loop in this thread - the sync bridge is applicable
        else:
            raise RuntimeError(
                "request_sync()/send_sync() must not be called on the event loop - "
                "await request()/send() instead")
        loop = get_host_loop()
        if loop is None:
            raise RuntimeError(
                "No Mercury host event loop in context - the sync bridge works inside "
                "a hosted sync function; elsewhere use asyncio.run(po.request(...))")
        info = get_trace()  # carried into this thread by the bus dispatch

        async def bridged() -> EventEnvelope:
            if info is None:
                return await factory()
            # a submitted Task runs in its own context - re-establish the caller's
            # trace there (the SAME TraceInfo object, so the chain is unbroken)
            token = _set_trace(info)
            try:
                return await factory()
            finally:
                _reset_trace(token)

        future = asyncio.run_coroutine_threadsafe(bridged(), loop)
        try:
            # inner deadlines already shape 408 envelopes; this outer margin only
            # guards a wedged path so an executor thread can never hang forever
            return future.result(timeout=max(100, timeout_ms) / 1000 + 10)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return EventEnvelope().set_status(408).set_body(f"Timeout for {timeout_ms} ms")

    def request_sync(self, route: str, body: Any = None, *,
                     headers: dict[str, str] | None = None,
                     timeout_ms: int = 30000,
                     endpoint: str | None = None,
                     from_route: str | None = None,
                     cid: str | None = None) -> EventEnvelope:
        """RPC from a plain ``def`` handler: blocks this worker thread only,
        never the event loop, while the call runs on the host loop."""
        return self._run_sync(
            lambda: self.request(route, body, headers=headers, timeout_ms=timeout_ms,
                                 endpoint=endpoint, from_route=from_route, cid=cid),
            timeout_ms)

    def send_sync(self, route: str, body: Any = None, *,
                  headers: dict[str, str] | None = None,
                  timeout_ms: int = 30000,
                  endpoint: str | None = None,
                  from_route: str | None = None,
                  cid: str | None = None) -> EventEnvelope:
        """Drop-n-forget from a plain ``def`` handler (see :meth:`request_sync`)."""
        return self._run_sync(
            lambda: self.send(route, body, headers=headers, timeout_ms=timeout_ms,
                              endpoint=endpoint, from_route=from_route, cid=cid),
            timeout_ms)


def _classify_sink_reply(reply: EventEnvelope,
                         streaming: bool) -> tuple[EventEnvelope, bool]:
    """Classify one reply-sink envelope exactly like the engines: unmarked
    before any segment = the classic single-shot answer; unmarked mid-stream =
    the bus's error contract for an uncaught interceptor exception (fails
    in-band); marked = a stream segment, terminal on eof/exception."""
    marker = stream_signal(reply)
    if marker is None:
        if streaming:
            message = str(reply.body) if reply.body is not None else "Stream failed"
            return exception_envelope(reply.get_status(), message), True
        return reply, True
    return reply, marker in (EOF, EXCEPTION)


def _relay_guard(status: int, message: str, cid: str | None) -> EventEnvelope:
    """An in-band exception envelope synthesized by the consuming relay."""
    event = exception_envelope(status, message)
    if cid:
        event.set_correlation_id(cid)
    return event


def _decode_frame(name: str | None, text: str, head_seen: bool,
                  cid: str | None) -> tuple[EventEnvelope | None, bool]:
    """Decode one SSE frame of the envelope-mode dialect: an ``envelope`` frame
    is one base64-encoded serialized envelope (the head, the terminals and
    non-text segments); any other frame is a raw text segment. Returns
    (envelope-or-None, terminal). Dialect guards fail in-band: the first frame
    must be an envelope frame, and a malformed frame ends the stream."""
    if name == ENVELOPE:
        try:
            # binascii.Error is a ValueError subclass - one catch covers both
            decoded = EventEnvelope.from_bytes(base64.b64decode(text, validate=True))
        except ValueError:
            return _relay_guard(500, "Invalid event stream - malformed envelope frame",
                                cid), True
        decoded.to = None
        decoded.reply_to = None
        if cid:
            decoded.set_correlation_id(cid)
        marker = stream_signal(decoded)
        return decoded, marker in (EOF, EXCEPTION)
    if not head_seen:
        # the dialect guarantees an envelope frame first (conformance guard)
        return _relay_guard(500, "Invalid event stream - missing envelope head",
                            cid), True
    segment = EventEnvelope(body=text).set_header(X_EVENT_STREAM, DATA)
    if name:
        segment.set_header(X_EVENT_NAME, name)
    if cid:
        segment.set_correlation_id(cid)
    return segment, False


def _decode_single_shot(payload: bytes, http_status: int) -> EventEnvelope:
    """Decode a single-shot Event-over-HTTP reply: a serialized envelope
    normally, with the classic tolerant handling of an edge-level REST error
    body ('{"type": "error", "status": n, "message": text}' JSON) and of a
    payload that is not a serialized envelope at all."""
    if not payload:
        return EventEnvelope().set_status(http_status)
    try:
        reply = EventEnvelope.from_bytes(payload)
    except ValueError as e:
        if http_status >= 400:
            try:
                data = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and data.get("type") == "error" \
                    and isinstance(data.get("message"), str):
                return EventEnvelope().set_status(http_status).set_body(data["message"])
        return EventEnvelope().set_status(400).set_body(
            f"Invalid event-over-http response - {e}")
    reply.reply_to = None
    return reply
