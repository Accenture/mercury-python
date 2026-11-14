"""
The MCP binding - a polyglot function host that serves the tools of MCP servers as composable
routes, one route per allowlisted remote tool (RFC-0011 / ADR-0033, section 9 of the agent loop's
design spec). The engines stay MCP-free: a graph's agent loop (``llm.run``) names a route in its
tool catalog, the engine reaches it through declarative Event-over-HTTP like any other function,
and this app issues ``tools/call`` to the server behind it. A stdio server runs as a subprocess of
this process, never of an engine, and its credentials stay in this process's environment.

Run:  pip install 'mercury-composable[mcp]'
      mercury-serve extensions/mcp-binding/mcp_binding.py

Servers are named in ``resources/mcp-servers.yml`` (the ``mcp.servers`` key of ``application.yml``
names the file): each has its transport (``stdio`` with a command, or ``http`` with a URL), its
credential references (``${ENV_VAR}`` placeholders, resolved from this process's environment at
boot and never logged), and its allowlist mapping each remote tool to a route with the SHA-256
digest of the tool's input schema. Boot fails fast when an allowlisted tool is missing from
``tools/list`` or its schema no longer matches the digest, so a server-side change stops a deploy
instead of becoming run-time errors a model improvises around.

    python extensions/mcp-binding/mcp_binding.py describe [server ...]

prints each server's tools with their digests and a catalog row to paste onto the graph, for the
developer to review: the model sees the graph's descriptions, never the server's.

The Node.js twin (mercury-nodejs extensions/mcp-binding) speaks the same contract, and both are
pinned by the shared vector file tests/vectors/mcp-binding-vectors.json.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import yaml

try:
    import httpx2
    from mcp import ClientSession, types
    from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    from mcp.shared.exceptions import MCPError
except ImportError as missing_sdk:  # the SDK is this app's one dependency - teach the fix
    raise SystemExit(
        "The MCP binding needs the MCP SDK - pip install 'mercury-composable[mcp]'"
    ) from missing_sdk

from mercury_composable import (
    AppException,
    Body,
    app_config,
    default_registry,
    get_logger,
    platform,
    preload,
)
from mercury_composable.registry import validate_route

log = get_logger(__name__)

# --- the contract's defaults and limits ----------------------------------------------

DEFAULT_SERVERS_FILE = "mcp-servers.yml"
DEFAULT_CALL_TIMEOUT_MS = 30000
DEFAULT_INSTANCES = 10
MIN_CALL_TIMEOUT_MS = 1000
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)
SERVER_KEYS = ("transport", "command", "args", "env", "cwd", "url", "headers", "timeout_ms",
               "instances", "tools")
TOOL_KEYS = ("route", "schema_sha256")
# the MCP specification's recommendation for a tool name
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
DIGEST = re.compile(r"[0-9a-f]{64}")
# the API-safe name a graph's catalog row may use for a route, derived for the describe output
API_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


class BindingError(Exception):
    """A configuration or boot failure: the application must not start."""


@dataclass(frozen=True)
class ToolBinding:
    """One allowlisted remote tool and the route that serves it."""
    name: str
    route: str
    schema_sha256: str


@dataclass(frozen=True)
class ServerSpec:
    """One configured MCP server."""
    name: str
    transport: str
    tools: tuple[ToolBinding, ...]
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS
    instances: int = DEFAULT_INSTANCES


# --- the pure half: digests, verification and the result mapping (shared vectors) -----------


def schema_digest(schema: Any) -> str:
    """The SHA-256 of a tool's input schema in canonical JSON: keys sorted, no spaces, Unicode
    kept. The same bytes in both packs, so a digest pinned by one is checked by the other."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify(spec: ServerSpec, offered: list[dict[str, Any]]) -> list[tuple[ToolBinding, dict[str, Any]]]:
    """Match the allowlist against the tools a server offers (``tools/list`` as plain maps with
    ``name``, ``description`` and ``input_schema``). Every allowlisted tool must be offered and its
    input schema must hash to the pinned digest; the first failure names the tool."""
    by_name = {str(tool.get("name")): tool for tool in offered}
    bound: list[tuple[ToolBinding, dict[str, Any]]] = []
    for binding in spec.tools:
        tool = by_name.get(binding.name)
        if tool is None:
            raise BindingError(
                f"MCP server {spec.name} does not offer the tool {binding.name} - "
                f"it offers {', '.join(sorted(by_name)) or 'no tools'}")
        digest = schema_digest(tool.get("input_schema"))
        if digest != binding.schema_sha256:
            raise BindingError(
                f"MCP server {spec.name} changed the input schema of {binding.name} - "
                f"its digest is now {digest}, the configuration pins {binding.schema_sha256}")
        bound.append((binding, tool))
    return bound


def tool_result(result: dict[str, Any]) -> Any:
    """The reply of a route from a ``CallToolResult`` as a plain map (``content``,
    ``structured_content``, ``is_error``): the structured content when the server gives it, else the
    text of the content blocks joined by newlines; a result marked ``is_error`` is a 400 carrying the
    server's text, which the agent loop returns to the model as that call's error."""
    content = result.get("content") or []
    texts: list[str] = []
    for block in content:
        block_type = block.get("type")
        if block_type == "text":
            texts.append(str(block.get("text", "")))
        elif block_type in ("resource_link", "resource"):
            resource = block.get("resource") or block
            texts.append(f"[{block_type}] {resource.get('uri', '')}".rstrip())
        else:
            texts.append(f"[{block_type}]")
    text = "\n".join(texts)
    if result.get("is_error"):
        raise AppException(400, text or "The tool reported an error without a message")
    structured = result.get("structured_content")
    if structured is not None:
        return structured
    return text


def as_plain(result: types.CallToolResult) -> dict[str, Any]:
    """The SDK's result as the plain map the shared contract speaks."""
    return {
        "content": [block.model_dump(mode="json", by_alias=False) for block in result.content],
        "structured_content": result.structured_content,
        "is_error": bool(result.is_error),
    }


def tool_as_plain(tool: types.Tool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}


# --- configuration ----------------------------------------------------------------------


def _resolve(value: Any) -> Any:
    """``${VAR}`` and ``${VAR:default}`` placeholders from the environment or the configuration, the
    pack's rule for every setting; the resolved value is never logged."""
    if isinstance(value, str):
        return app_config().resolve_text(value)
    return value


def _str_map(server: str, key: str, value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BindingError(f"MCP server {server}: {key} must be a map of names to values")
    resolved: dict[str, str] = {}
    for name, item in value.items():
        text = _resolve(item)
        if text is None or text == "":
            raise BindingError(f"MCP server {server}: {key}.{name} resolves to nothing - set the variable it names")
        resolved[str(name)] = str(text)
    return resolved


def _tools(server: str, value: Any) -> tuple[ToolBinding, ...]:
    if not isinstance(value, dict) or not value:
        raise BindingError(f"MCP server {server}: tools must map at least one remote tool name to its route and digest")
    bindings: list[ToolBinding] = []
    routes: set[str] = set()
    for name, item in value.items():
        name = str(name)
        if not TOOL_NAME.fullmatch(name):
            raise BindingError(f"MCP server {server}: '{name}' is not a tool name (letters, digits, '_', '.' and '-', up to 128)")
        if not isinstance(item, dict):
            raise BindingError(f"MCP server {server}: tool {name} must be a map with route and schema_sha256")
        unknown = sorted(set(item) - set(TOOL_KEYS))
        if unknown:
            raise BindingError(f"MCP server {server}: tool {name} has unknown keys {', '.join(unknown)} - the keys are route and schema_sha256")
        try:
            route = validate_route(str(item.get("route") or ""))
        except ValueError as exc:
            raise BindingError(f"MCP server {server}: tool {name}: {exc}") from exc
        if route in routes:
            raise BindingError(f"MCP server {server}: the route {route} is bound twice")
        routes.add(route)
        digest = str(item.get("schema_sha256") or "").lower()
        if not DIGEST.fullmatch(digest):
            raise BindingError(
                f"MCP server {server}: tool {name} needs schema_sha256, the 64-hex digest of its input schema "
                f"- run 'python mcp_binding.py describe {server}' to print it")
        bindings.append(ToolBinding(name=name, route=route, schema_sha256=digest))
    return tuple(bindings)


def _server(name: str, value: Any, default_timeout_ms: int) -> ServerSpec:
    if not isinstance(value, dict):
        raise BindingError(f"MCP server {name} must be a map")
    unknown = sorted(set(value) - set(SERVER_KEYS))
    if unknown:
        raise BindingError(f"MCP server {name} has unknown keys {', '.join(unknown)} - the keys are {', '.join(SERVER_KEYS)}")
    transport = str(value.get("transport") or "")
    if transport not in TRANSPORTS:
        raise BindingError(f"MCP server {name}: transport must be stdio or http")
    timeout_ms = int(value.get("timeout_ms") or default_timeout_ms)
    if timeout_ms < MIN_CALL_TIMEOUT_MS:
        raise BindingError(f"MCP server {name}: timeout_ms must be at least {MIN_CALL_TIMEOUT_MS}")
    instances = int(value.get("instances") or DEFAULT_INSTANCES)
    tools = _tools(name, value.get("tools"))
    if transport == TRANSPORT_STDIO:
        command = _resolve(value.get("command"))
        if not command:
            raise BindingError(f"MCP server {name}: a stdio server needs a command")
        raw_args = value.get("args") or []
        if not isinstance(raw_args, list):
            raise BindingError(f"MCP server {name}: args must be a list")
        args = tuple(str(_resolve(a)) for a in raw_args)
        cwd = _resolve(value.get("cwd"))
        if value.get("url") or value.get("headers"):
            raise BindingError(f"MCP server {name}: url and headers belong to an http server")
        return ServerSpec(name=name, transport=transport, tools=tools, command=str(command), args=args,
                          env=_str_map(name, "env", value.get("env")), cwd=None if cwd is None else str(cwd),
                          timeout_ms=timeout_ms, instances=instances)
    url = _resolve(value.get("url"))
    if not url:
        raise BindingError(f"MCP server {name}: an http server needs a url")
    if value.get("command") or value.get("args") or value.get("env") or value.get("cwd"):
        raise BindingError(f"MCP server {name}: command, args, env and cwd belong to a stdio server")
    return ServerSpec(name=name, transport=transport, tools=tools, url=str(url),
                      headers=_str_map(name, "headers", value.get("headers")),
                      timeout_ms=timeout_ms, instances=instances)


def load_servers(path: str | None = None) -> list[ServerSpec]:
    """The server file: ``mcp.servers`` names it (default ``mcp-servers.yml`` in the resources folder
    next to this file, or an absolute path)."""
    config = app_config()
    default_timeout = int(config.get("mcp.call.timeout.ms", DEFAULT_CALL_TIMEOUT_MS))
    file = path or str(config.get("mcp.servers", DEFAULT_SERVERS_FILE))
    if not os.path.isabs(file):
        base = config.resources_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
        file = os.path.join(base, file)
    if not os.path.isfile(file):
        raise BindingError(f"MCP server file not found - {file} (mcp.servers names it)")
    with open(file, encoding="utf-8") as f:
        data = yaml.safe_load(f.read()) or {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise BindingError(f"{file} must hold a 'servers' map")
    if not servers:
        log.warning("No MCP server configured in %s - the binding serves no tool route", file)
    specs = [_server(str(name), value, default_timeout) for name, value in servers.items()]
    routes: dict[str, str] = {}
    for spec in specs:
        for binding in spec.tools:
            owner = routes.get(binding.route)
            if owner:
                raise BindingError(f"The route {binding.route} is bound by both {owner} and {spec.name}")
            routes[binding.route] = spec.name
    return specs


# --- one connection per server, held by its own task --------------------------------------


class Server:
    """One MCP server: its session lives in a task of its own, so the transport's context managers
    are entered and left on one task (the SDK's cancel scopes require it), and every route of the
    server shares the session."""

    def __init__(self, spec: ServerSpec):
        self.spec = spec
        self.session: ClientSession | None = None
        self.bound: list[tuple[ToolBinding, dict[str, Any]]] = []
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    @property
    def connected(self) -> bool:
        return self.session is not None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.spec.name}")
        await self._ready.wait()
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        self._closing.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:  # noqa: BLE001 - a transport closing noisily must not fail the shutdown
                log.warning("MCP server %s closed with an error", self.spec.name)

    @asynccontextmanager
    async def _transport(self) -> AsyncIterator[tuple[Any, Any]]:
        spec = self.spec
        if spec.transport == TRANSPORT_STDIO:
            env = get_default_environment()
            env.update(spec.env)
            params = StdioServerParameters(command=str(spec.command), args=list(spec.args), env=env, cwd=spec.cwd)
            # the subprocess writes its own logs to this process's real stderr - a replaced
            # sys.stderr (a test's capture) has no file descriptor to inherit
            errlog = sys.__stderr__ or sys.stderr
            async with stdio_client(params, errlog=errlog) as streams:
                yield streams
        else:
            seconds = spec.timeout_ms / 1000
            client = create_mcp_http_client(headers=spec.headers or None,
                                            timeout=httpx2.Timeout(seconds, read=seconds))
            async with client, streamable_http_client(str(spec.url), http_client=client) as streams:
                yield streams

    async def _run(self) -> None:
        try:
            seconds = self.spec.timeout_ms / 1000
            async with (
                self._transport() as (read, write),
                ClientSession(read, write, read_timeout_seconds=seconds) as session,
            ):
                await session.initialize()
                offered = await _list_tools(session)
                self.bound = verify(self.spec, offered)
                self.session = session
                self._ready.set()
                await self._closing.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reported to the starter, or logged at run time
            leaf = _leaf(exc)
            if not self._ready.is_set():
                self._failure = leaf
            else:
                log.warning("MCP server %s disconnected: %s", self.spec.name, leaf)
        finally:
            self.session = None
            self._ready.set()

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        session = self.session
        if session is None:
            raise AppException(503, f"MCP server {self.spec.name} is not connected")
        try:
            result = await session.call_tool(name, arguments, read_timeout_seconds=self.spec.timeout_ms / 1000)
        except MCPError as exc:
            if "timed out" in str(exc.message).lower() or "timeout" in str(exc.message).lower():
                raise AppException(408, f"MCP server {self.spec.name} did not answer {name} within "
                                        f"{self.spec.timeout_ms} ms") from exc
            raise AppException(502, f"MCP server {self.spec.name} failed {name}: {exc.message}") from exc
        except TimeoutError as exc:
            raise AppException(408, f"MCP server {self.spec.name} did not answer {name} within "
                                    f"{self.spec.timeout_ms} ms") from exc
        if not isinstance(result, types.CallToolResult):
            raise AppException(502, f"MCP server {self.spec.name} answered {name} outside the tool contract")
        return tool_result(as_plain(result))


def _leaf(exc: BaseException) -> BaseException:
    """The first leaf of an exception group: the SDK's transports run in a task group, which wraps
    the failure of the code inside them."""
    # duck-typed, so Python 3.10 (where the group class is anyio's backport) reads the same
    while isinstance(getattr(exc, "exceptions", None), (list, tuple)) and exc.exceptions:  # type: ignore[attr-defined]
        exc = exc.exceptions[0]  # type: ignore[attr-defined]
    return exc


async def _list_tools(session: ClientSession) -> list[dict[str, Any]]:
    """Every page of ``tools/list`` as plain maps."""
    offered: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        offered.extend(tool_as_plain(tool) for tool in page.tools)
        cursor = page.next_cursor
        if not cursor:
            return offered


# --- the application: boot, the routes and the health check ------------------------------


class Binding:
    def __init__(self) -> None:
        self.servers: dict[str, Server] = {}

    async def boot(self) -> None:
        """Connect every configured server, verify its allowlist and register the routes - before
        the first request; a failure stops the application with its reason."""
        try:
            specs = load_servers()
            for spec in specs:
                server = Server(spec)
                await server.start()
                self.servers[spec.name] = server
                for binding, _tool in server.bound:
                    default_registry.register(binding.route, _handler(server, binding), instances=spec.instances)
                    log.info("MCP server %s: tool %s served as %s", spec.name, binding.name, binding.route)
            log.info("MCP binding ready - %d server(s), %d route(s)", len(self.servers),
                     sum(len(s.bound) for s in self.servers.values()))
        except Exception as exc:  # noqa: BLE001 - any boot failure stops the application, by name
            leaf = _leaf(exc)
            log.error("MCP binding cannot start - %s", leaf)
            await self.shutdown()
            raise SystemExit(1) from leaf

    async def shutdown(self) -> None:
        for server in list(self.servers.values()):
            await server.close()
        self.servers.clear()


def _handler(server: Server, binding: ToolBinding):
    async def call(_headers: dict[str, str], body: Body) -> Any:
        if not isinstance(body, dict):
            raise AppException(400, f"The arguments of {binding.name} must be a map")
        return await server.call(binding.name, body)
    call.__name__ = f"mcp_{binding.name}"
    return call


binding = Binding()


@preload(route="mcp.health", instances=5, private=True)
async def mcp_health(headers: dict[str, str], _body: Body) -> Any:
    """Health check speaking the engines' interface contract (type=info / type=health): which
    servers are configured, how many tools each serves, and whether each is connected."""
    if headers.get("type") == "info":
        return {
            "service": "mcp.binding",
            "href": "http://127.0.0.1",
            "servers": [
                {"name": s.spec.name, "transport": s.spec.transport, "tools": len(s.bound), "connected": s.connected}
                for s in binding.servers.values()
            ],
        }
    disconnected = [name for name, s in binding.servers.items() if not s.connected]
    if disconnected:
        raise AppException(503, f"MCP server(s) not connected: {', '.join(disconnected)}")
    return "mcp.binding is running fine"


# --- describe: the digests and the catalog rows for the developer ------------------------------


def _api_safe(name: str) -> str:
    return API_SAFE.sub("_", name)[:128] or "tool"


async def _describe(spec: ServerSpec) -> list[dict[str, Any]]:
    server = Server(spec)
    server_spec_without_check = ServerSpec(**{**spec.__dict__, "tools": ()})
    probe = Server(server_spec_without_check)
    await probe.start()
    try:
        session = probe.session
        assert session is not None
        offered = await _list_tools(session)
    finally:
        await probe.close()
    del server
    return offered


def describe(names: list[str]) -> int:
    """Print each server's tools, their input-schema digests and a catalog row for the graph."""
    try:
        specs = load_servers()
    except BindingError as exc:
        print(f"Cannot describe - {exc}", file=sys.stderr)
        return 1
    wanted = [s for s in specs if not names or s.name in names]
    missing = sorted(set(names) - {s.name for s in specs})
    if missing:
        print(f"Unknown server(s): {', '.join(missing)} - configured: {', '.join(s.name for s in specs)}", file=sys.stderr)
        return 1
    for spec in wanted:
        offered = asyncio.run(_describe(spec))
        configured = {b.name: b for b in spec.tools}
        print(f"# MCP server {spec.name} ({spec.transport}) offers {len(offered)} tool(s)")
        for tool in offered:
            name = tool["name"]
            digest = schema_digest(tool["input_schema"])
            bound = configured.get(name)
            state = ("bound as " + bound.route + (" (digest ok)" if bound.schema_sha256 == digest else " (DIGEST CHANGED)")) if bound else "not allowlisted"
            print(f"\n## {name} - {state}\nschema_sha256: {digest}")
            row = {
                "name": _api_safe(name),
                "route": bound.route if bound else "<route>",
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            print("catalog row (review the description before the graph carries it):")
            print(json.dumps(row, indent=2, ensure_ascii=False))
    return 0


def _main(argv: list[str]) -> int:
    # -Dkey=value arguments belong to the configuration, which reads them from sys.argv itself
    words = [a for a in argv if not a.startswith("-D")]
    if words and words[0] == "describe":
        return describe(words[1:])
    platform.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
else:
    # loaded by mercury-serve: the servers connect inside the serving loop, before the first request
    platform.on_startup(binding.boot)
    platform.on_cleanup(binding.shutdown)
