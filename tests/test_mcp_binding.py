"""The MCP binding (extensions/mcp-binding/mcp_binding.py).

The shared contract file tests/vectors/mcp-binding-vectors.json (byte-identical in mercury-nodejs) pins
the pure half: the input-schema digest, the allowlist verification and the result mapping. The tests
after the vector runs drive the real transports against the fixture server of tests/fixtures
(a small MCP server on the official SDK, over stdio and over streamable HTTP): the boot that registers
one route per allowlisted tool and fails fast by name, the calls through the registered handlers, the
credential references that reach the subprocess and never a log, the health route, the describe
output, and the platform's startup hook that runs the boot before the first request.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiohttp import web

from mercury_composable import AppException, app_config, default_registry, platform

_TESTS = Path(__file__).resolve().parent
_BINDING = _TESTS.parent / "extensions" / "mcp-binding" / "mcp_binding.py"
_FIXTURE = _TESTS / "fixtures" / "mcp_test_server.py"
_VECTORS = _TESTS / "vectors" / "mcp-binding-vectors.json"
# the Node.js twin pins the same digest: change the file in both packs or in neither
_VECTORS_SHA256 = "4bacc319859847ff82e5d169ad9c0dea4ffdd6fe06883537ebf5ee66bdba907a"

_spec = importlib.util.spec_from_file_location("mcp_binding_under_test", _BINDING)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes are
# unknowable statically - Any tells every analyzer to trust the runtime
binding: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("mcp_binding_under_test", binding)
_spec.loader.exec_module(binding)

VECTORS: dict[str, Any] = json.loads(_VECTORS.read_text(encoding="utf-8"))

FIXTURE_TOOLS = ("echo", "add", "fail", "slow", "env_present")


def _spec_of(case_server: dict[str, Any]) -> Any:
    tools = tuple(binding.ToolBinding(name=n, route=t["route"], schema_sha256=t["schema_sha256"])
                  for n, t in case_server["tools"].items())
    return binding.ServerSpec(name=case_server["name"], transport="stdio", tools=tools, command="x")


# --- the shared vectors -------------------------------------------------------------------


def test_the_vector_file_is_the_one_the_node_twin_runs() -> None:
    digest = hashlib.sha256(_VECTORS.read_bytes()).hexdigest()
    assert digest == _VECTORS_SHA256, "update both packs' pins when the shared file changes"


@pytest.mark.parametrize("case", VECTORS["digest_cases"], ids=lambda c: c["name"])
def test_schema_digest_contract(case: dict[str, Any]) -> None:
    assert binding.schema_digest(case["schema"]) == case["sha256"]


@pytest.mark.parametrize("case", VECTORS["verify_cases"], ids=lambda c: c["name"])
def test_verify_contract(case: dict[str, Any]) -> None:
    spec = _spec_of(case["server"])
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(binding.BindingError) as failure:
            binding.verify(spec, case["offered"])
        assert str(failure.value) == expect["error"]
    else:
        bound = binding.verify(spec, case["offered"])
        assert [b.route for b, _tool in bound] == expect["bound"]


@pytest.mark.parametrize("case", VECTORS["result_cases"], ids=lambda c: c["name"])
def test_tool_result_contract(case: dict[str, Any]) -> None:
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(AppException) as failure:
            binding.tool_result(case["result"])
        assert failure.value.status == expect["error"]["status"]
        assert failure.value.message == expect["error"]["message"]
    else:
        assert binding.tool_result(case["result"]) == expect["reply"]


# --- the fixture server over the real transports ------------------------------------------


@pytest.fixture(scope="module")
def offered() -> dict[str, dict[str, Any]]:
    """What the fixture server offers, as the binding reads it: the digests the tests pin come
    from the server itself, so a change of the SDK's schema generation cannot break them."""
    spec = binding.ServerSpec(name="probe", transport="stdio", tools=(), command=sys.executable,
                              args=(str(_FIXTURE),))
    tools = asyncio.run(binding._describe(spec))
    return {tool["name"]: tool for tool in tools}


def _servers_file(tmp_path: Path, servers: dict[str, Any]) -> Path:
    path = tmp_path / "mcp-servers.yml"
    path.write_text(yaml.safe_dump({"servers": servers}, sort_keys=False), encoding="utf-8")
    return path


def _stdio(offered: dict[str, dict[str, Any]], tools: dict[str, str], **extra: Any) -> dict[str, Any]:
    """A stdio server entry for the fixture, every tool with the digest the server reports."""
    entry: dict[str, Any] = {
        "transport": "stdio", "command": sys.executable, "args": [str(_FIXTURE)],
        "tools": {name: {"route": route, "schema_sha256": binding.schema_digest(offered[name]["input_schema"])}
                  for name, route in tools.items()},
    }
    entry.update(extra)
    return entry


async def _call(route: str, body: Any) -> Any:
    service = default_registry.get(route)
    assert service is not None, f"{route} is not registered"
    return await service.handler({}, body)


@pytest.fixture
def servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the binding at a temporary server file and give the test a fresh Binding."""
    def point(entries: dict[str, Any]) -> Any:
        path = _servers_file(tmp_path, entries)
        app_config().set("mcp.servers", str(path))
        return binding.Binding()
    yield point
    app_config().set("mcp.servers", binding.DEFAULT_SERVERS_FILE)


@pytest.mark.asyncio
async def test_boot_registers_one_route_per_allowlisted_tool_and_calls_them(
        offered: dict[str, dict[str, Any]], servers: Any) -> None:
    app = servers({"fixture": _stdio(offered, {"echo": "fixture.echo", "add": "fixture.add", "fail": "fixture.fail"})})
    await app.boot()
    try:
        assert sorted(app.servers) == ["fixture"]
        assert [b.route for b, _ in app.servers["fixture"].bound] == ["fixture.echo", "fixture.add", "fixture.fail"]
        assert await _call("fixture.echo", {"text": "hello"}) == "hello"
        assert await _call("fixture.add", {"a": 1, "b": 2}) == {"sum": 3}
        with pytest.raises(AppException) as failure:
            await _call("fixture.fail", {"reason": "the vault is closed"})
        assert failure.value.status == 400
        assert "the vault is closed" in failure.value.message
        with pytest.raises(AppException) as not_a_map:
            await _call("fixture.echo", "hello")
        assert not_a_map.value.status == 400
        assert "must be a map" in not_a_map.value.message
    finally:
        await app.shutdown()
    assert not app.servers


@pytest.mark.asyncio
async def test_a_slow_tool_is_a_408_within_the_servers_timeout(offered: dict[str, dict[str, Any]], servers: Any) -> None:
    app = servers({"fixture": _stdio(offered, {"slow": "fixture.slow"}, timeout_ms=1000)})
    await app.boot()
    try:
        started = time.monotonic()
        with pytest.raises(AppException) as failure:
            await _call("fixture.slow", {"seconds": 5})
        assert failure.value.status == 408, failure.value.message
        assert "within 1000 ms" in failure.value.message
        assert time.monotonic() - started < 4
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_a_missing_tool_fails_the_boot_by_name(offered: dict[str, dict[str, Any]], servers: Any,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    entry = _stdio(offered, {"echo": "fixture.echo"})
    entry["tools"]["nope"] = {"route": "fixture.nope", "schema_sha256": "0" * 64}
    app = servers({"fixture": entry})
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stop:
        await app.boot()
    assert stop.value.code == 1
    assert "does not offer the tool nope" in caplog.text
    assert not app.servers, "a failed boot leaves no connection behind"


@pytest.mark.asyncio
async def test_a_changed_schema_fails_the_boot_by_name(offered: dict[str, dict[str, Any]], servers: Any,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    entry = _stdio(offered, {"echo": "fixture.echo"})
    entry["tools"]["echo"]["schema_sha256"] = "f" * 64
    app = servers({"fixture": entry})
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await app.boot()
    assert "changed the input schema of echo" in caplog.text
    assert binding.schema_digest(offered["echo"]["input_schema"]) in caplog.text, "the current digest is shown"


@pytest.mark.asyncio
async def test_credential_references_reach_the_subprocess_and_never_a_log(
        offered: dict[str, dict[str, Any]], servers: Any, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("FIXTURE_TOKEN", "a-value-that-must-not-appear")
    app = servers({"fixture": _stdio(offered, {"env_present": "fixture.env.present"},
                                     env={"FIXTURE_FORWARDED": "${FIXTURE_TOKEN}"})})
    with caplog.at_level(logging.DEBUG):
        await app.boot()
        try:
            assert await _call("fixture.env.present", {"name": "FIXTURE_FORWARDED"}) == {"present": True}
            assert await _call("fixture.env.present", {"name": "FIXTURE_TOKEN"}) == {"present": False}, \
                "only the listed variables reach the subprocess, not the whole environment"
        finally:
            await app.shutdown()
    assert "a-value-that-must-not-appear" not in caplog.text


def test_an_unset_credential_reference_is_refused_at_load(offered: dict[str, dict[str, Any]], servers: Any,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIXTURE_MISSING_TOKEN", raising=False)
    servers({"fixture": _stdio(offered, {"echo": "fixture.echo"}, env={"TOKEN": "${FIXTURE_MISSING_TOKEN}"})})
    with pytest.raises(binding.BindingError) as failure:
        binding.load_servers()
    assert "env.TOKEN resolves to nothing" in str(failure.value)


@pytest.mark.parametrize(("entry", "message"), [
    ({"transport": "ftp", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}}, "transport must be stdio or http"),
    ({"transport": "stdio", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}}, "a stdio server needs a command"),
    ({"transport": "http", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}}, "an http server needs a url"),
    ({"transport": "stdio", "command": "x", "url": "http://x", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}},
     "url and headers belong to an http server"),
    ({"transport": "stdio", "command": "x", "colour": "blue", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}},
     "has unknown keys colour"),
    ({"transport": "stdio", "command": "x", "tools": {}}, "tools must map at least one remote tool"),
    ({"transport": "stdio", "command": "x", "tools": {"echo": {"route": "NoDots", "schema_sha256": "0" * 64}}}, "Invalid route name"),
    ({"transport": "stdio", "command": "x", "tools": {"echo": {"route": "a.b", "schema_sha256": "short"}}}, "needs schema_sha256"),
    ({"transport": "stdio", "command": "x", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64, "mode": "x"}}},
     "has unknown keys mode"),
    ({"transport": "stdio", "command": "x", "timeout_ms": 10, "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}},
     "timeout_ms must be at least 1000"),
    ({"transport": "stdio", "command": "x", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64},
                                                        "add": {"route": "a.b", "schema_sha256": "0" * 64}}},
     "the route a.b is bound twice"),
])
def test_the_server_file_is_checked_before_any_connection(servers: Any, entry: dict[str, Any], message: str) -> None:
    servers({"s": entry})
    with pytest.raises(binding.BindingError) as failure:
        binding.load_servers()
    assert message in str(failure.value), str(failure.value)


def test_a_route_bound_by_two_servers_is_refused(servers: Any) -> None:
    tool = {"echo": {"route": "shared.route", "schema_sha256": "0" * 64}}
    servers({"one": {"transport": "stdio", "command": "x", "tools": tool},
             "two": {"transport": "stdio", "command": "y", "tools": tool}})
    with pytest.raises(binding.BindingError) as failure:
        binding.load_servers()
    assert "bound by both one and two" in str(failure.value)


def test_an_empty_server_map_starts_with_no_route(servers: Any, caplog: pytest.LogCaptureFixture) -> None:
    servers({})
    with caplog.at_level(logging.WARNING):
        assert binding.load_servers() == []
    assert "No MCP server configured" in caplog.text


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(port: int, process: subprocess.Popen[bytes], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"the fixture server exited with {process.returncode}")
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError("the fixture server did not listen in time")


@pytest.fixture
def http_fixture_port():
    """The fixture server over streamable HTTP, started synchronously so the async test only talks to it."""
    port = _free_port()
    with subprocess.Popen([sys.executable, str(_FIXTURE), "http", str(port)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            _wait_for(port, process)
            yield port
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.asyncio
async def test_the_http_transport_serves_the_same_routes(offered: dict[str, dict[str, Any]], servers: Any,
                                                         monkeypatch: pytest.MonkeyPatch, http_fixture_port: int) -> None:
    monkeypatch.setenv("FIXTURE_HTTP_TOKEN", "bearer-value")
    app = servers({"remote": {
        "transport": "http", "url": f"http://127.0.0.1:{http_fixture_port}/mcp",
        "headers": {"Authorization": "Bearer ${FIXTURE_HTTP_TOKEN}"},
        "tools": {"echo": {"route": "remote.echo", "schema_sha256": binding.schema_digest(offered["echo"]["input_schema"])},
                  "add": {"route": "remote.add", "schema_sha256": binding.schema_digest(offered["add"]["input_schema"])}},
    }})
    await app.boot()
    try:
        assert await _call("remote.echo", {"text": "over http"}) == "over http"
        assert await _call("remote.add", {"a": 20, "b": 22}) == {"sum": 42}
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_health_reports_each_server_and_its_connection(offered: dict[str, dict[str, Any]], servers: Any) -> None:
    app = servers({"fixture": _stdio(offered, {"echo": "fixture.echo"})})
    await app.boot()
    try:
        binding.binding.servers = app.servers
        info = await binding.mcp_health({"type": "info"}, {})
        assert info["service"] == "mcp.binding"
        assert info["servers"] == [{"name": "fixture", "transport": "stdio", "tools": 1, "connected": True}]
        assert await binding.mcp_health({"type": "health"}, {}) == "mcp.binding is running fine"
    finally:
        await app.shutdown()
    binding.binding.servers = app.servers
    assert (await binding.mcp_health({"type": "info"}, {}))["servers"] == []
    lost = binding.Server(binding.ServerSpec(name="gone", transport="stdio", tools=(), command="x"))
    binding.binding.servers = {"gone": lost}
    try:
        with pytest.raises(AppException) as failure:
            await binding.mcp_health({"type": "health"}, {})
        assert failure.value.status == 503
        assert "gone" in failure.value.message
    finally:
        binding.binding.servers = {}


def test_describe_prints_the_digests_and_a_catalog_row(offered: dict[str, dict[str, Any]], servers: Any,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    servers({"fixture": _stdio(offered, {"echo": "fixture.echo"})})
    assert binding.describe(["fixture"]) == 0
    out = capsys.readouterr().out
    assert f"schema_sha256: {binding.schema_digest(offered['echo']['input_schema'])}" in out
    assert "## echo - bound as fixture.echo (digest ok)" in out
    assert "## add - not allowlisted" in out
    assert '"route": "fixture.echo"' in out
    assert '"description": "Echo the text back."' in out
    assert binding.describe(["unknown"]) == 1
    assert "Unknown server(s): unknown" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_platform_runs_startup_and_cleanup_callbacks_around_the_app() -> None:
    events: list[str] = []

    async def start() -> None:
        events.append("start")

    async def stop() -> None:
        events.append("stop")

    fresh = platform.__class__(default_registry)
    fresh.on_startup(start)
    fresh.on_cleanup(stop)
    runner = web.AppRunner(fresh.create_app())
    await runner.setup()
    assert events == ["start"], "the startup callback runs before the app listens"
    await runner.cleanup()
    assert events == ["start", "stop"]


@pytest.mark.asyncio
async def test_a_failing_startup_callback_stops_the_app_before_it_listens() -> None:
    async def start() -> None:
        raise SystemExit(1)

    fresh = platform.__class__(default_registry)
    fresh.on_startup(start)
    runner = web.AppRunner(fresh.create_app())
    with pytest.raises(SystemExit):
        await runner.setup()
    await runner.cleanup()


def test_the_fixture_server_has_the_tools_the_tests_rely_on(offered: dict[str, dict[str, Any]]) -> None:
    assert sorted(offered) == sorted(FIXTURE_TOOLS)
    assert os.path.isfile(_FIXTURE)
