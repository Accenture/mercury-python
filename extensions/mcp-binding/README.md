# The MCP binding

A library and a function host that serves the tools of [MCP](https://modelcontextprotocol.io) servers as composable
functions, one `@preload` function per tool, bound in code to the remote tool through the binding's encapsulated MCP
client - adapter family B of the AI agent orchestration concept, built as step 4 of RFC-0011 / ADR-0033 (the agent loop)
and reshaped by the engines' design spec `draft-design-specs/agent-tool-use.md` (2026-11-16). A graph's agent loop
(`llm.run` in the engines) names a route in its tool catalog; the engine reaches the route through declarative
Event-over-HTTP like any other function; the function here issues `tools/call` to the server behind it and returns the
result. The engines stay MCP-free: a stdio server runs as a subprocess of this process, never of an engine, and the
server's credentials stay in this process's environment.

The principle: **a tool is a composable function**, whatever stands behind it. A route names the capability, never the
transport (`mermaid.render`, not `mcp.mermaid.render`), because a tool may start as an MCP tool and later be implemented
another way, and the graph must not know.

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
`-Dkey=value` overrides any key. It listens on port 8088 by default (`rest.server.port`). Out of the box it serves one
tool: `mermaid.render`, bound to the public Mermaid Chart MCP endpoint named in the default server file, which needs no
credential. Its log ends with `MCP server mermaid: tool validate_and_render_mermaid_diagram bound`.

## Declare a tool

A tool is declared where every route is declared, in the code of `mcp_binding.py` (or of an application that imports
it):

```python
render = bind("mermaid", "validate_and_render_mermaid_diagram",
              schema_sha256="5ae6d75d4ecfab919d9ef934bd679605bdbfc8b90b4d46f4d9dbd54d19c275ad")

@preload(route="mermaid.render", instances=10)
async def mermaid_render(_headers: dict[str, str], body: Body):
    return await render(body)
```

- **`bind(server, tool, schema_sha256)`** names the server as the server file names it, the remote tool and the SHA-256
  digest of the tool's input schema (`describe` prints it), and returns the tool as an async callable: the arguments in,
  the result out, with the contract below.
- **The function is ordinary.** It may pass the arguments through, shape them, or add behaviour around the call; it is
  public or private like any function, and its `instances` are its own.
- **Boot verifies every declaration** before the first request (the platform's startup hook): it connects every server
  a function binds a tool of, lists its tools, and refuses to start - by name - when a bound server is not configured,
  a bound tool is not offered, or its input schema no longer hashes to the pinned digest. A configured server nobody
  binds is not connected (a warning); a tool the server offers but nothing binds is unreachable.

Adding a tool is a server entry when the server is new, these few lines, a catalog row on the graph and an
Event-over-HTTP entry on the engine.

## The server file

`mcp.servers` names the file (default `mcp-servers.yml` in the resources folder; a relative path is read from there).
It holds what is environment-specific, never the tools:

```yaml
servers:
  mermaid:
    transport: http
    url: 'https://chatgpt.mermaid.ai/anthropic/mcp'
    timeout_ms: 20000
  documents:
    transport: stdio
    command: npx
    args: ['-y', '@modelcontextprotocol/server-filesystem', '/srv/documents']
  tracker:
    transport: http
    url: 'https://mcp.example.internal/mcp'
    headers:
      Authorization: 'Bearer ${TRACKER_MCP_TOKEN}'
    timeout_ms: 20000
```

| Key | Meaning |
| --- | --- |
| `transport` | `stdio` (a command this process starts) or `http` (a streamable HTTP endpoint). Required. |
| `command`, `args`, `env`, `cwd` | The stdio server. The subprocess gets the SDK's minimal default environment plus the variables under `env`, each a credential reference such as `${GITHUB_TOKEN}`, resolved from this process's environment at boot. |
| `url`, `headers` | The http server and the headers it is reached with; a header value may be a reference such as `Bearer ${TOKEN}`. |
| `timeout_ms` | How long one `tools/call` may take; default `mcp.call.timeout.ms` (30000), at least 1000. |

Any other key is refused by name, so a misspelt setting never falls back to a default; a leftover `tools` allowlist from
the binding's first shape is refused with a pointer to the code. A reference that resolves to nothing (`${TOKEN}` with no
`TOKEN` set) stops the boot by name, and a resolved value is never logged.

### Digests: the server cannot change a tool under the graph

A graph's catalog row carries the tool's description and input schema, written on the graph and certified with it, and
the model reads the graph's text, never the server's. The digest ties the two together: `schema_sha256` is the SHA-256 of
the tool's input schema in canonical JSON (keys sorted, no spaces, Unicode kept), pinned next to the function that binds
the tool. At boot the binding lists each server's tools and refuses to start when a bound tool is missing or its schema
no longer hashes to the pinned digest, so a server-side change stops a deploy with its reason instead of becoming
run-time errors a model improvises around. Print the digests, the `bind` line to paste and a catalog row to review before
the graph carries it, with:

```bash
python extensions/mcp-binding/mcp_binding.py describe mermaid
```

```text
# MCP server mermaid (http) offers 1 tool(s)

## validate_and_render_mermaid_diagram - bound (digest ok)
schema_sha256: 5ae6d75d4ecfab919d9ef934bd679605bdbfc8b90b4d46f4d9dbd54d19c275ad
bind("mermaid", "validate_and_render_mermaid_diagram", schema_sha256="5ae6d75d…")
catalog row (name your function's route; review the description before the graph carries it):
{
  "name": "validate_and_render_mermaid_diagram",
  "route": "<the route of the function that binds it>",
  "description": "...",
  "input_schema": { ... }
}
```

A tool the server offers but no function binds is shown as `not bound` and is never served.

## The function contract

| | |
| --- | --- |
| Request | The event body is the tool's arguments, a map. Anything else is a 400. |
| Reply | The server's structured content when it gives one, else the text of its content blocks joined by newlines; a non-text block is named by its type (`[image]`, `[resource_link] file:///...`). |
| 400 | The server marked the result an error (`isError`): the reply carries the server's text. The agent loop returns it to the model as that call's error, so the model reads it and chooses another step. |
| 408 | The server did not answer within `timeout_ms`. |
| 502 | The server answered outside the protocol, or the SDK reported a protocol error. |
| 503 | The server is not connected (its process ended, the endpoint is gone, or the binding has not booted). |

The functions are ordinary composable functions: a Java or Rust engine maps them in its `yaml.event.over.http` file to
this app's `/api/event`, and a graph's catalog row names the route - or a function of the engine application calls it and
adds business logic of its own, as the Java Playground's `diagram.render` does in front of `mermaid.render`:

```yaml
event:
  http:
    - route: 'mermaid.render'
      target: 'http://127.0.0.1:8088/api/event'
```

```json
{ "name": "render_diagram", "route": "mermaid.render", "strict": true,
  "description": "Validate a Mermaid diagram and render it as SVG.",
  "input_schema": { "type": "object", "properties": { "diagramCode": { "type": "string" } }, "required": ["diagramCode"] } }
```

One session per server is shared by the functions bound to it, held by a task of its own for the life of the process.
The health route `mcp.health` (the `/health` actuator, `mandatory.health.dependencies`) reports each connected server,
its transport, how many tools its functions bind and whether it is connected, and answers 503 naming a server whose
session is gone.

**Tracing.** A trace names the function's route, as for any function: the route's own record, the execution context's
route, the reply's sender and the host's `Handled` log line all read `mermaid.render`. A traced call from an engine leaves
the records a Java or Rust callee leaves (the pack's guide `docs/guides/config-logging-actuators.md`): the edge's round
trip `http.request` (`POST /api/event`), parented onto the engine function that called; `event.api.service` under it; and
the route's round-trip record under that, carrying the execution's span, its `exec_time` and the `round_trip` - the MCP
server's `tools/call` is inside that `exec_time`, not a span of its own. Driven live through the Java Playground's
`diagram-assistant` graph on 2026-11-14 (under the route's earlier name), the lineage is continuous from the engine's
`diagram.render` into this function; the record is the engines' `docs/test-reports/agent-loop-trace-lineage.md`.

## Security

- **No engine runs a child process.** The MCP client, the subprocess and the credentials live here, in the pack, behind
  the engine's Event-over-HTTP map (the agent loop's design spec in the mercury-composable repository,
  `draft-design-specs/llm-helper-agent-loop.md`, section 9, and `draft-design-specs/agent-tool-use.md`).
- **Credentials are references**, resolved from the environment at boot and never logged. The subprocess receives the
  SDK's minimal default environment plus the variables listed, not this process's whole environment.
- **The bindings in the code are the surface.** A tool no function binds is not served; a bound tool whose schema changed
  is not served either, until the digest is reviewed and updated. What an application can reach is read in its code and
  certified with it.
- **The model never reads the server's descriptions.** The catalog row on the graph is the developer's text, reviewed
  through `describe`, so injection through a tool description is closed by construction.

## Tests

```bash
pip install -e '.[dev]'
pytest tests/test_mcp_binding.py
```

The shared vector file pins the pure half (the digest, the verification of declared bindings, the result mapping) in
both packs; the rest of the tests drive a small MCP server of this repository (`tests/fixtures/mcp_test_server.py`, on
the official SDK) over stdio and over streamable HTTP: the boot that verifies every binding and fails fast by name, the
calls through functions that bind the fixture's tools, the credential references that reach the subprocess and never a
log, the health route, the describe output, and the platform's startup hook that runs the boot before the first request.
