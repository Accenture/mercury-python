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
"""

from __future__ import annotations

from aiohttp import web

from .actuator import Actuator
from .bus import DeliveryTimeout
from .config import app_config
from .envelope import EventEnvelope
from .log import get_logger
from .registry import FunctionRegistry, default_registry

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
    """Thin ingress: protocol guards + header hygiene, then the registry's bus."""

    def __init__(self, registry: FunctionRegistry | None = None):
        self.registry = registry or default_registry
        self.actuator = Actuator(self.registry)

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
        # CompactFormatError is a ValueError - one catch covers the codec errors
        except ValueError as e:
            return _transport_error(400, str(e))
        if not event.to:
            return _transport_error(400, "Missing routing path")
        service = self.registry.get(event.to)
        if service is None:
            return _transport_error(404, f"Route {event.to} not found")
        if service.private:
            return _transport_error(403, f"{event.to} is private")
        headers = _handler_headers(event)
        bus = self.registry.bus
        if is_async:
            ack = bus.publish(service, headers, event.body, trace_id=event.trace_id,
                              trace_path=event.trace_path, cid=event.cid)
            return web.Response(status=202, body=ack.to_bytes(), content_type=OCTET_STREAM)
        try:
            reply = await bus.deliver(service, headers, event.body, ttl,
                                      trace_id=event.trace_id, trace_path=event.trace_path,
                                      cid=event.cid)
        except DeliveryTimeout:
            log.warning("Event %s timeout for %d ms (trace_id=%s)", event.to, ttl, event.trace_id)
            return _transport_error(408, f"Timeout for {ttl} ms")
        log.info("Handled %s status=%d exec_time=%sms trace_id=%s",
                 event.to, reply.get_status(), reply.exec_time, event.trace_id)
        return web.Response(status=200, body=reply.to_bytes(), content_type=OCTET_STREAM)

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_post("/api/event", self.handle_event)
        app.router.add_get("/info", self.actuator.handle_info)
        app.router.add_get("/info/routes", self.actuator.handle_routes)
        app.router.add_get("/env", self.actuator.handle_env)
        app.router.add_get("/health", self.actuator.handle_health)
        app.router.add_get("/livenessprobe", self.actuator.handle_livenessprobe)
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
