"""
The MCP binding - a library and a function host that serves the tools of MCP servers as composable
functions, one ``@preload`` function per tool, bound in code to the remote tool through this module's
encapsulated MCP client (RFC-0011 / ADR-0033; the engines' ``draft-design-specs/agent-tool-use.md``).
The engines stay MCP-free: a graph's agent loop (``llm.run``) names a route in its tool catalog, the
engine reaches it through declarative Event-over-HTTP like any other function, and the function here
issues ``tools/call`` to the server behind it. A stdio server runs as a subprocess of this process,
never of an engine, and its credentials stay in this process's environment.

A tool is declared where every route is declared - in the code::

    render = bind("mermaid", "validate_and_render_mermaid_diagram", schema_sha256="5ae6d75d...")

    @preload(route="mermaid.render", instances=10)
    async def mermaid_render(_headers, body):
        return await render(body)

``bind`` names the server (as the server file names it), the remote tool and the SHA-256 digest of
the tool's input schema, and returns the tool as an async callable. Boot connects every server a
function binds a tool of, lists its tools and verifies every binding before the first request:
a server that is not configured, a tool the server does not offer or an input schema whose digest
differs from the pinned one stops the application by name, so a server-side change stops a deploy
instead of becoming run-time errors a model improvises around. A route names the capability, never
the transport: a tool may start as an MCP tool and later be implemented another way, and the graph
must not know.

Run:  pip install 'mercury-composable[mcp]'
      mercury-serve extensions/mcp-binding/mcp_binding.py

Servers are named in ``resources/mcp-servers.yml`` (the ``mcp.servers`` key of ``application.yml``
names the file): each has its transport (``stdio`` with a command, or ``http`` with a URL), its
credential references (``${ENV_VAR}`` placeholders, resolved from this process's environment at
boot and never logged) and its call timeout - what is environment-specific. The tools are in the code.

    python extensions/mcp-binding/mcp_binding.py describe [server ...]

prints each server's tools with their digests, the ``bind`` line to paste and a catalog row for the
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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import yaml

try:
    import httpx2
    from mcp import ClientSession, types
    from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError
except ImportError as missing_sdk:  # the SDK is this app's one dependency - teach the fix
    raise SystemExit(
        "The MCP binding needs the MCP SDK - pip install 'mercury-composable[mcp]'"
    ) from missing_sdk

from mercury_composable import AppException, Body, app_config, get_logger, platform, preload

log = get_logger(__name__)

# --- the contract's defaults and limits ----------------------------------------------

DEFAULT_SERVERS_FILE = "mcp-servers.yml"
DEFAULT_CALL_TIMEOUT_MS = 30000
DEFAULT_INSTANCES = 10
MIN_CALL_TIMEOUT_MS = 1000
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)
SERVER_KEYS = ("transport", "command", "args", "env", "cwd", "url", "headers", "timeout_ms")
# the MCP specification's recommendation for a tool name
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
DIGEST = re.compile(r"[0-9a-f]{64}")
# the API-safe name a graph's catalog row may use, derived for the describe output
API_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


class BindingError(Exception):
    """A declaration, configuration or boot failure: the application must not start."""


@dataclass(frozen=True)
class ToolBinding:
    """One tool a function of this application binds: the server, the remote tool and the pinned
    digest of the tool's input schema."""
    server: str
    tool: str
    schema_sha256: str


@dataclass(frozen=True)
class ServerSpec:
    """One configured MCP server - what is environment-specific: the transport, the credential
    references and the call timeout. The tools are declared in the code."""
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS


# --- the pure half: digests, verification and the result mapping (shared vectors) -----------


def schema_digest(schema: Any) -> str:
    """The SHA-256 of a tool's input schema in canonical JSON: keys sorted, no spaces, Unicode
    kept. The same bytes in both packs, so a digest pinned by one is checked by the other."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify(server: str, bindings: Sequence[ToolBinding],
           offered: list[dict[str, Any]]) -> list[tuple[ToolBinding, dict[str, Any]]]:
    """Match the tools bound to a server against the tools it offers (``tools/list`` as plain maps
    with ``name``, ``description`` and ``input_schema``). Every bound tool must be offered and its
    input schema must hash to the pinned digest; the first failure, in declaration order, names
    the tool."""
    by_name = {str(tool.get("name")): tool for tool in offered}
    names: list[str] = sorted(by_name)
    bound: list[tuple[ToolBinding, dict[str, Any]]] = []
    for declared in bindings:
        tool = by_name.get(declared.tool)
        if tool is None:
            raise BindingError(
                f"MCP server {server} does not offer the tool {declared.tool} - "
                f"it offers {', '.join(names) or 'no tools'}")
        digest = schema_digest(tool.get("input_schema"))
        if digest != declared.schema_sha256:
            raise BindingError(
                f"MCP server {server} changed the input schema of {declared.tool} - "
                f"its digest is now {digest}, the binding pins {declared.schema_sha256}")
        bound.append((declared, tool))
    return bound


def tool_result(result: dict[str, Any]) -> Any:
    """The reply of a function from a ``CallToolResult`` as a plain map (``content``,
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


# --- configuration: the servers ----------------------------------------------------------


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


def _server(name: str, value: Any, default_timeout_ms: int) -> ServerSpec:
    if not isinstance(value, dict):
        raise BindingError(f"MCP server {name} must be a map")
    unknown = sorted(set(value) - set(SERVER_KEYS))
    if unknown:
        hint = " - the tools are bound in the code, not in this file" if "tools" in unknown else ""
        raise BindingError(f"MCP server {name} has unknown keys {', '.join(unknown)} - the keys are "
                           f"{', '.join(SERVER_KEYS)}{hint}")
    transport = str(value.get("transport") or "")
    if transport not in TRANSPORTS:
        raise BindingError(f"MCP server {name}: transport must be stdio or http")
    timeout_ms = int(value.get("timeout_ms") or default_timeout_ms)
    if timeout_ms < MIN_CALL_TIMEOUT_MS:
        raise BindingError(f"MCP server {name}: timeout_ms must be at least {MIN_CALL_TIMEOUT_MS}")
    if transport == TRANSPORT_STDIO:
        return _stdio_server(name, value, timeout_ms)
    return _http_server(name, value, timeout_ms)


def _stdio_server(name: str, value: dict[str, Any], timeout_ms: int) -> ServerSpec:
    """A stdio server: the command this process starts, its arguments, its environment references
    and its folder."""
    command = _resolve(value.get("command"))
    if not command:
        raise BindingError(f"MCP server {name}: a stdio server needs a command")
    raw_args = value.get("args") or []
    if not isinstance(raw_args, list):
        raise BindingError(f"MCP server {name}: args must be a list")
    if value.get("url") or value.get("headers"):
        raise BindingError(f"MCP server {name}: url and headers belong to an http server")
    cwd = _resolve(value.get("cwd"))
    return ServerSpec(name=name, transport=TRANSPORT_STDIO, command=str(command),
                      args=tuple(str(_resolve(a)) for a in raw_args),
                      env=_str_map(name, "env", value.get("env")), cwd=None if cwd is None else str(cwd),
                      timeout_ms=timeout_ms)


def _http_server(name: str, value: dict[str, Any], timeout_ms: int) -> ServerSpec:
    """An http server: the streamable HTTP URL and the headers it is reached with."""
    url = _resolve(value.get("url"))
    if not url:
        raise BindingError(f"MCP server {name}: an http server needs a url")
    if value.get("command") or value.get("args") or value.get("env") or value.get("cwd"):
        raise BindingError(f"MCP server {name}: command, args, env and cwd belong to a stdio server")
    return ServerSpec(name=name, transport=TRANSPORT_HTTP, url=str(url),
                      headers=_str_map(name, "headers", value.get("headers")), timeout_ms=timeout_ms)


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
        log.warning("No MCP server configured in %s - a function that binds a tool cannot start", file)
    return [_server(str(name), value, default_timeout) for name, value in servers.items()]


# --- one connection per server, held by its own task --------------------------------------


class Server:
    """One MCP server: its session lives in a task of its own, so the transport's context managers
    are entered and left on one task (the SDK's cancel scopes require it), and every function bound
    to the server shares the session."""

    def __init__(self, spec: ServerSpec, bindings: Sequence[ToolBinding] = ()):
        self.spec = spec
        self.bindings = tuple(bindings)
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
            # a transport closing noisily must not fail the shutdown
            except Exception:  # noqa: BLE001
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
            # the SDK's own HTTP client, with this server's headers and the call timeout as its deadline
            client = httpx2.AsyncClient(headers=spec.headers or None, timeout=httpx2.Timeout(seconds, read=seconds),
                                        follow_redirects=True)
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
                self.bound = verify(self.spec.name, self.bindings, offered)
                self.session = session
                self._ready.set()
                await self._closing.wait()
        except asyncio.CancelledError:
            raise
        # a failure of the transport or the handshake is reported to the starter, or logged at
        # run time; a cancellation passes through above
        except Exception as exc:  # noqa: BLE001
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


# --- the client: bind a tool, boot the servers, report the health ------------------------------


class BoundTool:
    """A remote tool as an async callable - what ``bind`` returns and a function calls with the
    tool's arguments (a map, else a 400). The reply is the result as ``tool_result`` shapes it; a
    server that is not connected is a 503."""

    def __init__(self, client: Binding, declared: ToolBinding):
        self._client = client
        self.declared = declared

    @property
    def server(self) -> str:
        return self.declared.server

    @property
    def tool(self) -> str:
        return self.declared.tool

    async def __call__(self, arguments: Body) -> Any:
        if not isinstance(arguments, dict):
            raise AppException(400, f"The arguments of {self.tool} must be a map")
        server = self._client.servers.get(self.server)
        if server is None:
            raise AppException(503, f"MCP server {self.server} is not connected")
        return await server.call(self.tool, arguments)


class Binding:
    """The encapsulated MCP client of this application: the tools its functions bind, and the
    servers' sessions once booted."""

    def __init__(self) -> None:
        self.servers: dict[str, Server] = {}
        # every tool a function of this application binds, in declaration order
        self.declared: list[ToolBinding] = []

    def bind(self, server: str, tool: str, *, schema_sha256: str) -> BoundTool:
        """Declare that a function of this application serves a tool of a server: the server as the
        server file names it, the remote tool's name and the digest of its input schema (``describe``
        prints it). Boot verifies the declaration against the live server before the first request."""
        name = str(server or "").strip()
        if not name:
            raise BindingError(f"bind() needs the server's name for the tool {tool} - the one the server file uses")
        if not TOOL_NAME.fullmatch(str(tool or "")):
            raise BindingError(f"'{tool}' is not a tool name (letters, digits, '_', '.' and '-', up to 128)")
        digest = str(schema_sha256 or "").lower()
        if not DIGEST.fullmatch(digest):
            raise BindingError(f"The tool {tool} of {name} needs schema_sha256, the 64-hex digest of its input schema "
                               f"- run 'python mcp_binding.py describe {name}' to print it")
        declared = ToolBinding(server=name, tool=str(tool), schema_sha256=digest)
        self.declared.append(declared)
        return BoundTool(self, declared)

    def declared_for(self, server: str) -> tuple[ToolBinding, ...]:
        return tuple(d for d in self.declared if d.server == server)

    async def boot(self) -> None:
        """Connect every server a function binds a tool of and verify every binding - before the
        first request; a failure stops the application with its reason."""
        try:
            specs = load_servers()
            configured = {spec.name for spec in specs}
            for declared in self.declared:
                if declared.server not in configured:
                    raise BindingError(f"No MCP server named {declared.server} is configured - "
                                       f"a function binds its tool {declared.tool}")
            for spec in specs:
                bindings = self.declared_for(spec.name)
                if not bindings:
                    log.warning("MCP server %s: no function binds a tool of it - not connected", spec.name)
                    continue
                server = Server(spec, bindings)
                await server.start()
                self.servers[spec.name] = server
                for declared, _schema in server.bound:
                    log.info("MCP server %s: tool %s bound", spec.name, declared.tool)
            bound = sum(len(server.bound) for server in self.servers.values())
            log.info("MCP binding ready - %d server(s), %d bound tool(s)", len(self.servers), bound)
        # any boot failure stops the application, by name
        except Exception as exc:  # noqa: BLE001
            leaf = _leaf(exc)
            log.error("MCP binding cannot start - %s", leaf)
            await self.shutdown()
            raise SystemExit(1) from leaf

    async def shutdown(self) -> None:
        for server in self.servers.values():
            await server.close()
        self.servers.clear()


binding = Binding()
bind = binding.bind


@preload(route="mcp.health", instances=5, private=True)
async def mcp_health(headers: dict[str, str], _body: Body) -> Any:
    """Health check speaking the engines' interface contract (type=info / type=health): which
    servers are connected and how many tools the functions of this application bind on each."""
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


# --- the functions: a tool of an MCP server is a composable function of this application -------
#
# Declare each tool with bind(server, tool, schema_sha256) - the server as the server file names
# it, the remote tool's name and the digest `describe` prints - and call it from an ordinary
# @preload function, which may pass the arguments through, shape them or add behaviour around the
# call. Name the route after the capability, never the transport.

render_mermaid = bind("mermaid", "validate_and_render_mermaid_diagram",
                      schema_sha256="5ae6d75d4ecfab919d9ef934bd679605bdbfc8b90b4d46f4d9dbd54d19c275ad")


@preload(route="mermaid.render", instances=DEFAULT_INSTANCES)
async def mermaid_render(_headers: dict[str, str], body: Body) -> Any:
    """Validate a Mermaid diagram and render it as SVG - the tool of the public Mermaid Chart MCP
    server the default server file names. The body is the tool's arguments (``diagramCode``);
    the reply is the server's structured result."""
    return await render_mermaid(body)


# --- describe: the digests, the bind lines and the catalog rows for the developer ----------------


def _api_safe(name: str) -> str:
    return API_SAFE.sub("_", name)[:128] or "tool"


async def offered_tools(spec: ServerSpec) -> list[dict[str, Any]]:
    """The tools a server offers, listed over a connection of its own (the ``describe`` probe)."""
    probe = Server(spec)
    await probe.start()
    try:
        session = probe.session
        assert session is not None
        return await _list_tools(session)
    finally:
        await probe.close()


def describe(names: list[str]) -> int:
    """Print each server's tools, their input-schema digests, the ``bind`` line that declares each
    and a catalog row for the graph."""
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
        offered = asyncio.run(offered_tools(spec))
        declared = {d.tool: d for d in binding.declared_for(spec.name)}
        print(f"# MCP server {spec.name} ({spec.transport}) offers {len(offered)} tool(s)")
        for tool in offered:
            name = tool["name"]
            digest = schema_digest(tool["input_schema"])
            bound = declared.get(name)
            if bound is None:
                state = "not bound"
            elif bound.schema_sha256 == digest:
                state = "bound (digest ok)"
            else:
                state = "bound (DIGEST CHANGED)"
            print(f"\n## {name} - {state}\nschema_sha256: {digest}")
            print(f'bind("{spec.name}", "{name}", schema_sha256="{digest}")')
            row = {
                "name": _api_safe(name),
                "route": "<the route of the function that binds it>",
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            print("catalog row (name your function's route; review the description before the graph carries it):")
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
