# Mercury Composable — polyglot functions for Python

Write decoupled functions in Python and let [Mercury Composable](https://github.com/Accenture/mercury-composable)
engines (Java, and the official [Rust port](https://github.com/Accenture/mercury)) orchestrate
them from Event Script flows and MiniGraph knowledge graphs — with no orchestration code in
Python at all.

This package is a deliberately **lightweight wrapper of the Event-over-HTTP protocol**:

- an **Event API host** (`POST /api/event`) that dispatches incoming event envelopes to your
  registered functions,
- a **thin client** (`PostOffice`) to call functions on peer applications the same way,
- the **standard event envelope wire format** codec (language-neutral MsgPack), and
- a **primitive in-process event bus** — the single dispatch pipeline: one FIFO mailbox
  per route consumed by `instances` worker tasks, and
- the **minimalist utilities** shared with the engines for consistency: configuration
  management, logging in the engines' presentation format, and distributed-trace context, and
- the engines' **REST automation** (opt-in, `rest.automation=true`): `rest.yaml` as the router
  in front of your functions - a URL and its methods mapped to a function, with CORS, header
  transforms, an authentication function, a timeout, tracing and static content - so a utility
  application in Python is built and deployed as an engine application is, and
- the **OpenTelemetry forwarder** (opt-in, `otel.forwarding=true`): the host's trace spans
  exported over OTLP/HTTP to Dynatrace, Splunk or a collector — no SDK, no new dependency.

Orchestration deliberately stays in the engines. Functions written here are addressed by
route name through the engines' declarative `yaml.event.over.http` map, so a flow or a
graph task calls a Python function exactly as if it were local.

**Documentation:** <https://accenture.github.io/mercury-python/> — including the
[AI Agent Guide](https://accenture.github.io/mercury-python/guides/ai-agent-guide/)
for deterministic function generation.

> **Status:** v4.12.30 is on PyPI — `pip install mercury-composable`. This repository was
> repurposed in August 2026 for the polyglot initiative. The legacy Mercury language-pack
> implementation remains in tagged releases.

## Quick start

```python
# app.py
from mercury_composable import AppException, Body, platform, preload

@preload(route="hello.python", instances=10)
def handle_event(headers: dict[str, str], body: Body):
    if not isinstance(body, dict) or "text" not in body:
        raise AppException(400, "missing 'text'")
    return {"text": str(body["text"]).upper(), "language": "python"}

if __name__ == "__main__":
    platform.run()   # port from rest.server.port (default 8085)
```

Run it:

```bash
pip install mercury-composable
mercury-serve app.py -Drest.server.port=8086
```

Call it from a Mercury engine application with two configuration entries and no code —
`application.properties`:

```properties
yaml.event.over.http=classpath:/event-over-http.yaml
```

`event-over-http.yaml`:

```yaml
event.http:
  - route: 'hello.python'
    target: 'http://127.0.0.1:8086/api/event'
```

Any Event Script task or MiniGraph `graph.task` node that names the route `hello.python`
now executes the Python function, with trace context carried end to end.

## The function contract

A handler receives the same two-part input as an engine `TypedLambdaFunction` —
`(headers: dict[str, str], body: Body)` — `Body` is any MsgPack value — and returns the reply body (or an `EventEnvelope` for full
control of status and reply headers). **Both `async def` and plain `def` handlers are
first-class**, because Python has two library ecosystems and a polyglot function must be
able to wrap either:

- **plain `def`** — for the synchronous world: `requests` (one of the most popular HTTP
  clients), NumPy/pandas and most ML inference stacks, database drivers. These handlers
  run in a thread-pool executor, so a blocking call can never stall the event loop that
  hosts every other function. This is the Python analog of the Java engine's virtual
  threads: write sequential blocking-style code and the platform makes it safe.
- **`async def`** — for asyncio-native I/O and for composing sibling functions with
  `await po.request(...)`.

The style is detected automatically (no flag to set), and trace context, `instances`,
envelopes and telemetry behave identically in both. Rule of thumb: wrapping a blocking
library → plain `def`; composing functions or async I/O → `async def`.

- Raise `AppException(status, message)` for intentional errors — it becomes the portable
  error contract on the wire (envelope status + message), handled by the calling flow's
  exception handler or the graph's `error.*` contract.
- `get_trace()` exposes `trace_id` / `trace_path` / `cid`; `annotate_trace(k, v)` sends an
  annotation back on the reply envelope.
- Outside a hosted function (batch jobs, tests), `trace_context(trace_id, trace_path)`
  establishes the context your `PostOffice` calls inherit — the node `runWithTrace` twin.
- **Route aliases**: `route` takes one name or a comma-separated list
  (`@preload(route="hello.declarative, hello.world")`), the engines' `@PreLoad` semantics —
  every name is the same function with the same `instances` and visibility, and a handler
  reads the name it was called as from `get_trace().route`.
- Functions must be stateless; anything you must keep belongs to the caller's flow model
  or state machine.

### Local function calls

`PostOffice` **without an endpoint** delivers through this application's own event bus —
the engines' semantics for an in-app `po` call:

- `private=True` means exactly what it means in the engines: callable **in-app only**.
  Local calls reach private and public routes alike; the HTTP host keeps answering 403
  for private targets from the wire.
- `instances` is faithful: each route has one FIFO mailbox consumed by that many worker
  tasks. RPC waits are bounded by `timeout_ms` (the standard 408 envelope on breach), and
  a queued call whose caller already timed out is skipped, never wastefully executed.
- **Sync handlers compose too**: `po.request_sync(...)` / `po.send_sync(...)` run the
  same call on the host loop while blocking only the handler's own worker thread — the
  trace chain rides across the bridge unbroken. Calling the sync bridge from async code
  is refused with a teaching error (`await po.request(...)` is the async way), and using
  it outside a hosted function tells you to use `asyncio.run(po.request(...))` instead.
- There is **no spill tier and no queue cap** by design: back-pressure belongs to the tier
  that owns recovery — the engines' flows and graphs. A leaf host fails fast by deadline
  instead of hoarding work.

Local eventing is for simple leaf-side composition. Workflow processing belongs in Event
Script and Knowledge Graph on the engines — that boundary is the architecture.

## Configuration, logging, telemetry

The same conventions as the engines, so a polyglot installation stays uniform:

| Key | Meaning | Default |
|-----|---------|---------|
| `application.name` | application identity in logs | `application` |
| `rest.server.port` | Event API port | `8085` |
| `log.format` | `text`, `json` (pretty-printed) or `compact` (single-line JSONL) | `text` |
| `log.level` | log level (`LOG_LEVEL` env var wins) | `INFO` |
| `static.html.folder` | the folder of static web content the host serves (HTML, CSS, JS, images), relative to the application file | `resources/public` |

Configuration lives in the `resources` folder next to the application file, mirroring the
engines' classpath resources: `resources/application.yml` (or `.yaml` / `.properties`) beside
the file `mercury-serve` is given, or beside the script `python app.py` ran — never the working
directory, so the configuration does not depend on where the app is launched from — or an
explicit `--config` path; see
[`examples/demo-app/resources/application.yml`](examples/demo-app/resources/application.yml) for a worked
sample. Values support `${ENV_VAR:default}` substitution. Runtime parameter overrides use the
same `-D` syntax as the Java engine and the Rust port — checked first on every read, their values
resolved with the same substitution, so `-Dkey='${ENV_VAR}'` keeps a credential out of the process
list (`AppConfig.set(key, value)` does the same programmatically, the `f:setConfig` analog):

```bash
mercury-serve app.py -Drest.server.port=8086 -Dlog.format=json
```

Log lines follow the Java reference engine's pattern for one-aggregation consistency:

```text
2026-08-22 10:15:30.123 INFO  my_app:42 - Loaded PUBLIC hello.python, instances=10
```

## Actuator endpoints

The host serves the engines' operational endpoints on the same port as `/api/event`, so
Kubernetes probes and dashboards treat a Python app exactly like a Java or Rust engine app:

| Endpoint | Purpose |
|----------|---------|
| `GET /` | the static folder's `index.html`, else a minimal index page linking the endpoints below |
| `GET /info` | app identity, runtime, origin id, start time, uptime |
| `GET /info/routes` | registered routes split by visibility, with instance counts |
| `GET /env` | selected environment variables and configuration parameters |
| `GET /health` | dependency health checks — `UP` (HTTP 200) or `DOWN` (HTTP 400) |
| `GET /livenessprobe` | `OK` while the last health outcome was good, else HTTP 400 |

Configuration keys carry the engines' names: `info.app.version`, `info.app.description`,
`show.env.variables` and `show.application.properties` (opt-in lists — secrets are never
dumped wholesale), and `mandatory.health.dependencies` / `optional.health.dependencies`
(routes of health check functions; optional ones never change the overall status). A
health check function is a normal registered function — usually private — speaking the
engines' interface contract, called through the event bus:

```python
@preload("demo.health", private=True)
async def health(headers: dict[str, str], _body: Body) -> Body:
    if headers.get("type") == "info":
        return {"service": "demo.service", "href": "http://127.0.0.1"}
    return "demo.service is running fine"   # a non-200 reply marks it down
```

JSON responses are pretty-printed — the engines' default-serializer presentation — and
unknown paths answer the engines' error shape
(`{"status": 404, "message": "Resource not found", "type": "error"}`).

Kubernetes wiring: point `livenessProbe` at `/livenessprobe` and `readinessProbe` at
`/health`.

## Static content

The host serves the files of `static.html.folder` — the Java engine's key, with the Rust engine's
location as its default, `resources/public` next to the application file — for any `GET` or `HEAD`
that no host path, extension route or function claims, so an application can ship a web front-end
beside its functions the way the engines' Playground does. A folder is served as its `index.html`,
so `resources/public/index.html` replaces the landing page at `/`. The content type follows the
engines' table by extension (`text/javascript`, `text/css`, `image/svg+xml`, fonts and images;
`application/octet-stream` otherwise); every file carries an ETag and answers `If-None-Match` with
304, and HTML is served `Cache-Control: no-cache`, so a redeployed page is revalidated while hashed
assets stay cached. A hidden entry, a `..` segment, a symbolic link that leaves the folder or
anything that is not a regular file is not found, in the engines' error shape. A relative value of
the key is under the application file's folder, never the working directory; `file:` prefixes an
absolute path as in the Java engine; an empty value turns the service off.

## REST automation

With `rest.automation: true` the host also serves the entries of `rest.yaml` — the engines' REST
automation, where the file *is* the router: an entry maps a URL and its methods to a function, with
CORS, header transforms, an authentication function, a timeout, tracing and static content, so a
utility application in Python is designed and deployed as an engine application is. The file is
read from `yaml.rest.automation` (default `classpath:/rest.yaml`, the `resources/rest.yaml` next to
the application file; `file:` prefixes an absolute path) in the engines' grammar — the
[REST automation guide](https://accenture.github.io/mercury-composable/guides/rest-automation/) and
its [grammar](https://accenture.github.io/mercury-composable/guides/rest-automation/rest-grammar/)
apply unchanged, and a misconfigured entry fails the start with the engines' message.

```yaml
rest:
  - service: "profile.lookup"
    methods: ['GET']
    url: "/api/profile/{id}"
    timeout: 10s
    cors: cors_1
    headers: header_1
    authentication: "v1.api.auth"
    tracing: true
cors:
  - id: cors_1
    options:
      - "Access-Control-Allow-Origin: ${api.origin:*}"
      - "Access-Control-Allow-Methods: GET, OPTIONS"
    headers:
      - "Access-Control-Allow-Origin: ${api.origin:*}"
headers:
  - id: header_1
    response:
      add: ["Cache-Control: no-cache, no-store"]
```

The function receives the whole request as its body — the engines' `AsyncHttpRequest` map, never
the bare HTTP body — and reads it with the engines' accessors; a returned `EventEnvelope` sets the
status and headers, a raised `AppException` is the error:

```python
from mercury_composable import AppException, AsyncHttpRequest, Body, EventEnvelope, preload

@preload(route="profile.lookup", instances=10)
async def lookup(headers: dict[str, str], body: Body):
    request = AsyncHttpRequest(body)
    profile_id = request.path_parameter("id")     # from url: /api/profile/{id}
    if profile_id == "0":
        raise AppException(404, "no such profile")  # the standard error body on the wire
    return EventEnvelope().set_header("x-source", "demo").set_body({"profile_id": profile_id})
```

What the edge does, as the engines do it: the request body is parsed by its declared content type
(`application/json` to a map or list, the text types to text, a form's fields into the query
parameters, anything else to bytes); repeated query parameters become a list and cookies a map;
the response content type is the function's or negotiated from `Accept` (`text/html` wraps a map
in an HTML shell; no `Accept` means no content type); a failure renders as the standard error body
`{"status", "message", "type": "error"}`; the correlation id (`X-Correlation-Id`, or
`http.correlation.id.header`) is always ensured, delivered as the read-only `my_correlation_id`
header and echoed on the response; with `tracing: true` the edge starts a trace from a W3C
`traceparent`, else the trace-id header (`http.trace.id.header`), else a fresh id, and leaves the
`http.request` record the engines leave, so the OpenTelemetry forwarder exports the request as a
SERVER span. The entry's `timeout` (1 s to 5 min, 30 s by default) rides the request as `x-ttl` and
answers 408 when it expires; an inbound `x-ttl` wins. `OPTIONS` is answered from the `options` lines
of the entry's CORS block (405 without one); a known path under another method is 405, an unknown
path 404. `stream: true` renders an interceptor's segments progressively — Server-Sent Events for a
`text/event-stream` head or `Accept`, chunked text or JSON lines otherwise. `POST /api/event` and
the actuators are the engines' default entries, so a user entry for `/api/event` can attach
`authentication`, CORS or a timeout to it; an entry whose function is not registered at the end of
the boot is skipped with a warning, never a live URL. The `static-content` block applies to the
static folder: the `no-cache-pages` (default `/` and `/index.html`) are served
`Cache-Control: no-cache, no-store`, and the `filter` function sees every matching static request
first — its headers ride the response, and a status other than 200 (a 302 to a sign-in page)
passes through.

Precedence: a `rest.yaml` entry, then an extension's mounted routes, then the static folder, then
the engines' error shape. Refused by name at load, so the grammar shows the scope fence: a `flow`
binding (`flow:` or `service: http.flow.adapter` — flows run in the engines), the HTTP relay (an
`http(s)://` service) and the A/B dual service. `upload: true` on a `POST` entry is the pack's bounded
form of the engines' streaming upload: the files of a `multipart/form-data` body arrive in the request
map under the engines' keys (`filename`, `file_type`, `file_length`, the `upload` tags, `size`) with
their bytes in `contents`, read with `AsyncHttpRequest.files()`, all together within
`rest.upload.max.bytes` (16 MiB; 413 beyond it); the form's text fields become query parameters.
Deferred as in the Rust engine: `custom.content.types`, `mime.types`, XML and request-body
streaming. With the switch off, the default, the host is exactly what it was.

## Wire compatibility

The codec implements the
[Event Envelope Wire Format](https://accenture.github.io/mercury-composable/guides/event-envelope-wire-format/)
(standard format) and is verified against the golden conformance vectors shared by the
Java and Rust engines (`tests/vectors/vectors.json`). The classic compact format is
detected and rejected with a teaching error — engines default to the standard format for
Event over HTTP.

Serialization notes: integers and floats follow MsgPack's natural widths (the same
long/integer care as the engines applies); timestamps travel as ISO-8601 UTC strings with
millisecond precision; binary payloads use MsgPack `bin`.

## Scope

This package intentionally contains **no orchestration: no flows, no graphs, no
persistence, no pub/sub broadcast** — those live in the engines. What it does carry is
deliberately minimal: functions, a primitive in-process event bus (route mailboxes +
workers, RPC and drop-n-forget — nothing more), the engines' REST automation edge (an opt-in
`rest.yaml` — ingress, never orchestration), and the minimalist foundation utilities,
keeping Python fast to prototype with while the composable core guarantees the
architecture.

A function always runs in this process, never in a subprocess or an embedded interpreter.
The one subprocess the pack may start is an **MCP server reached over stdio**: the pack runs it
as a child process and talks to it over its standard input and output. That is why the MCP
clients of the engines' agent loop (RFC-0011) live in the language packs: no engine starts a
child process. The MCP binding in `extensions/mcp-binding/` is that client: one composable route per
allowlisted remote tool, over stdio or streamable HTTP, on the official MCP SDK.

The LLM helper in `extensions/llm-helper/` is an application on this package, not part of it.
Its `llm.chat` is configurable: a stateless chat, or one turn of a multi-turn tool-use loop whose
transcript lives in the store the helper is configured with. The built-in agent tools in
`extensions/agent-tools/` are the essential tools such a loop names in its catalog - files inside one
workspace, the web inside an allowlist of sites, a clock - each a composable function whose sandbox is
deployment configuration, never an argument of a call.
The ask-a-person main flow in `extensions/ask-person/` carries such a loop's wait for a person to the
installation's channels and brings the decision back through a resume link, knowing no channel and keeping
no store; the agent-question app, a store-and-forward channel for an occasionally connected device, is a
separate application on the same package.

## Development

```bash
uv venv .venv && uv pip install -e '.[dev]'   # environment (uv-managed python)
.venv/bin/pytest -q                           # tests
uvx ruff check .                              # lint (config in pyproject.toml)
uvx basedpyright                              # type check (config in pyproject.toml)
```

PyCharm: use interpreter type **uv** pointing at the project `.venv`, and set
*Settings → Tools → Python Integrated Tools → Package requirements file* to
`pyproject.toml` so the requirements inspection reads `[project.dependencies]`.

## License

Apache 2.0 — see [LICENSE.txt](LICENSE.txt).
