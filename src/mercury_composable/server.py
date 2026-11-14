"""
The Event API host: POST /api/event, exactly as the engines speak it.

Behavior mirrors the Java engine's EventApiService:

- Request body = event envelope bytes; ``x-ttl`` header (ms, floor 1000) bounds
  the handler execution; ``x-async: true`` means drop-n-forget (HTTP 202 with an
  ack envelope ``{type: async, delivered: true, time}``).
- Reply body is always envelope bytes with ``content-type:
  application/octet-stream``. Transport-level failures produced by this host
  (400 undecodable / missing route, 403 private, 404 unknown route, 408
  timeout) set the HTTP status as well; a handler's own result — including an
  AppException or an unexpected exception — rides HTTP 200 with the status
  inside the envelope, exactly like an engine function's reply.
- Reserved header hygiene: ``x-event-api`` (the engines' relay recursion
  guard) and transported ``my_*`` keys are removed from the handler's header
  view; the ``my_cid`` tag is injected as the read-only ``my_correlation_id``
  header, per the wire-format contract.
- The host also serves the engines' actuator endpoints (``/info``,
  ``/info/routes``, ``/env``, ``/health``, ``/livenessprobe``) for
  operations and Kubernetes probes - see :mod:`mercury_composable.actuator`.
- A traced call leaves the records a Java or Rust callee leaves for the same
  call, so a span lineage stays continuous into this host: ``http.request``
  (``POST /api/event``, the edge's round trip, parented onto the caller's span
  - the envelope's span, else the ``traceparent`` header's), ``event.api.service``
  (the ingress, parented onto the edge) and, for an RPC, the function's
  round-trip record with the responder's span (the caller side's record in
  the engines); a drop-n-forget or streaming function emits its own record,
  parented onto the service. An untraced call records nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from . import otel
from .actuator import Actuator, app_origin
from .bus import DeliveryTimeout, EventBus, round_trip_record
from .config import app_config
from .envelope import EventEnvelope, iso_utc
from .event_stream import (
    DATA,
    STREAM_CALLER_REQUIRED,
    TEXT_EVENT_STREAM,
    data_frame,
    envelope_frame,
    exception_envelope,
    keep_alive_ms,
    stream_signal,
)
from .log import get_logger
from .registry import FunctionRegistry, ServiceDef, default_registry
from .trace import MY_CID_TAG, MY_CORRELATION_ID

OCTET_STREAM = "application/octet-stream"
X_TTL = "x-ttl"
X_ASYNC = "x-async"
X_EVENT_API = "x-event-api"
# the engines' reserved route name for the Event-over-HTTP ingress
EVENT_API_SERVICE = "event.api.service"
# the edge's own service name and path in its round-trip record (the engines' REST edge)
HTTP_REQUEST = "http.request"
EDGE_PATH = "POST /api/event"
TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")

log = get_logger("mercury.server")


def _transport_error(status: int, message: str) -> web.Response:
    reply = EventEnvelope().set_status(status).set_body(message)
    return web.Response(status=status, body=reply.to_bytes(), content_type=OCTET_STREAM)


def _caller_span(request: web.Request, event: EventEnvelope) -> str | None:
    """The remote caller's span: the one on the envelope (the engines' PostOffice stamps
    the calling function's span), else the W3C traceparent header's."""
    if event.span_id:
        return event.span_id
    match = TRACEPARENT.match((request.headers.get("traceparent") or "").strip().lower())
    return match.group(2) if match else None


class _Edge:
    """The trace records of one traced call through the /api/event edge - what a Java or
    Rust callee leaves for the same call: ``http.request`` (the edge's round trip, the
    backend's SERVER span, parented onto the caller's span) and ``event.api.service`` (the
    ingress, parented onto the edge); the relayed event carries the service's span, so the
    function parents onto it. An RPC adds the function's round-trip record - the caller
    side's record in the engines (InboxBase) - with the responder's span from the reply."""

    def __init__(self, bus: EventBus, event: EventEnvelope, caller_span: str | None) -> None:
        self.bus = bus
        self.trace_id = event.trace_id or ""
        self.trace_path = event.trace_path
        self.route = event.to or ""
        self.caller_span = caller_span
        self.edge_span = secrets.token_hex(8)
        self.service_span = secrets.token_hex(8)
        self.start = iso_utc()
        self.started = time.perf_counter()

    def _elapsed(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 3)

    def record_service(self) -> None:
        """The ingress record, when the event is handed on - the engines' EventApiService
        execution, parented onto the edge."""
        self.bus.emit({
            "origin": app_origin(), "id": self.trace_id, "path": EDGE_PATH, "service": EVENT_API_SERVICE,
            "start": self.start, "from": HTTP_REQUEST, "exec_time": self._elapsed(), "status": 200,
            "success": True, "span_id": self.service_span, "parent_span_id": self.edge_span,
        })

    def record_function(self, sender: str | None, reply: EventEnvelope, dispatched: str,
                        started: float) -> None:
        """The RPC's round-trip record, parented onto the service - the engines' InboxBase
        record for the relayed call."""
        self.bus.emit(round_trip_record(service=self.route, sender=sender, trace_id=self.trace_id,
                                        trace_path=self.trace_path, reply=reply,
                                        parent_span_id=self.service_span, start=dispatched,
                                        round_trip_ms=(time.perf_counter() - started) * 1000),
                      reply.annotations)

    def record_edge(self, status: int, error: str | None = None) -> None:
        """The edge's round-trip record when the response is complete - the engines' REST
        edge: the whole call under the caller's span, the HTTP status sent."""
        trace: dict[str, Any] = {
            "origin": app_origin(), "id": self.trace_id, "path": EDGE_PATH, "service": HTTP_REQUEST,
            "start": self.start, "exec_time": self._elapsed(), "status": status, "success": status < 400,
            "span_id": self.edge_span,
        }
        if self.caller_span:
            trace["parent_span_id"] = self.caller_span
        if status >= 400 and error:
            trace["exception"] = error
        self.bus.emit(trace)


def _handler_headers(event: EventEnvelope) -> dict[str, str]:
    headers = {k: v for k, v in event.headers.items()
               if k.lower() != X_EVENT_API and not k.lower().startswith("my_")}
    my_cid = event.tags.get(MY_CID_TAG)
    if my_cid:
        headers[MY_CORRELATION_ID] = my_cid
    return headers


class EventApiServer:
    """Thin ingress: protocol guards + header hygiene, then the registry's bus."""

    def __init__(self, registry: FunctionRegistry | None = None):
        self.registry = registry or default_registry
        self.actuator = Actuator(self.registry)

    async def handle_event(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        try:
            ttl = int(request.headers.get(X_TTL, "0") or 0)
        except ValueError:
            ttl = 0
        ttl = max(1000, ttl)
        is_async = request.headers.get(X_ASYNC, "") == "true"
        try:
            event = EventEnvelope.from_bytes(raw)
        # CompactFormatError is a ValueError - one catch covers the codec errors
        except ValueError as e:
            return _transport_error(400, str(e))
        if not event.to:
            return _transport_error(400, "Missing routing path")
        bus = self.registry.bus
        edge = _Edge(bus, event, _caller_span(request, event)) if event.trace_id else None
        service = self.registry.get(event.to)
        if service is None:
            return self._refuse(edge, 404, f"Route {event.to} not found")
        if service.private:
            return self._refuse(edge, 403, f"{event.to} is private")
        if not event.sender:
            # the engines' EventApiService parity: its PostOffice fills the
            # sender with its own route when the wire envelope carries none
            event.set_from(EVENT_API_SERVICE)
        if edge:
            # the relayed event carries the service's span, so the function
            # parents onto it - the engines' relay
            event.set_span_id(edge.service_span)
            edge.record_service()
        headers = _handler_headers(event)
        if is_async:
            ack = bus.publish(service, headers, event.body, trace_id=event.trace_id,
                              trace_path=event.trace_path, cid=event.cid, envelope=event)
            if edge:
                edge.record_edge(202)
            return web.Response(status=202, body=ack.to_bytes(), content_type=OCTET_STREAM)
        if service.interceptor:
            # interceptor dispatch (the reply_to mechanism): the handler
            # receives the raw envelope with a per-request reply sink as its
            # reply address and answers manually - single-shot or streaming
            capable = TEXT_EVENT_STREAM in (request.headers.get("accept") or "")
            return await self._dispatch_interceptor(request, service, event, headers,
                                                    ttl, capable, edge)
        dispatched, started = iso_utc(), time.perf_counter()
        try:
            reply = await bus.deliver(service, headers, event.body, ttl,
                                      trace_id=event.trace_id, trace_path=event.trace_path,
                                      cid=event.cid, envelope=event)
        except DeliveryTimeout:
            log.warning("Event %s timeout for %d ms (trace_id=%s)", event.to, ttl, event.trace_id)
            if edge:
                timeout = EventEnvelope().set_status(408).set_body(f"Timeout for {ttl} ms")
                edge.record_function(event.sender, timeout, dispatched, started)
                edge.record_edge(408, f"Timeout for {ttl} ms")
            return _transport_error(408, f"Timeout for {ttl} ms")
        log.info("Handled %s status=%d exec_time=%sms trace_id=%s",
                 event.to, reply.get_status(), reply.exec_time, event.trace_id)
        if edge:
            edge.record_function(event.sender, reply, dispatched, started)
            edge.record_edge(200)
        return web.Response(status=200, body=reply.to_bytes(), content_type=OCTET_STREAM)

    @staticmethod
    def _refuse(edge: _Edge | None, status: int, message: str) -> web.Response:
        """A transport refusal, recorded at the edge when the call was traced."""
        if edge:
            edge.record_service()
            edge.record_edge(status, message)
        return _transport_error(status, message)

    async def _dispatch_interceptor(self, request: web.Request, service: ServiceDef,
                                    event: EventEnvelope, headers: dict[str, str],
                                    ttl: int, capable: bool,
                                    edge: _Edge | None = None) -> web.StreamResponse:
        """Dispatch to an interceptor and classify its first reply exactly like
        the engines: unmarked = the classic single-shot response, byte
        identical; marked = the envelope-mode SSE dialect for a caller that
        accepts text/event-stream, or the pinned 406 refusal for one that
        does not."""
        bus = self.registry.bus
        sink_route, queue = bus.open_sink()
        try:
            handler_event = EventEnvelope(to=event.to, body=event.body, headers=headers)
            handler_event.set_reply_to(sink_route)
            if event.tags:
                # engine-managed tags (e.g. the business correlation-id) ride
                # the delivered envelope verbatim, the engines' way
                handler_event.tags = dict(event.tags)
            if event.cid:
                handler_event.set_correlation_id(event.cid)
            if event.trace_id:
                handler_event.set_trace(event.trace_id, event.trace_path or service.route)
            if event.span_id:
                # the service's span (the caller's when the call is untraced at the
                # edge) - the handler's span parents onto it
                handler_event.set_span_id(event.span_id)
            if event.sender:
                handler_event.set_from(event.sender)
            bus.publish_envelope(service, handler_event)
            try:
                first = await asyncio.wait_for(queue.get(), ttl / 1000)
            except asyncio.TimeoutError:
                log.warning("Event %s timeout for %d ms (trace_id=%s)",
                            event.to, ttl, event.trace_id)
                if edge:
                    edge.record_edge(408, f"Timeout for {ttl} ms")
                return _transport_error(408, f"Timeout for {ttl} ms")
            marker = stream_signal(first)
            if marker is None:
                # the classic single-shot reply (a manual answer, or the bus's
                # error contract for an uncaught interceptor exception)
                log.info("Handled %s status=%d exec_time=%sms trace_id=%s",
                         event.to, first.get_status(), first.exec_time, event.trace_id)
                if edge:
                    edge.record_edge(200)
                return web.Response(status=200, body=first.to_bytes(),
                                    content_type=OCTET_STREAM)
            if not capable:
                # a streaming reply cannot ride a single-shot response
                if edge:
                    edge.record_edge(406, STREAM_CALLER_REQUIRED)
                return _transport_error(406, STREAM_CALLER_REQUIRED)
            response, status = await self._stream_response(request, queue, first, marker, ttl)
            if edge:
                # the engines' edge: a stream failing in-band after its head reports
                # the failure's own status
                edge.record_edge(status)
            return response
        finally:
            bus.close_sink(sink_route)

    async def _stream_response(self, request: web.Request,
                               queue: asyncio.Queue[EventEnvelope],
                               first: EventEnvelope, first_marker: str,
                               ttl: int) -> tuple[web.StreamResponse, int]:
        """Render the envelope-mode SSE dialect: envelope frames for the head,
        the terminals and non-text segments; raw frames for plain text. The
        x-ttl allowance (overridable by the producer's head control, in
        seconds) is the per-segment idle; expiry fails the stream in-band with
        the standard 408 error body. Keep-alive comments ride while the
        producer is quiet (event.stream.keep.alive, the engines' key)."""
        idle_ms = ttl
        for key, value in first.headers.items():
            if key.lower() == X_TTL:
                with contextlib.suppress(ValueError):
                    seconds = int(str(value).strip())
                    if seconds > 0:
                        idle_ms = seconds * 1000
        response = web.StreamResponse(status=first.get_status())
        response.headers["content-type"] = TEXT_EVENT_STREAM
        response.headers.setdefault("cache-control", "no-cache")
        await response.prepare(request)
        status = first.get_status()
        try:
            await response.write(envelope_frame(first))
            if first_marker == DATA:
                status = await self._stream_segments(response, queue, idle_ms)
        except ConnectionError:
            # a disconnected client ends the stream; late segments are no-op drops
            log.debug("Client disconnected from event stream")
        with contextlib.suppress(ConnectionError):
            await response.write_eof()
        return response, status

    async def _stream_segments(self, response: web.StreamResponse,
                               queue: asyncio.Queue[EventEnvelope], idle_ms: int) -> int:
        """Render the segments until the terminal; the status returned is the stream's
        outcome - 200, or an in-band failure's own status - for the edge's record."""
        ping_ms = keep_alive_ms()
        while True:
            event = await self._next_segment(response, queue, idle_ms, ping_ms)
            if event is None:
                # idle expiry - fail in-band (the engines' housekeeper parity)
                seconds = idle_ms // 1000
                frame = envelope_frame(exception_envelope(408, f"Timeout for {seconds} seconds"))
                await response.write(frame)
                return 408
            marker = stream_signal(event)
            if marker == DATA:
                frame = data_frame(event, first_frame=False)
                if frame:
                    await response.write(frame)
            elif marker is not None:
                # eof or exception: the terminal envelope frame ends the
                # response cleanly - no cosmetic frames on this wire
                await response.write(envelope_frame(event))
                return event.get_status() if event.has_error() else 200
            elif event.has_error():
                # the bus's error contract for an uncaught interceptor
                # exception mid-stream - fail in-band with the exact status
                message = str(event.body) if event.body is not None else "Stream failed"
                frame = envelope_frame(exception_envelope(event.get_status(), message))
                await response.write(frame)
                return event.get_status()
            else:
                log.warning("Dropping event - invalid %s signal", "x-event-stream")

    @staticmethod
    async def _next_segment(response: web.StreamResponse,
                            queue: asyncio.Queue[EventEnvelope],
                            idle_ms: int, ping_ms: int) -> EventEnvelope | None:
        """Wait for the next segment within the idle allowance, emitting SSE
        keep-alive comments while the producer is quiet (best-effort; pings
        never extend the idle allowance)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + idle_ms / 1000
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            wait = min(remaining, ping_ms / 1000) if ping_ms > 0 else remaining
            try:
                return await asyncio.wait_for(queue.get(), wait)
            except asyncio.TimeoutError:
                if loop.time() >= deadline:
                    return None
                with contextlib.suppress(ConnectionError):
                    await response.write(b": ping\n\n")

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_post("/api/event", self.handle_event)
        # the engines' landing page + actuator endpoints (see actuator.py)
        for path in ("/", "/info", "/info/routes", "/env", "/health", "/livenessprobe"):
            app.router.add_get(path, self.actuator.handle)
        # any other path or method answers the engines' error shape, not
        # aiohttp's default text page (exact routes above win - they are
        # registered first)
        app.router.add_route("*", "/{unknown:.*}", self.actuator.handle)
        return app


class Platform:
    """Runs the Event API host for the default (or a given) registry."""

    def __init__(self, registry: FunctionRegistry | None = None):
        self.registry = registry or default_registry
        self._startup: list[Callable[[], Awaitable[None]]] = []
        self._cleanup: list[Callable[[], Awaitable[None]]] = []

    def on_startup(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run an async callback inside the server's event loop before the first request is
        served - the place for an extension's asynchronous boot work, such as connecting to a
        peer and registering the routes it serves. A callback that raises stops the application
        before it listens, so a boot failure is a failure to start, never a run-time surprise."""
        self._startup.append(callback)

    def on_cleanup(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run an async callback when the application shuts down, after the last request."""
        self._cleanup.append(callback)

    def create_app(self) -> web.Application:
        """The Event API application with this platform's startup and cleanup callbacks
        attached, and the opt-in OpenTelemetry forwarder activated on its extension route;
        ``run`` serves it, and a test may serve it with aiohttp's own runner."""
        config = app_config()
        # the opt-in OpenTelemetry forwarder registers on the extension route
        # before the route list is announced; its HTTP client closes with the app
        exporter = otel.activate(config, self.registry)
        app = EventApiServer(self.registry).create_app()
        for callback in self._startup:
            app.on_startup.append(_lifecycle(callback))
        for callback in self._cleanup:
            app.on_cleanup.append(_lifecycle(callback))
        if exporter is not None:
            async def _close_exporter(_app: web.Application) -> None:
                await exporter.close()
            app.on_cleanup.append(_close_exporter)
        return app

    def run(self, port: int | None = None, host: str = "127.0.0.1") -> None:
        config = app_config()
        app_name = config.get_property("application.name", "application")
        actual_port = int(port if port is not None else config.get("rest.server.port", 8085))
        app = self.create_app()
        for route, service in sorted(self.registry.routes().items()):
            visibility = "PRIVATE" if service.private else "PUBLIC"
            log.info("Loaded %s %s, instances=%d", visibility, route, service.instances)
        log.info("Configuration: %s", config.source)
        log.info("%s - Event API service started on port %d", app_name, actual_port)
        web.run_app(app, host=host, port=actual_port, print=None, handle_signals=True)


def _lifecycle(callback: Callable[[], Awaitable[None]]) -> Callable[[web.Application], Awaitable[None]]:
    async def run(_app: web.Application) -> None:
        await callback()
    return run


platform = Platform()
