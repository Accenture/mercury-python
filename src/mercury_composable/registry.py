"""
Function registry and the @preload decorator.

Mirrors the engines' PreLoad vocabulary: a function is registered under a
route name with an instance count (its concurrency limit) and a private flag.
Handlers take ``(headers: dict[str, str], body)`` — the same two-part input as a
TypedLambdaFunction — and return the reply body (or an EventEnvelope for full
control of status and reply headers).

Both ``async def`` and plain ``def`` handlers are first-class, because Python
has two library ecosystems: plain ``def`` wraps the synchronous world
(``requests``, NumPy/pandas and most ML inference stacks, database drivers)
and runs in the default executor so a blocking call never stalls the event
loop — the Python analog of the Java engine's virtual threads; ``async def``
serves asyncio-native I/O and function composition. Detection is automatic
(``inspect.iscoroutinefunction``); sync handlers compose siblings via
``PostOffice.request_sync`` / ``send_sync`` (the sync bridge in client.py).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .bus import EventBus
from .envelope import Body

# the function contract: (headers, body) in, reply body (or EventEnvelope) out -
# mirrors the node package's exported Handler type
Handler = Callable[[dict[str, str], Body], Any]

_ROUTE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def validate_route(route: str) -> str:
    route = (route or "").strip()
    if not _ROUTE_PATTERN.fullmatch(route) or "." not in route:
        raise ValueError(
            f"Invalid route name '{route}' - use lowercase letters, digits, "
            "period, hyphen or underscore with at least one period")
    return route


@dataclass
class ServiceDef:
    route: str
    handler: Handler
    instances: int = 10
    private: bool = False
    is_async: bool = False


class FunctionRegistry:
    def __init__(self) -> None:
        self._services: dict[str, ServiceDef] = {}
        # the registry's own dispatch pipeline (see bus.py) - shared by the
        # HTTP host and the local side of PostOffice
        self.bus = EventBus()

    def register(self, route: str, handler: Handler, *,
                 instances: int = 10, private: bool = False) -> ServiceDef:
        route = validate_route(route)
        service = ServiceDef(
            route=route,
            handler=handler,
            instances=max(1, int(instances)),
            private=bool(private),
            is_async=inspect.iscoroutinefunction(handler),
        )
        self._services[route] = service
        return service

    def get(self, route: str) -> ServiceDef | None:
        return self._services.get(route)

    def exists(self, route: str) -> bool:
        return route in self._services

    def routes(self) -> dict[str, ServiceDef]:
        return dict(self._services)


# the default registry used by @preload and platform.run()
default_registry = FunctionRegistry()


def preload(route: str, instances: int = 10, private: bool = False):
    """Register a function handler under a route name (engine PreLoad analog).

    Usage::

        @preload(route="hello.python", instances=10)
        def handle_event(headers: dict[str, str], body):
            return {"text": body["text"].upper()}
    """
    def wrapper(fn: Handler) -> Handler:
        default_registry.register(route, fn, instances=instances, private=private)
        return fn
    return wrapper
