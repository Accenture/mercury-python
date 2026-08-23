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
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import traceback

from aiohttp import web

from .config import app_config
from .envelope import EventEnvelope, iso_utc
from .exceptions import AppException, CompactFormatError
from .log import get_logger
from .registry import FunctionRegistry, ServiceDef, default_registry
from .trace import TraceInfo, _reset_trace, _set_trace

OCTET_STREAM = "application/octet-stream"
X_TTL = "x-ttl"
X_ASYNC = "x-async"
X_EVENT_API = "x-event-api"
MY_CID_TAG = "my_cid"
MY_CORRELATION_ID = "my_correlation_id"

log = get_logger("mercury.server")


def _transport_error(status: int, message: str) -> web.Response:
    reply = EventEnvelope().set_status(status).set_body(message)
    return web.Response(status=status, body=reply.to_bytes(), content_type=OCTET_STREAM)


def _handler_headers(event: EventEnvelope) -> dict[str, str]:
    headers = {k: v for k, v in event.headers.items()
               if k.lower() != X_EVENT_API and not k.lower().startswith("my_")}
    my_cid = event.tags.get(MY_CID_TAG)
    if my_cid:
        headers[MY_CORRELATION_ID] = my_cid
    return headers


class EventApiServer:
    def __init__(self, registry: FunctionRegistry | None = None):
        self.registry = registry or default_registry
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _semaphore(self, service: ServiceDef) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(service.route)
        if semaphore is None:
            semaphore = asyncio.Semaphore(service.instances)
            self._semaphores[service.route] = semaphore
        return semaphore

    async def _invoke(self, service: ServiceDef, event: EventEnvelope,
                      headers: dict[str, str]) -> EventEnvelope:
        """Run the handler under its trace context and shape the outcome as a reply."""
        info = TraceInfo(trace_id=event.trace_id, trace_path=event.trace_path, cid=event.cid)
        token = _set_trace(info)
        start = time.perf_counter()
        try:
            async with self._semaphore(service):
                if service.is_async:
                    result = await service.handler(headers, event.body)
                else:
                    # copy_context() carries the trace contextvar into the executor thread
                    context = contextvars.copy_context()
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: context.run(service.handler, headers, event.body))
            reply = result if isinstance(result, EventEnvelope) else EventEnvelope(body=result)
        except AppException as e:
            reply = EventEnvelope().set_status(e.status).set_body(e.message)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the host converts ANY handler failure
            # into the portable error contract (envelope status 500 + message + stack),
            # mirroring the engines; letting it propagate would drop the reply
            reply = EventEnvelope().set_status(500).set_body(str(e))
            reply.stack = traceback.format_exc(limit=20)
        finally:
            _reset_trace(token)
        reply.sender = reply.sender or service.route
        reply.exec_time = round((time.perf_counter() - start) * 1000, 3)
        if info.annotations:
            reply.annotations.update(info.annotations)
        return reply

    async def handle_event(self, request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            ttl = int(request.headers.get(X_TTL, "0") or 0)
        except ValueError:
            ttl = 0
        ttl = max(1000, ttl)
        is_async = request.headers.get(X_ASYNC, "") == "true"
        try:
            event = EventEnvelope.from_bytes(raw)
        except (CompactFormatError, ValueError) as e:
            return _transport_error(400, str(e))
        if not event.to:
            return _transport_error(400, "Missing routing path")
        service = self.registry.get(event.to)
        if service is None:
            return _transport_error(404, f"Route {event.to} not found")
        if service.private:
            return _transport_error(403, f"{event.to} is private")
        headers = _handler_headers(event)
        if is_async:
            task = asyncio.get_running_loop().create_task(
                self._invoke(service, event, headers))
            task.add_done_callback(self._log_async_outcome(event.to))
            ack = EventEnvelope().set_status(202).set_body(
                {"type": "async", "delivered": True, "time": iso_utc()})
            return web.Response(status=202, body=ack.to_bytes(), content_type=OCTET_STREAM)
        try:
            reply = await asyncio.wait_for(
                self._invoke(service, event, headers), timeout=ttl / 1000)
        except asyncio.TimeoutError:
            log.warning("Event %s timeout for %d ms (trace_id=%s)", event.to, ttl, event.trace_id)
            return _transport_error(408, f"Timeout for {ttl} ms")
        log.info("Handled %s status=%d exec_time=%sms trace_id=%s",
                 event.to, reply.get_status(), reply.exec_time, event.trace_id)
        return web.Response(status=200, body=reply.to_bytes(), content_type=OCTET_STREAM)

    def _log_async_outcome(self, route: str):
        def callback(task: asyncio.Task[EventEnvelope]) -> None:
            try:
                reply = task.result()
                if reply.has_error():
                    log.warning("Async event %s ended with status %d - %s",
                                route, reply.get_status(), reply.body)
            except Exception as e:  # noqa: BLE001 - log-only sink: a drop-n-forget
                # event has no requester to answer, so any failure is logged, never raised
                log.error("Async event %s failed - %s", route, e)
        return callback

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.Response(text="OK")

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_post("/api/event", self.handle_event)
        app.router.add_get("/health", self.handle_health)
        return app


class Platform:
    """Runs the Event API host for the default (or a given) registry."""

    def __init__(self, registry: FunctionRegistry | None = None):
        self.registry = registry or default_registry

    def run(self, port: int | None = None, host: str = "127.0.0.1") -> None:
        config = app_config()
        app_name = config.get_property("application.name", "application")
        actual_port = int(port if port is not None else config.get("rest.server.port", 8085))
        server = EventApiServer(self.registry)
        for route, service in sorted(self.registry.routes().items()):
            visibility = "PRIVATE" if service.private else "PUBLIC"
            log.info("Loaded %s %s, instances=%d", visibility, route, service.instances)
        log.info("%s - Event API service started on port %d", app_name, actual_port)
        web.run_app(server.create_app(), host=host, port=actual_port,
                    print=None, handle_signals=True)


platform = Platform()
