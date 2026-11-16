"""The MCP binding (extensions/mcp-binding/mcp_binding.py).

The shared contract file tests/vectors/mcp-binding-vectors.json (byte-identical in mercury-nodejs) pins
the pure half: the input-schema digest, the verification of the tools the functions bind against the tools a
server offers, and the result mapping. The tests after the vector runs drive the real transports against the
fixture server of tests/fixtures (a small MCP server on the official SDK, over stdio and over streamable HTTP):
the boot that verifies every binding and fails fast by name, the calls through functions that bind the
fixture's tools (local RPC through the bus, as the host dispatches them), the credential references that reach
the subprocess and never a log, the health route, the describe output, and the platform's startup hook that
runs the boot before the first request.
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
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from aiohttp import web

from mercury_composable import (
    AppException,
    EventEnvelope,
    PostOffice,
    app_config,
    default_registry,
    platform,
    preload,
)

_TESTS = Path(__file__).resolve().parent
_BINDING = _TESTS.parent / "extensions" / "mcp-binding" / "mcp_binding.py"
_FIXTURE = _TESTS / "fixtures" / "mcp_test_server.py"
_VECTORS = _TESTS / "vectors" / "mcp-binding-vectors.json"
# the Node.js twin pins the same digest: change the file in both packs or in neither
_VECTORS_SHA256 = "b470762efe1d77145c430cb3f946956256126f3e4e53e8d001cfed35316ee6a2"

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


# --- the shared vectors -------------------------------------------------------------------


def test_the_vector_file_is_the_one_the_node_twin_runs() -> None:
    digest = hashlib.sha256(_VECTORS.read_bytes()).hexdigest()
    assert digest == _VECTORS_SHA256, "update both packs' pins when the shared file changes"


@pytest.mark.parametrize("case", VECTORS["digest_cases"], ids=lambda c: c["name"])
def test_schema_digest_contract(case: dict[str, Any]) -> None:
    assert binding.schema_digest(case["schema"]) == case["sha256"]


@pytest.mark.parametrize("case", VECTORS["verify_cases"], ids=lambda c: c["name"])
def test_verify_contract(case: dict[str, Any]) -> None:
    server = case["server"]["name"]
    bindings = [binding.ToolBinding(server=server, tool=b["tool"], schema_sha256=b["schema_sha256"])
                for b in case["bindings"]]
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(binding.BindingError) as failure:
            binding.verify(server, bindings, case["offered"])
        assert str(failure.value) == expect["error"]
    else:
        bound = binding.verify(server, bindings, case["offered"])
        assert [b.tool for b, _tool in bound] == expect["bound"]


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
    spec = binding.ServerSpec(name="probe", transport="stdio", command=sys.executable, args=(str(_FIXTURE),))
    tools = asyncio.run(binding.offered_tools(spec))
    return {tool["name"]: tool for tool in tools}


def _servers_file(tmp_path: Path, servers: dict[str, Any]) -> Path:
    path = tmp_path / "mcp-servers.yml"
    path.write_text(yaml.safe_dump({"servers": servers}, sort_keys=False), encoding="utf-8")
    return path


def _stdio(**extra: Any) -> dict[str, Any]:
    """A stdio server entry for the fixture - the servers file names servers only; the tools are bound in code."""
    entry: dict[str, Any] = {"transport": "stdio", "command": sys.executable, "args": [str(_FIXTURE)]}
    entry.update(extra)
    return entry


def _serve(app: Any, offered: dict[str, dict[str, Any]], route: str, server: str, tool: str, *,
           instances: int = 10, digest: str | None = None) -> Any:
    """The documented shape: bind a tool of a server with the digest the server reports (or the one
    given), and declare a @preload function under the route that calls it."""
    call = app.bind(server, tool, schema_sha256=digest or binding.schema_digest(offered[tool]["input_schema"]))

    @preload(route=route, instances=instances)
    async def function(_headers: dict[str, str], body: Any) -> Any:
        return await call(body)

    return call


@pytest_asyncio.fixture
async def po() -> AsyncIterator[PostOffice]:
    """Local RPC through the registry's bus, the path a request takes behind the HTTP host."""
    office = PostOffice(registry=default_registry)
    yield office
    # the bus binds a route's workers to the loop of their first use - drop them for the next test's loop
    await default_registry.bus.close()


async def _call(po: PostOffice, route: str, body: Any) -> EventEnvelope:
    assert default_registry.get(route) is not None, f"{route} is not registered"
    return await po.request(route, body=body, timeout_ms=10000)


@pytest.fixture
def servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, Any]], Any]]:
    """Point the binding at a temporary server file and give the test a fresh Binding, installed as
    the module's one instance - the one mcp_health and describe read."""
    def point(entries: dict[str, Any]) -> Any:
        path = _servers_file(tmp_path, entries)
        app_config().set("mcp.servers", str(path))
        app = binding.Binding()
        monkeypatch.setattr(binding, "binding", app)
        return app
    yield point
    app_config().set("mcp.servers", binding.DEFAULT_SERVERS_FILE)


def test_the_example_function_binds_the_mermaid_tool_out_of_the_box() -> None:
    # the extension's own function: declared with @preload like any route, public, bound in code
    service = default_registry.get("mermaid.render")
    assert service is not None
    assert service.handler is binding.mermaid_render
    assert not service.private
    assert (binding.render_mermaid.server, binding.render_mermaid.tool) == ("mermaid", "validate_and_render_mermaid_diagram")
    assert binding.DIGEST.fullmatch(binding.render_mermaid.declared.schema_sha256)
    assert default_registry.get("mcp.tool") is None, "the generic route is gone - a tool is a function"


@pytest.mark.asyncio
async def test_boot_verifies_every_binding_and_the_functions_call_their_tools(
        offered: dict[str, dict[str, Any]], servers: Any, po: PostOffice) -> None:
    app = servers({"fixture": _stdio()})
    echo = _serve(app, offered, "fixture.echo", "fixture", "echo", instances=3)
    _serve(app, offered, "fixture.add", "fixture", "add")
    _serve(app, offered, "fixture.fail", "fixture", "fail")
    await app.boot()
    try:
        assert sorted(app.servers) == ["fixture"]
        assert [d.tool for d, _ in app.servers["fixture"].bound] == ["echo", "add", "fail"]
        service = default_registry.get("fixture.echo")
        assert service is not None
        assert service.instances == 3
        assert not service.private
        reply = await _call(po, "fixture.echo", {"text": "hello"})
        assert reply.get_status() == 200
        assert reply.body == "hello"
        assert (await _call(po, "fixture.add", {"a": 1, "b": 2})).body == {"sum": 3}
        failed = await _call(po, "fixture.fail", {"reason": "the vault is closed"})
        assert failed.get_status() == 400
        assert "the vault is closed" in str(failed.body)
        not_a_map = await _call(po, "fixture.echo", "hello")
        assert not_a_map.get_status() == 400
        assert "must be a map" in str(not_a_map.body)
        assert (echo.server, echo.tool) == ("fixture", "echo")
    finally:
        await app.shutdown()
    assert not app.servers
    # the declarations are code and survive a shutdown; a call before the next boot is a 503 by name
    assert [d.tool for d in app.declared] == ["echo", "add", "fail"]
    gone = await _call(po, "fixture.echo", {"text": "later"})
    assert gone.get_status() == 503
    assert "MCP server fixture is not connected" in str(gone.body)


@pytest.mark.asyncio
async def test_two_functions_may_bind_the_same_tool(offered: dict[str, dict[str, Any]], servers: Any,
                                                   po: PostOffice) -> None:
    app = servers({"fixture": _stdio()})
    _serve(app, offered, "fixture.echo.one", "fixture", "echo")
    _serve(app, offered, "fixture.echo.two", "fixture", "echo")
    await app.boot()
    try:
        assert len(app.servers["fixture"].bound) == 2
        assert (await _call(po, "fixture.echo.one", {"text": "1"})).body == "1"
        assert (await _call(po, "fixture.echo.two", {"text": "2"})).body == "2"
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_a_slow_tool_is_a_408_within_the_servers_timeout(offered: dict[str, dict[str, Any]], servers: Any,
                                                               po: PostOffice) -> None:
    app = servers({"fixture": _stdio(timeout_ms=1000)})
    _serve(app, offered, "fixture.slow", "fixture", "slow")
    await app.boot()
    try:
        started = time.monotonic()
        reply = await _call(po, "fixture.slow", {"seconds": 5})
        assert reply.get_status() == 408, reply.body
        assert "within 1000 ms" in str(reply.body)
        assert time.monotonic() - started < 4
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_a_missing_tool_fails_the_boot_by_name(offered: dict[str, dict[str, Any]], servers: Any,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    app = servers({"fixture": _stdio()})
    _serve(app, offered, "fixture.echo", "fixture", "echo")
    _serve(app, offered, "fixture.nope", "fixture", "nope", digest="0" * 64)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stop:
        await app.boot()
    assert stop.value.code == 1
    assert "does not offer the tool nope" in caplog.text
    assert not app.servers, "a failed boot leaves no connection behind"


@pytest.mark.asyncio
async def test_a_changed_schema_fails_the_boot_by_name(offered: dict[str, dict[str, Any]], servers: Any,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    app = servers({"fixture": _stdio()})
    _serve(app, offered, "fixture.echo", "fixture", "echo", digest="f" * 64)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await app.boot()
    assert "changed the input schema of echo" in caplog.text
    assert binding.schema_digest(offered["echo"]["input_schema"]) in caplog.text, "the current digest is shown"


@pytest.mark.asyncio
async def test_a_binding_to_an_unconfigured_server_fails_the_boot_by_name(
        offered: dict[str, dict[str, Any]], servers: Any, caplog: pytest.LogCaptureFixture) -> None:
    app = servers({"fixture": _stdio()})
    _serve(app, offered, "ghost.echo", "ghost", "echo")
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await app.boot()
    assert "No MCP server named ghost is configured - a function binds its tool echo" in caplog.text
    assert not app.servers


@pytest.mark.asyncio
async def test_a_configured_server_nobody_binds_is_not_connected(offered: dict[str, dict[str, Any]], servers: Any,
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    # the spare server's command does not exist: it is never started, so the boot still succeeds
    app = servers({"fixture": _stdio(), "spare": {"transport": "stdio", "command": "no-such-mcp-server-command"}})
    _serve(app, offered, "fixture.echo", "fixture", "echo")
    with caplog.at_level(logging.WARNING):
        await app.boot()
    try:
        assert sorted(app.servers) == ["fixture"]
        assert "MCP server spare: no function binds a tool of it - not connected" in caplog.text
    finally:
        await app.shutdown()


@pytest.mark.parametrize(("server", "tool", "digest", "message"), [
    ("", "echo", "0" * 64, "needs the server's name"),
    ("fixture", "bad name!", "0" * 64, "is not a tool name"),
    ("fixture", "echo", "short", "needs schema_sha256"),
])
def test_bind_refuses_a_bad_declaration(server: str, tool: str, digest: str, message: str) -> None:
    client = binding.Binding()
    with pytest.raises(binding.BindingError) as failure:
        client.bind(server, tool, schema_sha256=digest)
    assert message in str(failure.value), str(failure.value)


@pytest.mark.asyncio
async def test_credential_references_reach_the_subprocess_and_never_a_log(
        offered: dict[str, dict[str, Any]], servers: Any, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, po: PostOffice) -> None:
    monkeypatch.setenv("FIXTURE_TOKEN", "a-value-that-must-not-appear")
    app = servers({"fixture": _stdio(env={"FIXTURE_FORWARDED": "${FIXTURE_TOKEN}"})})
    _serve(app, offered, "fixture.env.present", "fixture", "env_present")
    with caplog.at_level(logging.DEBUG):
        await app.boot()
        try:
            assert (await _call(po, "fixture.env.present", {"name": "FIXTURE_FORWARDED"})).body == {"present": True}
            assert (await _call(po, "fixture.env.present", {"name": "FIXTURE_TOKEN"})).body == {"present": False}, \
                "only the listed variables reach the subprocess, not the whole environment"
        finally:
            await app.shutdown()
    assert "a-value-that-must-not-appear" not in caplog.text


def test_an_unset_credential_reference_is_refused_at_load(servers: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIXTURE_MISSING_TOKEN", raising=False)
    servers({"fixture": _stdio(env={"TOKEN": "${FIXTURE_MISSING_TOKEN}"})})
    with pytest.raises(binding.BindingError) as failure:
        binding.load_servers()
    assert "env.TOKEN resolves to nothing" in str(failure.value)


@pytest.mark.parametrize(("entry", "message"), [
    ({"transport": "ftp"}, "transport must be stdio or http"),
    ({"transport": "stdio"}, "a stdio server needs a command"),
    ({"transport": "http"}, "an http server needs a url"),
    ({"transport": "stdio", "command": "x", "url": "http://x"}, "url and headers belong to an http server"),
    ({"transport": "stdio", "command": "x", "colour": "blue"}, "has unknown keys colour"),
    ({"transport": "stdio", "command": "x", "timeout_ms": 10}, "timeout_ms must be at least 1000"),
    # the allowlist row of the first shape: the tools are bound in the code now, and the file says so
    ({"transport": "stdio", "command": "x", "tools": {"echo": {"route": "a.b", "schema_sha256": "0" * 64}}},
     "the tools are bound in the code, not in this file"),
    ({"transport": "stdio", "command": "x", "instances": 3}, "has unknown keys instances"),
])
def test_the_server_file_is_checked_before_any_connection(servers: Any, entry: dict[str, Any], message: str) -> None:
    servers({"s": entry})
    with pytest.raises(binding.BindingError) as failure:
        binding.load_servers()
    assert message in str(failure.value), str(failure.value)


def test_an_empty_server_map_loads_no_server(servers: Any, caplog: pytest.LogCaptureFixture) -> None:
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
async def test_the_http_transport_serves_the_same_functions(offered: dict[str, dict[str, Any]], servers: Any,
                                                            monkeypatch: pytest.MonkeyPatch, http_fixture_port: int,
                                                            po: PostOffice) -> None:
    monkeypatch.setenv("FIXTURE_HTTP_TOKEN", "bearer-value")
    app = servers({"remote": {
        "transport": "http", "url": f"http://127.0.0.1:{http_fixture_port}/mcp",
        "headers": {"Authorization": "Bearer ${FIXTURE_HTTP_TOKEN}"},
    }})
    _serve(app, offered, "remote.echo", "remote", "echo")
    _serve(app, offered, "remote.add", "remote", "add")
    await app.boot()
    try:
        assert (await _call(po, "remote.echo", {"text": "over http"})).body == "over http"
        assert (await _call(po, "remote.add", {"a": 20, "b": 22})).body == {"sum": 42}
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_health_reports_each_server_and_its_connection(offered: dict[str, dict[str, Any]], servers: Any,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    app = servers({"fixture": _stdio()})
    _serve(app, offered, "fixture.echo", "fixture", "echo")
    await app.boot()
    try:
        info = await binding.mcp_health({"type": "info"}, {})
        assert info["service"] == "mcp.binding"
        assert info["servers"] == [{"name": "fixture", "transport": "stdio", "tools": 1, "connected": True}]
        assert await binding.mcp_health({"type": "health"}, {}) == "mcp.binding is running fine"
    finally:
        await app.shutdown()
    assert (await binding.mcp_health({"type": "info"}, {}))["servers"] == []
    lost = binding.Server(binding.ServerSpec(name="gone", transport="stdio", command="x"))
    monkeypatch.setattr(binding.binding, "servers", {"gone": lost})
    with pytest.raises(AppException) as failure:
        await binding.mcp_health({"type": "health"}, {})
    assert failure.value.status == 503
    assert "gone" in failure.value.message


def test_describe_prints_the_digests_the_bind_lines_and_a_catalog_row(
        offered: dict[str, dict[str, Any]], servers: Any, capsys: pytest.CaptureFixture[str]) -> None:
    app = servers({"fixture": _stdio()})
    _serve(app, offered, "fixture.echo", "fixture", "echo")
    assert binding.describe(["fixture"]) == 0
    out = capsys.readouterr().out
    echo_digest = binding.schema_digest(offered["echo"]["input_schema"])
    add_digest = binding.schema_digest(offered["add"]["input_schema"])
    assert f"schema_sha256: {echo_digest}" in out
    assert "## echo - bound (digest ok)" in out
    assert "## add - not bound" in out
    assert f'bind("fixture", "add", schema_sha256="{add_digest}")' in out
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
