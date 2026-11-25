"""REST automation: ``rest.yaml`` served by the host (RFC-0018 in mercury-composable).

With ``rest.automation: true`` the host reads one ``yaml.rest.automation`` location (default
``classpath:/rest.yaml``, read as ``resources/rest.yaml`` next to the application file under the
pack's resources rule; ``file:`` prefixes an absolute path) and serves its entries on the one
server, beside ``POST /api/event`` and the actuators, which become the engines' default entries so
a user entry can attach ``authentication``, CORS or another timeout to them. The grammar, the keys,
the messages and the error body are the engines' - the pure half lives in
:mod:`mercury_composable.rest_routing`, pinned by the shared vector file; this module is the edge:

- a request is matched to its entry (a known path under a wrong method is 405, the CORS preflight
  answers 204 with the ``options`` lines, no CORS block means 405 for ``OPTIONS``);
- the entry's request transform applies, the trace is resolved (a W3C ``traceparent`` first, the
  trace-id header next, else a fresh id when ``tracing`` is on; the caller's span is the edge's
  parent), the correlation id is always ensured and echoed, and the entry's timeout rides the
  request as ``x-ttl`` in milliseconds unless the caller sent one;
- the function receives the whole request as the ``AsyncHttpRequest`` map, after the optional
  authentication function whose ``true`` verdict lets the request through and whose headers ride as
  the ``session`` map; its reply becomes the response with the engines' rules (a returned envelope
  sets the status and headers; a failure renders as the standard error body);
- ``upload: true`` on a POST entry reads a ``multipart/form-data`` body: each file arrives in the
  request map under the engines' keys (``filename``, ``file_type``, ``file_length``, the ``upload``
  tags, ``size``) with its bytes in ``contents`` - the pack's bounded form of the engines' streaming
  upload, within ``rest.upload.max.bytes`` (16 MiB; 413 beyond it) - and the form's text fields
  become query parameters;
- ``stream: true`` renders an interceptor's segments as Server-Sent Events or chunked text on the
  pack's reply sink; a traced call leaves the edge's ``http.request`` record and the function's
  round-trip record, as the engines do;
- a path no entry claims falls through to an extension's routes and then to the static folder,
  under the ``static-content`` rules - the ``no-cache-pages`` headers and the ``filter`` function.

Off by default: with the switch off, the host is exactly what it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml
from aiohttp import BodyPartReader, web

from .actuator import app_origin
from .async_http_request import AsyncHttpRequest, UploadedFile
from .bus import DeliveryTimeout, EventBus, round_trip_record
from .config import AppConfig
from .envelope import EventEnvelope, iso_utc
from .event_stream import (
    DATA,
    EOF,
    EXCEPTION,
    X_EVENT_NAME,
    X_EVENT_STREAM,
    keep_alive_ms,
    sse_frame,
    stream_event_name,
    stream_signal,
)
from .exceptions import AppException
from .log import get_logger
from .registry import FunctionRegistry, ServiceDef
from .rest_routing import (
    AssignedRoute,
    ParsedBody,
    RouteInfo,
    RoutingTable,
    base_content_type,
    compact_json,
    error_body,
    error_payload,
    map_response,
    matched_element,
    negotiate_stream_type,
    parse_body,
    parse_cookies,
    parse_query,
    pretty_json,
    stream_text,
    substitute_values,
)
from .trace import MY_CID_TAG

if TYPE_CHECKING:
    from .server import EventApiServer

log = get_logger("mercury.rest")

REST_AUTOMATION = "rest.automation"
YAML_REST_AUTOMATION = "yaml.rest.automation"
DEFAULT_LOCATION = "classpath:/rest.yaml"
TRACE_ID_HEADER = "http.trace.id.header"
CORRELATION_ID_HEADER = "http.correlation.id.header"
TRACEPARENT_HEADER = "http.traceparent.header"
DEFAULT_TRACE_ID_HEADER = "X-Trace-Id"
DEFAULT_CORRELATION_ID_HEADER = "X-Correlation-Id"
TRACEPARENT = "traceparent"
X_TTL = "x-ttl"
# the bound on the files of one upload, all of them together (the pack's bounded form of the
# engines' streaming upload)
UPLOAD_MAX_BYTES = "rest.upload.max.bytes"
DEFAULT_UPLOAD_MAX_BYTES = 16 * 1024 * 1024
MULTIPART_FORM_DATA = "multipart/form-data"
# the edge's own service name in the trace records (the engines' REST edge)
HTTP_REQUEST = "http.request"
EVENT_API_SERVICE = "event.api.service"
# the engines' service names of the built-in endpoints, served by the host itself
ACTUATOR_SERVICES = ("info.actuator.service", "routes.actuator.service", "env.actuator.service",
                     "health.actuator.service", "liveness.actuator.service")
BUILT_IN_SERVICES = frozenset((EVENT_API_SERVICE, *ACTUATOR_SERVICES))
# the static-content filter's wait (the engines' FILTER_TIMEOUT)
FILTER_TIMEOUT_MS = 10_000
NO_CACHE_HEADERS = (("Cache-Control", "no-cache, no-store"), ("Pragma", "no-cache"),
                    ("Expires", "Thu, 01 Jan 1970 00:00:00 GMT"))
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def enabled(config: AppConfig) -> bool:
    """``rest.automation`` is on - a boolean or the text ``true``."""
    value = config.get(REST_AUTOMATION, False)
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def resolve_location(config: AppConfig, location: str) -> str | None:
    """The file ``yaml.rest.automation`` names: ``classpath:`` is the ``resources`` folder,
    ``file:`` an absolute path, a relative path is under the application file's folder; None when
    the application file is unknown (an interactive session)."""
    value = location.strip()
    if value.startswith("classpath:"):
        if config.resources_dir is None:
            return None
        return os.path.join(config.resources_dir, value[len("classpath:"):].lstrip("/"))
    value = value.removeprefix("file:")
    if os.path.isabs(value):
        return value
    if config.app_dir is None:
        return None
    return os.path.join(config.app_dir, value)


def load_table(config: AppConfig) -> RoutingTable:
    """The routing table: the file's entries (``${ENV:default}`` resolved in every value) plus the
    engines' default endpoints the file does not claim. A missing or empty file degrades to the
    defaults with a warning, as in the engines; an invalid entry fails the load."""
    location = config.get_property(YAML_REST_AUTOMATION, DEFAULT_LOCATION) or DEFAULT_LOCATION
    if "," in location:
        # the Java engine merges several locations; this pack, like the Rust port, reads one
        log.warning("%s names several locations - this pack reads the first", YAML_REST_AUTOMATION)
        location = location.split(",", 1)[0]
    path = resolve_location(config, location)
    table = RoutingTable()
    if path is None or not os.path.isfile(path):
        log.warning("REST automation configuration not found - %s (the default endpoints only)",
                    location)
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f.read())
        if data is None:
            log.warning("REST automation configuration is empty - %s (the default endpoints only)",
                        path)
        else:
            table = RoutingTable.load(substitute_values(data, config.resolve_text))
            log.info("Loading REST automation from %s", path)
    table.merge_defaults()
    return table


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """A well-formed W3C traceparent as ``(trace id, parent span)``."""
    match = TRACEPARENT_RE.match((value or "").strip().lower())
    return (match.group(1), match.group(2)) if match else None


def _error(status: int, message: str) -> web.Response:
    # the header as the engines send it, without aiohttp's charset suffix
    return web.Response(status=status, body=error_payload(status, message),
                        headers={"Content-Type": "application/json"})


def _stream_error_message(event: EventEnvelope) -> str:
    body = event.body
    if isinstance(body, dict):
        message = body.get("message")
        return stream_text(message) if message is not None else "Stream failed"
    if body is None:
        return "Stream failed"
    return stream_text(body)


def _chunk_bytes(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return (compact_json(body) + "\n").encode("utf-8")


def envelope_payload(reply: EventEnvelope) -> tuple[str | None, bytes]:
    """A reply's body as an HTTP payload with its own content type - the filter pass-through."""
    body = reply.body
    if body is None:
        return None, b""
    if isinstance(body, str):
        return "text/plain", body.encode("utf-8")
    if isinstance(body, (bytes, bytearray)):
        return "application/octet-stream", bytes(body)
    return "application/json", pretty_json(body).encode("utf-8")


class _RestResponse(web.Response):
    """A response that may carry no content type at all: the engines send none when the caller sent
    no ``Accept``, where aiohttp would add ``application/octet-stream``."""

    omit_content_type: bool = False

    async def _prepare_headers(self) -> None:
        await super()._prepare_headers()
        if self.omit_content_type:
            self._headers.popall("Content-Type", None)


class _RestEdge:
    """The trace records of one traced call through a REST entry - what the engines leave: the
    edge's ``http.request`` round trip (the SERVER span, parented onto the caller's span from the
    traceparent) and, for an RPC, the function's round-trip record parented onto the edge."""

    def __init__(self, bus: EventBus, trace_id: str, trace_path: str, caller_span: str | None) -> None:
        self.bus = bus
        self.trace_id = trace_id
        self.trace_path = trace_path
        self.caller_span = caller_span
        self.edge_span = secrets.token_hex(8)
        self.start = iso_utc()
        self.started = time.perf_counter()

    def record_function(self, service: str, reply: EventEnvelope, dispatched: str,
                        started: float) -> None:
        self.bus.emit(round_trip_record(service=service, sender=HTTP_REQUEST, trace_id=self.trace_id,
                                        trace_path=self.trace_path, reply=reply,
                                        parent_span_id=self.edge_span, start=dispatched,
                                        round_trip_ms=(time.perf_counter() - started) * 1000),
                      reply.annotations)

    def record(self, status: int, error: str | None = None) -> None:
        trace: dict[str, Any] = {
            "origin": app_origin(), "id": self.trace_id, "path": self.trace_path,
            "service": HTTP_REQUEST, "start": self.start,
            "exec_time": round((time.perf_counter() - self.started) * 1000, 3),
            "status": status, "success": status < 400, "span_id": self.edge_span,
        }
        if self.caller_span:
            trace["parent_span_id"] = self.caller_span
        if status >= 400 and error:
            trace["exception"] = error
        self.bus.emit(trace)


@dataclass
class _Prepared:
    """One request, prepared for its entry: the request model, the resolved ids and the context
    the response needs."""

    http_request: AsyncHttpRequest
    cid: str
    cid_header: str
    trace_id: str | None
    trace_path: str
    accept: str | None
    edge: _RestEdge | None
    is_head: bool


class RestAutomation:
    """The REST automation edge of one host."""

    def __init__(self, table: RoutingTable, config: AppConfig, server: EventApiServer) -> None:
        self.table = table
        self.server = server
        self.registry: FunctionRegistry = server.registry
        self.bus: EventBus = server.registry.bus
        self.trace_header = (config.get_property(TRACE_ID_HEADER, DEFAULT_TRACE_ID_HEADER)
                             or DEFAULT_TRACE_ID_HEADER).lower()
        self.cid_header = (config.get_property(CORRELATION_ID_HEADER, DEFAULT_CORRELATION_ID_HEADER)
                           or DEFAULT_CORRELATION_ID_HEADER).lower()
        self.traceparent_header = (config.get_property(TRACEPARENT_HEADER, TRACEPARENT)
                                   or TRACEPARENT).lower()
        try:
            self.upload_max_bytes = int(config.get(UPLOAD_MAX_BYTES, DEFAULT_UPLOAD_MAX_BYTES))
        except (TypeError, ValueError):
            self.upload_max_bytes = DEFAULT_UPLOAD_MAX_BYTES
        self._fallback: list[web.AbstractRoute] = []
        self._started = False

    @classmethod
    def load(cls, config: AppConfig, server: EventApiServer) -> RestAutomation | None:
        """The edge when ``rest.automation`` is on, else None."""
        if not enabled(config):
            return None
        return cls(load_table(config), config, server)

    def attach(self, app: web.Application, *fallback: web.AbstractRoute) -> None:
        """Put the edge in front of the host's routes; ``fallback`` names the routes that mean
        nothing specific claimed the path (the landing page and the catch-all), where a static file
        is served under the ``static-content`` rules."""
        self._fallback = list(fallback)
        app.middlewares.append(self.middleware)

    async def startup(self) -> None:
        """Drop the entries whose service is not registered (the engines' load-time rule, run at
        the end of the boot so an extension's startup registrations count) and announce the table."""
        if self._started:
            return
        self._started = True
        skipped = self.table.retain_available(
            lambda service: service in BUILT_IN_SERVICES or self.registry.exists(service))
        for methods, url, service in skipped:
            log.warning("Skip %s %s - Service %s not available", methods, url, service)
        for route in self.table.routes:
            log.info("REST [%s] %s -> %s", ", ".join(route.methods), route.url, route.service)
        static_filter = self.table.static_content.filter
        if static_filter is not None and not self.registry.exists(static_filter.service):
            log.warning("Static content filter %s ignored because it does not exist",
                        static_filter.service)

    # ---- the edge ----

    @web.middleware
    async def middleware(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        method = request.method.upper()
        path = request.path
        assigned = self.table.find(method, path)
        if assigned is None and path == "/":
            # the engines: a request for exactly "/" falls back to a declared /index.html entry
            assigned = self.table.find(method, "/index.html")
        if assigned is None:
            if self.table.path_matches_any_method(path):
                return _error(405, "Method not allowed")
            if request.match_info.route in self._fallback:
                if method in ("GET", "HEAD"):
                    served = await self._static(request, path)
                    if served is not None:
                        return served
                if path != "/":
                    # nothing claims the path: the engines' 404, in their header shape
                    return _error(404, "Resource not found")
            return await handler(request)
        if method == "OPTIONS":
            return self._preflight(assigned.info)
        if assigned.info.service in BUILT_IN_SERVICES:
            return await self._builtin(request, assigned)
        return await self._process(request, assigned)

    @staticmethod
    def _preflight(info: RouteInfo) -> web.Response:
        """The CORS preflight: the ``options`` lines with 204; no CORS block means 405."""
        if info.cors is None or not info.cors.options:
            return _error(405, "Method not allowed")
        response = web.Response(status=204)
        for name, value in info.cors.options:
            response.headers[name] = value
        return response

    @staticmethod
    def _request_headers(request: web.Request) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in request.headers.items():
            headers[name.lower()] = value
        return headers

    async def _prepare(self, request: web.Request, assigned: AssignedRoute) -> _Prepared:
        """The engines' ingress: the request transform, the trace and correlation ids, the
        ``x-ttl`` stamp, and the request model built from the method, path, query, cookies and the
        body parsed by its content type."""
        info = assigned.info
        method = request.method.upper()
        path = request.path
        headers = self._request_headers(request)
        if info.headers is not None:
            headers = info.headers.request.apply(headers)
        trace_header = (info.trace_id_header or self.trace_header).lower()
        cid_header = (info.correlation_id_header or self.cid_header).lower()
        # a well-formed standard traceparent wins; the custom name is read only when it is absent
        traceparent = parse_traceparent(headers.get(TRACEPARENT))
        if traceparent is None:
            custom = (info.traceparent_header or self.traceparent_header).lower()
            if custom != TRACEPARENT:
                traceparent = parse_traceparent(headers.get(custom))
        if traceparent is not None:
            trace_id: str | None = traceparent[0]
            parent_span: str | None = traceparent[1]
        else:
            trace_id, parent_span = headers.get(trace_header), None
        trace_id = (trace_id or uuid.uuid4().hex) if info.tracing else None
        # a business correlation id is always ensured; one header name serving both roles
        # yields one id, not two
        cid = headers.get(cid_header)
        if not cid:
            cid = trace_id if (cid_header == trace_header and trace_id) else uuid.uuid4().hex
        headers[cid_header] = cid
        headers.setdefault(X_TTL, str(info.timeout_seconds * 1000))
        query_text = request.query_string
        query = parse_query(query_text)
        cookies = parse_cookies(headers.pop("cookie", None))
        accept = headers.get("accept")
        files: list[UploadedFile] = []
        declared = (base_content_type(headers.get("content-type")) or "").lower()
        if info.upload and method == "POST" and declared == MULTIPART_FORM_DATA:
            # the engines' upload, bounded: the files' bytes ride the request map, a form's text
            # fields become query parameters, the body stays absent
            files, fields = await self._read_multipart(request)
            for name, value in fields.items():
                query[name] = value
            parsed = ParsedBody("upload", None)
        else:
            parsed = parse_body(headers.get("content-type"), await request.read())
            if parsed.kind == "form":
                for name, value in parsed.value.items():
                    query[name] = value
        http_request = (AsyncHttpRequest().set_method(method).set_url(path)
                        .set_remote_ip(request.remote or "unknown")
                        .set_secure(headers.get("x-forwarded-proto") == "https")
                        .set_target_host(headers.get("host", ""))
                        .set_timeout_seconds(info.timeout_seconds))
        for name, value in headers.items():
            http_request.set_header(name, value)
        for name, value in assigned.path_params.items():
            http_request.set_path_parameter(name, value)
        for name, value in query.items():
            http_request.set_query_parameter(name, value)
        if parsed.kind == "upload":
            for uploaded in files:
                http_request.add_file(uploaded.name, uploaded.content_type, uploaded.content,
                                      uploaded.tag)
        else:
            http_request.set_body(None if parsed.kind == "form" else parsed.value)
        if query_text:
            http_request.set_query_string(query_text)
        for name, value in cookies.items():
            http_request.set_cookie(name, value)
        trace_path = f"{method} {path}?{query_text}" if query_text else f"{method} {path}"
        edge = _RestEdge(self.bus, trace_id, trace_path, parent_span) if trace_id else None
        return _Prepared(http_request, cid, cid_header, trace_id, trace_path, accept, edge,
                         method == "HEAD")

    async def _read_multipart(self, request: web.Request) -> tuple[list[UploadedFile], dict[str, str]]:
        """The parts of a multipart body: a part with a filename is a file (its field name the
        upload tag, its declared type or octet-stream), any other part a text field. The files
        together may not exceed ``rest.upload.max.bytes`` - a 413 the moment they do."""
        files: list[UploadedFile] = []
        fields: dict[str, str] = {}
        total = 0
        try:
            reader = await request.multipart()
            while True:
                # reader.next() rather than an async for: the iteration protocol is not typed on
                # every Python the pack supports
                part = await reader.next()
                if part is None:
                    break
                if not isinstance(part, BodyPartReader):
                    continue
                if part.filename:
                    chunks: list[bytes] = []
                    while True:
                        chunk = await part.read_chunk(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.upload_max_bytes:
                            raise AppException(413, f"Upload exceeds {self.upload_max_bytes} bytes")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    content_type = part.headers.get("Content-Type", "application/octet-stream")
                    files.append(UploadedFile(part.filename, content_type, len(content),
                                              part.name or "file", content))
                else:
                    text = await part.text()
                    total += len(text)
                    if total > self.upload_max_bytes:
                        raise AppException(413, f"Upload exceeds {self.upload_max_bytes} bytes")
                    fields[part.name or ""] = text
        except AppException:
            raise
        except (ValueError, LookupError, AssertionError) as e:
            raise AppException(400, f"Invalid multipart body - {e}") from None
        return files, fields

    @staticmethod
    def _event(to: str, prepared: _Prepared) -> EventEnvelope:
        """The event a function (or the authentication function) receives: the request map as the
        body, the edge as the sender, the correlation id as the engine-managed tag, and under a
        trace the edge's span as the parent."""
        event = EventEnvelope(to=to, body=prepared.http_request.to_map())
        event.set_from(HTTP_REQUEST)
        event.set_correlation_id(prepared.cid)
        event.tags[MY_CID_TAG] = prepared.cid
        if prepared.trace_id:
            event.set_trace(prepared.trace_id, prepared.trace_path)
            if prepared.edge is not None:
                event.set_span_id(prepared.edge.edge_span)
        return event

    async def _rpc(self, route: str, service: ServiceDef, prepared: _Prepared,
                   timeout_ms: int) -> EventEnvelope:
        """One RPC to a function through the bus, recorded on the edge under a trace."""
        event = self._event(route, prepared)
        dispatched, started = iso_utc(), time.perf_counter()
        try:
            reply = await self.bus.deliver(service, {}, event.body, timeout_ms,
                                           trace_id=prepared.trace_id,
                                           trace_path=prepared.trace_path,
                                           cid=prepared.cid, envelope=event)
        except DeliveryTimeout:
            message = f"Timeout for {timeout_ms} ms"
            if prepared.edge is not None:
                timeout = EventEnvelope().set_status(408).set_body(message)
                prepared.edge.record_function(route, timeout, dispatched, started)
            raise AppException(408, message) from None
        if prepared.edge is not None:
            prepared.edge.record_function(route, reply, dispatched, started)
        return reply

    async def _authenticate(self, info: RouteInfo, prepared: _Prepared) -> None:
        """The optional authentication function: ``true`` lets the request through, anything else
        is 401, an error is its own status; the verdict's headers become the session map."""
        route = info.authentication or ""
        service = self.registry.get(route)
        if service is None:
            raise AppException(503, f"Service {route} not reachable")
        verdict = await self._rpc(route, service, prepared, info.timeout_ms)
        if verdict.has_error():
            message = verdict.body if isinstance(verdict.body, str) else "Unauthorized"
            raise AppException(verdict.get_status(), message)
        if verdict.body is not True:
            raise AppException(401, "Unauthorized")
        for name, value in verdict.headers.items():
            prepared.http_request.set_session_info(name, value)

    async def _first_reply(self, service: ServiceDef, event: EventEnvelope,
                           timeout_ms: int) -> tuple[EventEnvelope, str | None, str]:
        """Send to an interceptor with a reply sink and take its first valid envelope; the sink
        route is returned for the caller to close."""
        sink_route, queue = self.bus.open_sink()
        event.set_reply_to(sink_route)
        self.bus.publish_envelope(service, event)
        while True:
            try:
                first = await asyncio.wait_for(queue.get(), timeout_ms / 1000)
            except TimeoutError:
                self.bus.close_sink(sink_route)
                raise AppException(408, f"Timeout for {timeout_ms} ms") from None
            marker = stream_signal(first)
            if marker is None or marker in (DATA, EOF, EXCEPTION):
                return first, marker, sink_route
            log.warning("Dropping event - invalid %s signal", X_EVENT_STREAM)

    async def _process(self, request: web.Request, assigned: AssignedRoute) -> web.StreamResponse:
        info = assigned.info
        try:
            prepared = await self._prepare(request, assigned)
        except AppException as e:
            return _error(e.status, e.message)
        edge = prepared.edge
        try:
            if info.authentication:
                await self._authenticate(info, prepared)
            service = self.registry.get(info.service)
            if service is None:
                raise AppException(503, f"Service {info.service} not reachable")
            if service.interceptor:
                event = self._event(info.service, prepared)
                first, marker, sink_route = await self._first_reply(service, event, info.timeout_ms)
                if marker is not None and info.stream and not prepared.is_head:
                    # the multi-shot reply: the first segment commits the head, the renderer
                    # owns the round-trip record from here
                    return await self._stream(request, info, prepared, first, marker, sink_route)
                self.bus.close_sink(sink_route)
                if marker == EXCEPTION:
                    status = first.get_status() if first.has_error() else 500
                    raise AppException(status, _stream_error_message(first))
                # a single-shot reply (a marked segment taken as the whole reply - the first
                # event completes the exchange, the engines' rule)
                first.headers = {k: v for k, v in first.headers.items()
                                 if k.lower() not in (X_EVENT_STREAM, X_EVENT_NAME)}
                reply = first
            else:
                reply = await self._rpc(info.service, service, prepared, info.timeout_ms)
        except AppException as e:
            if edge is not None:
                edge.record(e.status, e.message)
            return _error(e.status, e.message)
        mapped = map_response(status=reply.get_status(), reply_headers=reply.headers,
                              body=reply.body, accept=prepared.accept, is_head=prepared.is_head,
                              transform=info.headers.response if info.headers else None,
                              cors=info.cors, cid=prepared.cid, cid_header=prepared.cid_header)
        response = _RestResponse(status=mapped.status, body=mapped.body)
        response.headers.popall("Content-Type", None)
        for name, value in mapped.headers:
            response.headers.add(name, value)
        response.omit_content_type = not any(name == "content-type" for name, _ in mapped.headers)
        if edge is not None:
            edge.record(mapped.status, None)
        return response

    async def _stream(self, request: web.Request, info: RouteInfo, prepared: _Prepared,
                      first: EventEnvelope, marker: str, sink_route: str) -> web.StreamResponse:
        """Render an interceptor's segments progressively: Server-Sent Events when the head or the
        negotiation says ``text/event-stream``, chunked text or JSON lines otherwise; the stream
        ends at ``eof``, fails in band at an ``exception`` or when the producer idles past the
        allowance (the entry's timeout, or the head's ``x-ttl`` in seconds)."""
        queue = self.bus._sinks.get(sink_route)
        headers: dict[str, str] = {}
        cookies: list[str] = []
        content_type: str | None = None
        idle_ms = info.timeout_ms
        for name, value in first.headers.items():
            key = name.lower()
            if key in (X_EVENT_STREAM, X_EVENT_NAME, "x-stream-id"):
                continue
            if key == X_TTL:
                with contextlib.suppress(ValueError):
                    seconds = int(value.strip())
                    if seconds > 0:
                        idle_ms = seconds * 1000
            elif key == "content-type":
                content_type = value.lower()
            elif key == "set-cookie":
                cookies.extend(c.strip() for c in value.split("|"))
            else:
                headers[key] = value
        if info.headers is not None:
            headers = info.headers.response.apply(headers)
        if not any(name.lower() == prepared.cid_header for name in headers):
            headers[prepared.cid_header] = prepared.cid
        if info.cors is not None:
            for name, value in info.cors.headers:
                headers[name.lower()] = value
        content_type = content_type or negotiate_stream_type(prepared.accept)
        sse = content_type.startswith("text/event-stream")
        if sse:
            headers.setdefault("cache-control", "no-cache")
        response = web.StreamResponse(status=first.get_status())
        for name, value in headers.items():
            response.headers[name] = value
        for cookie in cookies:
            if cookie:
                response.headers.add("Set-Cookie", cookie)
        response.headers["content-type"] = content_type
        status, error = first.get_status(), None
        try:
            await response.prepare(request)
            if queue is not None:
                status, error = await self._render(response, queue, first, marker, sse, idle_ms)
        except ConnectionError:
            log.debug("Client disconnected from event stream")
        finally:
            self.bus.close_sink(sink_route)
        with contextlib.suppress(ConnectionError):
            await response.write_eof()
        if prepared.edge is not None:
            prepared.edge.record(status, error)
        return response

    async def _render(self, response: web.StreamResponse, queue: asyncio.Queue[EventEnvelope],
                      first: EventEnvelope, first_marker: str, sse: bool,
                      idle_ms: int) -> tuple[int, str | None]:
        """The frames until the terminal; returns the stream's outcome for the edge's record."""
        pending: tuple[EventEnvelope, str] | None = (first, first_marker)
        head_status = first.get_status()
        ping_ms = keep_alive_ms() if sse else 0
        while True:
            if pending is not None:
                event, marker = pending
                pending = None
            else:
                waited = await self._next(response, queue, idle_ms, ping_ms)
                if waited is None:
                    seconds = idle_ms // 1000
                    message = f"Timeout for {seconds} seconds"
                    if sse:
                        await response.write(sse_frame("error", compact_json(error_body(408, message))))
                    return 408, message
                found = stream_signal(waited)
                if found is None:
                    log.warning("Single-shot reply dropped - its streaming response has already started")
                    continue
                if found not in (DATA, EOF, EXCEPTION):
                    log.warning("Dropping event - invalid %s signal", X_EVENT_STREAM)
                    continue
                event, marker = waited, found
            if marker == DATA:
                if sse:
                    if event.body is not None:
                        await response.write(sse_frame(stream_event_name(event), stream_text(event.body)))
                else:
                    chunk = _chunk_bytes(event.body)
                    if chunk:
                        await response.write(chunk)
            elif marker == EOF:
                if sse:
                    text = stream_text(event.body) if event.body is not None else "{}"
                    await response.write(sse_frame("done", text))
                return head_status, None
            else:
                status = event.get_status() if event.has_error() else 500
                message = _stream_error_message(event)
                if sse:
                    await response.write(sse_frame("error", compact_json(error_body(status, message))))
                return status, message

    @staticmethod
    async def _next(response: web.StreamResponse, queue: asyncio.Queue[EventEnvelope],
                    idle_ms: int, ping_ms: int) -> EventEnvelope | None:
        """The next segment within the idle allowance, with SSE keep-alive comments while the
        producer is quiet; None at expiry."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + idle_ms / 1000
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            wait = min(remaining, ping_ms / 1000) if ping_ms > 0 else remaining
            try:
                return await asyncio.wait_for(queue.get(), wait)
            except TimeoutError:
                if loop.time() >= deadline:
                    return None
                with contextlib.suppress(ConnectionError):
                    await response.write(b": ping\n\n")

    # ---- the built-in endpoints and static content ----

    async def _builtin(self, request: web.Request, assigned: AssignedRoute) -> web.StreamResponse:
        """A default entry, or a user entry naming a built-in service: the host's own handler,
        behind the entry's authentication, within its timeout (a caller's ``x-ttl`` wins), with
        the entry's CORS headers and added response headers."""
        info = assigned.info
        try:
            prepared = await self._prepare(request, assigned)
        except AppException as e:
            return _error(e.status, e.message)
        if info.authentication:
            try:
                await self._authenticate(info, prepared)
            except AppException as e:
                return _error(e.status, e.message)
        ttl_ms = info.timeout_ms
        header = request.headers.get(X_TTL)
        if header is not None:
            with contextlib.suppress(ValueError):
                ttl_ms = max(1000, int(header.strip()))
        try:
            response = await asyncio.wait_for(self._builtin_call(request, info.service), ttl_ms / 1000)
        except TimeoutError:
            return _error(408, f"Timeout for {ttl_ms} ms")
        if not response.prepared:
            if info.cors is not None:
                for name, value in info.cors.headers:
                    response.headers[name] = value
            if info.headers is not None:
                for name, value in info.headers.response.add:
                    response.headers[name] = value
        return response

    async def _builtin_call(self, request: web.Request, service: str) -> web.StreamResponse:
        if service == EVENT_API_SERVICE:
            return await self.server.handle_event(request)
        response = await self.server.actuator.by_service(service)
        return response if response is not None else _error(404, "Resource not found")

    async def _static(self, request: web.Request, path: str) -> web.StreamResponse | None:
        """A static file under the ``static-content`` rules: the filter function first (its headers
        ride the response; a status other than 200 passes its reply through), then the file with
        the no-cache headers for an entry page, else the host's ETag service."""
        static = self.server.static
        if not static.enabled:
            return None
        file = static.locate(path)
        if file is None:
            return None
        config = self.table.static_content
        filter_headers: list[tuple[str, str]] = []
        static_filter = config.filter
        if static_filter is not None and matched_element(static_filter.path_list, path) \
                and not matched_element(static_filter.exclusion_list, path):
            service = self.registry.get(static_filter.service)
            if service is None:
                log.warning("Static content filter %s ignored because it does not exist",
                            static_filter.service)
            else:
                try:
                    filtered = await self._run_filter(request, service, path)
                    filter_headers = list(filtered.headers.items())
                    if filtered.get_status() != 200:
                        kind, payload = envelope_payload(filtered)
                        response = web.Response(status=filtered.get_status(), body=payload)
                        response.headers.popall("Content-Type", None)
                        for name, value in filter_headers:
                            response.headers[name] = value
                        if kind is not None and "Content-Type" not in response.headers:
                            response.headers["Content-Type"] = kind
                        return response
                except (AppException, DeliveryTimeout) as e:
                    # the Rust port's resilient divergence: log and serve the file anyway
                    log.error("Unable to filter static content HTTP-GET %s - %s",
                              static_filter.service, e)
        response = static.response(file)
        for name, value in filter_headers:
            response.headers[name] = value
        if matched_element(config.no_cache_pages, path):
            for name, value in NO_CACHE_HEADERS:
                response.headers[name] = value
        return response

    async def _run_filter(self, request: web.Request, service: ServiceDef,
                          path: str) -> EventEnvelope:
        """The filter receives an ``AsyncHttpRequest``-shaped event without a body."""
        headers = self._request_headers(request)
        http_request = (AsyncHttpRequest().set_method("GET").set_url(path)
                        .set_remote_ip(request.remote or "unknown").set_secure(False)
                        .set_target_host(headers.get("host", "")).set_body(None))
        for name, value in headers.items():
            http_request.set_header(name, value)
        for name, value in parse_query(request.query_string).items():
            http_request.set_query_parameter(name, value)
        event = EventEnvelope(to=service.route, body=http_request.to_map())
        return await self.bus.deliver(service, {}, event.body, FILTER_TIMEOUT_MS, envelope=event)
