"""
Actuator endpoints for operations and Kubernetes deployment.

The same operational surface as the engines (the Java ``ActuatorServices``
and its Rust port), so a polyglot installation monitors every app one way:

- ``GET /info`` - application identity (name, version, description), runtime,
  origin id, start/current time and uptime.
- ``GET /info/routes`` - the local routing table split by visibility
  (``routing.public`` / ``routing.private``, route -> instance count).
- ``GET /env`` - selected environment variables (``show.env.variables``) and
  selected configuration parameters (``show.application.properties``) -
  opt-in lists, so secrets are never dumped wholesale (engine parity).
- ``GET /health`` - runs the health-check functions listed in
  ``mandatory.health.dependencies`` / ``optional.health.dependencies``.
  All mandatory up -> ``UP`` (HTTP 200); any mandatory down -> ``DOWN``
  (HTTP 400, engine parity). The outcome feeds the liveness state.
- ``GET /livenessprobe`` - ``OK`` (text) while the last health outcome is
  good, else HTTP 400 ``Unhealthy. Please check '/health' endpoint.``

A health-check function is a normal registered function (usually private)
speaking the engines' interface contract - called through the same event bus
that serves PostOffice, first with header ``type=info`` (an advisory identity
map merged into its dependency entry), then with ``type=health`` (a status
text or map; a non-200 reply marks the dependency down)::

    @preload("demo.health", private=True)
    async def health(headers: dict[str, str], _body: Body) -> Body:
        if headers.get("type") == "info":
            return {"service": "demo.service", "href": "http://127.0.0.1"}
        return "demo.service is running fine"

Engine deltas (deliberate, wrapper-scale): no ``/info/lib`` (a wrapper app
has no runtime dependency manifest - deferred on the Rust port too), no XML
responses, and no 5-second info cache (dependencies are in-process
functions, so the ``type=info`` lookup costs nothing).
"""

from __future__ import annotations

import contextlib
import os
import platform as runtime_platform
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from .bus import DeliveryTimeout
from .config import app_config
from .envelope import iso_utc
from .log import get_logger
from .registry import FunctionRegistry
from .version import __version__

log = get_logger("mercury.actuator")

INFO_TIMEOUT_MS = 3000  # engine value for the advisory type=info lookup
HEALTH_TIMEOUT_MS = 10000  # engine value for the type=health probe
UNHEALTHY = "Unhealthy. Please check '/health' endpoint."

_SPLIT = re.compile(r"[,\s]+")

_origin: str | None = None


def app_origin() -> str:
    """Unique instance id, minted once per process (the Java reference
    engine's format: UTC yyyyMMdd date prefix + 32-hex uuid)."""
    global _origin
    if _origin is None:
        _origin = time.strftime("%Y%m%d", time.gmtime()) + uuid.uuid4().hex
    return _origin


def elapsed_time(milliseconds: float) -> str:
    """Human-readable duration matching the engines' rendering
    (including their strict boundary behavior, kept verbatim for parity)."""
    one_second = 1000
    one_minute = 60 * one_second
    one_hour = 60 * one_minute
    one_day = 24 * one_hour
    remaining = int(milliseconds)
    parts: list[str] = []
    if remaining > one_day:
        days = remaining // one_day
        parts.append(f"{days} day" if days == 1 else f"{days} days")
        remaining -= days * one_day
    if remaining > one_hour:
        hours = remaining // one_hour
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
        remaining -= hours * one_hour
    if remaining > one_minute:
        minutes = remaining // one_minute
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
        remaining -= minutes * one_minute
    if remaining >= one_second:
        seconds = remaining // one_second
        parts.append(f"{seconds} second" if seconds == 1 else f"{seconds} seconds")
    return " ".join(parts) if parts else f"{remaining} ms"


def _as_list(value: Any) -> list[str]:
    """A comma/space-separated string (engine syntax) or a YAML list."""
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = _SPLIT.split(str(value or ""))
    return [item for item in items if item]


class Actuator:
    """HTTP handlers for the actuator endpoints (wired by EventApiServer)."""

    def __init__(self, registry: FunctionRegistry):
        config = app_config()
        self.registry = registry
        self.start = datetime.now(timezone.utc)
        self.healthy = True  # liveness follows the most recent /health outcome
        self.app_name = config.get_property("application.name", "application") or "application"
        self.app_version = config.get_property("info.app.version", __version__) or __version__
        self.description = (config.get_property("info.app.description", self.app_name)
                            or self.app_name)
        self.required = _as_list(config.get("mandatory.health.dependencies"))
        self.optional = _as_list(config.get("optional.health.dependencies"))
        if self.required:
            log.info("Mandatory service dependencies - %s", self.required)
        if self.optional:
            log.info("Optional services dependencies - %s", self.optional)

    def _app_block(self) -> dict[str, Any]:
        return {"name": self.app_name, "version": self.app_version,
                "description": self.description}

    # aiohttp handlers must be coroutines - async is the framework contract
    # even when a handler has nothing to await
    async def handle_info(self, _request: web.Request) -> web.Response:
        now = datetime.now(timezone.utc)
        return web.json_response({
            "app": self._app_block(),
            "runtime": {
                "language": "python",
                "python": runtime_platform.python_version(),
                "mercury_composable": __version__,
            },
            "origin": app_origin(),
            "time": {"start": iso_utc(self.start), "current": iso_utc(now)},
            "up_time": elapsed_time((now - self.start).total_seconds() * 1000),
        })

    async def handle_routes(self, _request: web.Request) -> web.Response:
        public: dict[str, int] = {}
        private: dict[str, int] = {}
        for route, service in sorted(self.registry.routes().items()):
            target = private if service.private else public
            target[route] = service.instances
        return web.json_response({
            "app": self._app_block(),
            "routing": {"public": public, "private": private},
        })

    async def handle_env(self, _request: web.Request) -> web.Response:
        config = app_config()
        environment = {name: os.environ.get(name, "")
                       for name in _as_list(config.get("show.env.variables"))}
        properties = {name: config.get_property(name) or ""
                      for name in _as_list(config.get("show.application.properties"))}
        return web.json_response({
            "app": self._app_block(),
            "env": {"environment": environment, "properties": properties},
        })

    async def handle_health(self, _request: web.Request) -> web.Response:
        dependency: list[dict[str, Any]] = []
        # optional services never affect the overall status (engine semantics)
        await self._check_services(self.optional, required=False, dependency=dependency)
        up = await self._check_services(self.required, required=True, dependency=dependency)
        self.healthy = up
        result: dict[str, Any] = {}
        if not dependency:
            result["message"] = ("Did you forget to define mandatory.health.dependencies "
                                 "or optional.health.dependencies")
        result["dependency"] = dependency
        result["status"] = "UP" if up else "DOWN"
        result["origin"] = app_origin()
        result["name"] = self.app_name
        return web.json_response(result, status=200 if up else 400)

    async def handle_livenessprobe(self, _request: web.Request) -> web.Response:
        if self.healthy:
            return web.Response(text="OK")
        return web.Response(status=400, text=UNHEALTHY)

    async def _check_services(self, services: list[str], *, required: bool,
                              dependency: list[dict[str, Any]]) -> bool:
        all_up = True
        for route in services:
            entry: dict[str, Any] = {"route": route, "required": required}
            dependency.append(entry)
            service = self.registry.get(route)
            if service is None:
                all_up = False
                entry["status_code"] = 404
                entry["message"] = f"Please check - Route {route} not found"
                continue
            bus = self.registry.bus
            # info is advisory - merge whatever the service reports about
            # itself; the health probe below decides the status
            with contextlib.suppress(DeliveryTimeout):
                info = await bus.deliver(service, {"type": "info"}, None, INFO_TIMEOUT_MS)
                if isinstance(info.body, dict):
                    entry.update(info.body)
            try:
                reply = await bus.deliver(service, {"type": "health"}, None, HEALTH_TIMEOUT_MS)
                entry["status_code"] = reply.get_status()
                if isinstance(reply.body, (str, dict)):
                    entry["message"] = reply.body
                if reply.has_error():
                    all_up = False
            except DeliveryTimeout as e:
                all_up = False
                entry["status_code"] = 408
                entry["message"] = f"Please check - {e}"
        return all_up
