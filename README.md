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
- the **minimalist utilities** shared with the engines for consistency: configuration
  management, logging in the engines' presentation format, and distributed-trace context.

Orchestration deliberately stays in the engines. Functions written here are addressed by
route name through the engines' declarative `yaml.event.over.http` map, so a flow or a
graph task calls a Python function exactly as if it were local.

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
mercury-serve app.py --port 8086
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
control of status and reply headers). `async def` and plain `def` are both supported;
synchronous handlers run in a thread-pool executor so the event loop never blocks.

- Raise `AppException(status, message)` for intentional errors — it becomes the portable
  error contract on the wire (envelope status + message), handled by the calling flow's
  exception handler or the graph's `error.*` contract.
- `get_trace()` exposes `trace_id` / `trace_path` / `cid`; `annotate_trace(k, v)` sends an
  annotation back on the reply envelope.
- Functions must be stateless; anything you must keep belongs to the caller's flow model
  or state machine.

## Configuration, logging, telemetry

The same conventions as the engines, so a polyglot installation stays uniform:

| Key | Meaning | Default |
|-----|---------|---------|
| `application.name` | application identity in logs | `application` |
| `rest.server.port` | Event API port | `8085` |
| `log.format` | `text` or `json` | `text` |
| `log.level` | log level (`LOG_LEVEL` env var wins) | `INFO` |

Configuration lives in the `resources` folder, mirroring the engines:
`resources/application.yml` (or `.yaml` / `.properties`), or an explicit `--config` path.
Values support `${ENV_VAR:default}` substitution. Runtime parameter overrides use the
same `-D` syntax as the Java engine and the Rust port — checked first on every read
(`AppConfig.set(key, value)` does the same programmatically, the `f:setConfig` analog):

```bash
mercury-serve app.py -Drest.server.port=8086 -Dlog.format=json
```

Log lines follow the Java reference engine's pattern for one-aggregation consistency:

```text
2026-08-22 10:15:30.123 INFO  my_app:42 - Loaded PUBLIC hello.python, instances=10
```

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

This package intentionally contains **no event bus, no flows, no graphs and no
orchestration** — those live in the engines. It provides functions plus the minimalist
foundation utilities, keeping Python fast to prototype with while the composable core
guarantees the architecture.

## License

Apache 2.0 — see [LICENSE.txt](LICENSE.txt).
