"""
Function registry and the @preload decorator.

Mirrors the engines' PreLoad vocabulary: a function is registered under a
route name with an instance count (its concurrency limit) and a private flag.
Handlers take ``(headers: dict, body)`` — the same two-part input as a
TypedLambdaFunction — and return the reply body (or an EventEnvelope for full
control of status and reply headers). Both ``async def`` and plain ``def``
handlers are supported; synchronous handlers run in the default executor so
they never block the event loop.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    handler: Callable[[dict[str, str], Any], Any]
    instances: int = 10
    private: bool = False
    is_async: bool = False


class FunctionRegistry:
    def __init__(self) -> None:
        self._services: dict[str, ServiceDef] = {}

    def register(self, route: str, handler: Callable, *,
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
        def handle_event(headers: dict, body):
            return {"text": body["text"].upper()}
    """
    def wrapper(fn: Callable) -> Callable:
        default_registry.register(route, fn, instances=instances, private=private)
        return fn
    return wrapper
