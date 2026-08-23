"""Actuator endpoint pins: engine-parity operational surface for Kubernetes."""

import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from mercury_composable import AppException, Body, FunctionRegistry, __version__, app_config
from mercury_composable.actuator import app_origin, elapsed_time
from mercury_composable.server import EventApiServer

ORIGIN_SHAPE = r"\d{8}[0-9a-f]{32}"  # UTC yyyyMMdd + 32-hex uuid (Java reference format)

CONFIG_KEYS = [
    "mandatory.health.dependencies", "optional.health.dependencies",
    "show.env.variables", "show.application.properties",
    "application.name", "info.app.description", "info.app.version",
]


@pytest.fixture(autouse=True)
def clean_config() -> Iterator[None]:
    yield
    config = app_config()
    for key in CONFIG_KEYS:
        config.set(key, "")  # empty override = unset (the Actuator treats "" as absent)


@asynccontextmanager
async def actuator_server(registry: FunctionRegistry) -> AsyncIterator[str]:
    # the Actuator reads its configuration at construction (engine semantics),
    # so each test sets config BEFORE entering this context
    server = EventApiServer(registry)
    runner = web.AppRunner(server.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()
        await registry.bus.close()


async def get_json(url: str) -> tuple[int, Any]:
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.json()


async def get_text(url: str) -> tuple[int, str]:
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.text()


def engine_contract_registry() -> FunctionRegistry:
    """A health-check function speaking the engines' type=info/type=health contract."""
    registry = FunctionRegistry()

    async def health(headers: dict[str, str], _body: Body):
        if headers.get("type") == "info":
            return {"service": "demo.service", "href": "http://127.0.0.1"}
        return "demo.service is running fine"

    registry.register("demo.health", health, private=True)
    return registry


async def test_info_reports_identity_runtime_origin():
    config = app_config()
    config.set("application.name", "unit-app")
    config.set("info.app.description", "actuator test app")
    async with actuator_server(FunctionRegistry()) as url:
        status, info = await get_json(f"{url}/info")
    assert status == 200
    assert info["app"] == {"name": "unit-app", "version": __version__,
                           "description": "actuator test app"}
    assert info["runtime"]["language"] == "python"
    assert info["runtime"]["mercury_composable"] == __version__
    assert re.fullmatch(ORIGIN_SHAPE, info["origin"])
    assert info["time"]["start"] <= info["time"]["current"]
    assert "up_time" in info


async def test_info_routes_splits_by_visibility():
    registry = FunctionRegistry()

    async def noop(_headers: dict[str, str], _body: Body):
        return None

    registry.register("unit.public.route", noop, instances=8)
    registry.register("unit.private.route", noop, instances=2, private=True)
    async with actuator_server(registry) as url:
        status, result = await get_json(f"{url}/info/routes")
    assert status == 200
    assert result["routing"] == {"public": {"unit.public.route": 8},
                                 "private": {"unit.private.route": 2}}
    assert result["app"]["name"] == "application"


async def test_env_shows_only_opted_in_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MERCURY_UNIT_ENV", "unit-value")
    config = app_config()
    config.set("show.env.variables", "MERCURY_UNIT_ENV, MERCURY_UNIT_ABSENT")
    config.set("show.application.properties", "application.name")
    config.set("application.name", "unit-app")
    async with actuator_server(FunctionRegistry()) as url:
        status, result = await get_json(f"{url}/env")
    assert status == 200
    # a missing environment variable renders as an empty string (engine parity)
    assert result["env"]["environment"] == {"MERCURY_UNIT_ENV": "unit-value",
                                            "MERCURY_UNIT_ABSENT": ""}
    assert result["env"]["properties"] == {"application.name": "unit-app"}


async def test_health_up_with_engine_contract_dependency():
    app_config().set("mandatory.health.dependencies", "demo.health")
    async with actuator_server(engine_contract_registry()) as url:
        status, health = await get_json(f"{url}/health")
        live = await get_text(f"{url}/livenessprobe")
    assert status == 200
    assert health["status"] == "UP"
    assert health["name"] == "application"
    assert re.fullmatch(ORIGIN_SHAPE, health["origin"])
    # the info map merges into the dependency entry; health decides the status
    assert health["dependency"] == [{
        "route": "demo.health", "required": True,
        "service": "demo.service", "href": "http://127.0.0.1",
        "status_code": 200, "message": "demo.service is running fine",
    }]
    assert live == (200, "OK")


async def test_health_down_missing_dependency_drives_liveness():
    app_config().set("mandatory.health.dependencies", "no.such.route")
    async with actuator_server(FunctionRegistry()) as url:
        assert await get_text(f"{url}/livenessprobe") == (200, "OK")  # healthy until proven
        status, health = await get_json(f"{url}/health")
        live_status, live_text = await get_text(f"{url}/livenessprobe")
    assert status == 400
    assert health["status"] == "DOWN"
    assert health["dependency"] == [{
        "route": "no.such.route", "required": True,
        "status_code": 404, "message": "Please check - Route no.such.route not found",
    }]
    assert live_status == 400
    assert live_text == "Unhealthy. Please check '/health' endpoint."


async def test_optional_failure_never_downs_health():
    registry = engine_contract_registry()

    async def broken(_headers: dict[str, str], _body: Body):
        raise AppException(500, "backend down")

    registry.register("broken.health", broken, private=True)
    config = app_config()
    config.set("mandatory.health.dependencies", "demo.health")
    config.set("optional.health.dependencies", "broken.health")
    async with actuator_server(registry) as url:
        status, health = await get_json(f"{url}/health")
    assert status == 200
    assert health["status"] == "UP"
    broken_dep = next(d for d in health["dependency"] if d["route"] == "broken.health")
    assert broken_dep["required"] is False
    assert broken_dep["status_code"] == 500
    assert broken_dep["message"] == "backend down"


async def test_health_without_dependencies_teaches():
    async with actuator_server(FunctionRegistry()) as url:
        status, health = await get_json(f"{url}/health")
    assert status == 200
    assert health["status"] == "UP"
    assert health["dependency"] == []
    assert health["message"].startswith("Did you forget to define")


def test_elapsed_time_matches_engine_rendering():
    assert elapsed_time(0) == "0 ms"
    assert elapsed_time(500) == "500 ms"
    assert elapsed_time(1000) == "1 second"
    assert elapsed_time(61_000) == "1 minute 1 second"
    # the engines' strict boundary behavior, kept verbatim
    assert elapsed_time(60_000) == "60 seconds"
    assert elapsed_time(3_600_000) == "60 minutes"
    assert elapsed_time(86_400_000) == "24 hours"
    assert elapsed_time(120_000) == "2 minutes"
    assert elapsed_time(90_061_000) == "1 day 1 hour 1 minute 1 second"


def test_origin_is_stable_and_engine_shaped():
    assert app_origin() == app_origin()  # minted once per process
    assert re.fullmatch(ORIGIN_SHAPE, app_origin())
