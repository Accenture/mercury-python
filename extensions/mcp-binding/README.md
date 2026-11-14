# The MCP binding

A polyglot function host that serves the tools of [MCP](https://modelcontextprotocol.io) servers as composable routes,
one route per allowlisted remote tool - adapter family B of the AI agent orchestration concept, built as step 4 of
RFC-0011 / ADR-0033 (the agent loop). A graph's agent loop (`llm.run` in the engines) names a route in its tool
catalog; the engine reaches the route through declarative Event-over-HTTP like any other function; this app issues
`tools/call` to the server behind it and returns the result. The engines stay MCP-free: a stdio server runs as a
subprocess of this process, never of an engine, and the server's credentials stay in this process's environment.

The client library is the official MCP SDK (`mcp` on PyPI, 2.x). The Node.js twin (mercury-nodejs
`extensions/mcp-binding`) speaks the same contract, and both are pinned by the shared vector file
`tests/vectors/mcp-binding-vectors.json`.

## Run

```bash
pip install 'mercury-composable[mcp]'
mercury-serve extensions/mcp-binding/mcp_binding.py
python extensions/mcp-binding/mcp_binding.py          # the same without mercury-serve
```

The binding reads `resources/application.yml` next to `mcp_binding.py`, whichever directory the command runs from;
`-Dkey=value` overrides any key. It listens on port 8088 by default (`rest.server.port`). With no server configured
it starts and serves no tool route.

## The server file

`mcp.servers` names the file (default `mcp-servers.yml` in the resources folder; a relative path is read from there):

```yaml
servers:
  documents:
    transport: stdio
    command: npx
    args: ['-y', '@modelcontextprotocol/server-filesystem', '/srv/documents']
    tools:
      read_text_file:
        route: docs.read.text.file
        schema_sha256: 'c6b1...'
      list_directory:
        route: docs.list.directory
        schema_sha256: '9a0e...'
  tracker:
    transport: http
    url: 'https://mcp.example.internal/mcp'
    headers:
      Authorization: 'Bearer ${TRACKER_MCP_TOKEN}'
    timeout_ms: 20000
    tools:
      create_issue:
        route: tracker.create.issue
        schema_sha256: '41d7...'
```

| Key | Meaning |
| --- | --- |
| `transport` | `stdio` (a command this process starts) or `http` (a streamable HTTP endpoint). Required. |
| `command`, `args`, `env`, `cwd` | The stdio server. The subprocess gets the SDK's minimal default environment plus the variables under `env`, each a credential reference such as `${GITHUB_TOKEN}`, resolved from this process's environment at boot. |
| `url`, `headers` | The http server and the headers it is reached with; a header value may be a reference such as `Bearer ${TOKEN}`. |
| `timeout_ms` | How long one `tools/call` may take; default `mcp.call.timeout.ms` (30000), at least 1000. |
| `instances` | How many calls one route serves at once; default 10. |
| `tools` | The allowlist: each remote tool name maps to `route` (a Mercury route name, lowercase with a period) and `schema_sha256`, the digest of the tool's input schema. Required, at least one. |

Any other key is refused by name, so a misspelt setting never falls back to a default. A reference that resolves to
nothing (`${TOKEN}` with no `TOKEN` set) stops the boot by name, and a resolved value is never logged. A route may be
bound once across all servers.

### Digests: the server cannot change a tool under the graph

A graph's catalog row carries the tool's description and input schema, written on the graph and certified with it, and
the model reads the graph's text, never the server's. The digest ties the two together: `schema_sha256` is the SHA-256 of
the tool's input schema in canonical JSON (keys sorted, no spaces, Unicode kept). At boot the binding lists each server's
tools and refuses to start when an allowlisted tool is missing or its schema no longer hashes to the pinned digest, so a
server-side change stops a deploy with its reason instead of becoming run-time errors a model improvises around. Print
the digests, and a catalog row to review before the graph carries it, with:

```bash
python extensions/mcp-binding/mcp_binding.py describe documents
```

```text
# MCP server documents (stdio) offers 14 tool(s)

## read_text_file - bound as docs.read.text.file (digest ok)
schema_sha256: c6b1...
catalog row (review the description before the graph carries it):
{
  "name": "read_text_file",
  "route": "docs.read.text.file",
  "description": "...",
  "input_schema": { ... }
}
```

A tool the server offers but the allowlist omits is shown as `not allowlisted` and is never served.

## The route contract

| | |
| --- | --- |
| Request | The event body is the tool's arguments, a map. Anything else is a 400. |
| Reply | The server's structured content when it gives one, else the text of its content blocks joined by newlines; a non-text block is named by its type (`[image]`, `[resource_link] file:///...`). |
| 400 | The server marked the result an error (`isError`): the reply carries the server's text. The agent loop returns it to the model as that call's error, so the model reads it and chooses another step. |
| 408 | The server did not answer within `timeout_ms`. |
| 502 | The server answered outside the protocol, or the SDK reported a protocol error. |
| 503 | The server is not connected (its process ended, or the endpoint is gone). |

The routes are ordinary composable functions: a Java or Rust engine maps them in its `yaml.event.over.http` file to this
app's `/api/event`, and a graph's catalog row names the route:

```yaml
event:
  http:
    - route: 'docs.read.text.file'
      target: 'http://127.0.0.1:8088/api/event'
```

```json
{ "name": "read_text_file", "route": "docs.read.text.file", "strict": true,
  "description": "Read a document of the store's policy folder by its path.",
  "input_schema": { "type": "object", "properties": { "path": { "type": "string" } }, "required": ["path"] } }
```

One session per server is shared by its routes, held by a task of its own for the life of the process. The health route
`mcp.health` (the `/health` actuator, `mandatory.health.dependencies`) reports each server, its transport, its tool count
and whether it is connected, and answers 503 naming a server whose session is gone.

Every bound route is served by one function, `mcp.tool`, declared with `@preload` like any composable function and private:
the host answers 403 for it on the wire, and it is bound to no tool. At boot, once a server's allowlist is verified, the
binding registers each allowlisted tool's route as an alias of that function - `default_registry.register(route, mcp_tool,
instances=...)`, the pack's counterpart of the engines' `Platform.register`, with the server's `instances` - so every route
is a service of its own, with its own mailbox and workers. A call learns the route it was addressed to from its execution
context (`get_trace().route`, the pack's counterpart of the engines' read-only `my_route` header), which names the server
and the remote tool; a call to `mcp.tool` by its own name is a 404.

## Security

- **No engine runs a child process.** The MCP client, the subprocess and the credentials live here, in the pack, behind
  the engine's Event-over-HTTP map (the agent loop's design spec in the mercury-composable repository,
  `draft-design-specs/llm-helper-agent-loop.md`, section 9).
- **Credentials are references**, resolved from the environment at boot and never logged. The subprocess receives the
  SDK's minimal default environment plus the variables listed, not this process's whole environment.
- **The allowlist is the surface.** A tool that is not listed is not served; a listed tool whose schema changed is not
  served either, until the digest is reviewed and updated.
- **The model never reads the server's descriptions.** The catalog row on the graph is the developer's text, reviewed
  through `describe`, so injection through a tool description is closed by construction.

## Tests

```bash
pip install -e '.[dev]'
pytest tests/test_mcp_binding.py
```

The shared vector file pins the pure half (the digest, the allowlist verification, the result mapping) in both packs;
the rest of the tests drive a small MCP server of this repository (`tests/fixtures/mcp_test_server.py`, on the official
SDK) over stdio and over streamable HTTP: the boot that registers one route per tool and fails fast by name, the calls,
the credential references that reach the subprocess and never a log, the health route, the describe output, and the
platform's startup hook that runs the boot before the first request.
