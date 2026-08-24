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
  management, logging in the engines' presentation format, and distributed-trace context.

Orchestration deliberately stays in the engines. Functions written here are addressed by
route name through the engines' declarative `yaml.event.over.http` map, so a flow or a
graph task calls a Python function exactly as if it were local.

**Documentation:** <https://accenture.github.io/mercury-python/> — including the
[AI Agent Guide](https://accenture.github.io/mercury-python/guides/ai-agent-guide/)
for deterministic function generation.

> **Status: pre-release.** This repository was repurposed in August 2026 for the polyglot
> initiative. The legacy Mercury language-pack implementation remains available in the git
> history.

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
pip install -e '.[dev]'
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

Configuration lives in the `resources` folder, mirroring the engines:
`resources/application.yml` (or `.yaml` / `.properties`) in the working directory or next
to the application file, or an explicit `--config` path — see
[`examples/resources/application.yml`](examples/resources/application.yml) for a worked
sample. Values support `${ENV_VAR:default}` substitution. Runtime parameter overrides use the
same `-D` syntax as the Java engine and the Rust port — checked first on every read
(`AppConfig.set(key, value)` does the same programmatically, the `f:setConfig` analog):

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
| `GET /` | minimal index page linking the endpoints below |
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
workers, RPC and drop-n-forget — nothing more), and the minimalist foundation utilities,
keeping Python fast to prototype with while the composable core guarantees the
architecture.

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
