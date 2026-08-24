---
title: Join an Event Script Flow
summary: Wire a Python function into a flow - the declarative map, the task that names it,
  data mapping from the function's seat, and the exception path end to end.
audience: [developer]
keywords: [event script, flow, yaml.event.over.http, declarative, task, exception handler]
---

# Join an Event Script Flow

*Join the engines: your function as a first-class flow task.*

> **At a glance**
>
> - **What** — one YAML entry on the engine makes a route remote; the flow itself does
>   not change at all.
> - **Worked demo** — the engine's `composable-example` ships this exact wiring and
>   has executed the Python demo function unchanged.

## The one moving part: the declarative map

On the engine application, enable the map and point the route at your host —
`application.properties`:

```properties
yaml.event.over.http=classpath:/event-over-http.yaml
```

`event-over-http.yaml`:

```yaml
event.http:
  - route: 'hello.declarative'
    target: 'http://${peer.demo.host:127.0.0.1}:${peer.demo.port}/api/event'
    # optional security headers, e.g. an authorization token the host or a
    # gateway validates:
    # headers:
    #   authorization: '${DEMO_PEER_TOKEN:demo}'
```

That is the entire integration surface. Every Event Script task (and MiniGraph
`graph.task`) that names `hello.declarative` now calls your Python host. The full map
grammar lives in the engine's
[Event over HTTP guide](https://accenture.github.io/mercury-composable/guides/event-over-http/).

## The flow does not know, and must not care

This is the engine's shipped demo flow — note that nothing in it says "remote" or
"python":

```yaml
flow:
  id: 'event-over-http-declarative'
  description: 'Demonstrate Event-over-Http protocol using declarative means'
  ttl: 10s
  exception: 'v1.hello.exception'

first.task: 'event-over-http-declarative'

tasks:
  - name: 'event-over-http-declarative'
    input:
      - 'input.header -> header'
      - 'input.body -> *'
    process: 'hello.declarative'
    output:
      - 'text(application/json) -> output.header.content-type'
      - 'result -> output.body'
    execution: end
```

Register the route on the Python side and the flow executes it:

```python
@preload(route="hello.declarative", instances=10)
async def declarative_echo(headers: dict[str, str], body: Body):
    return {"body": body, "headers": headers, "language": "python"}
```

## What your function sees

```mermaid
sequenceDiagram
    participant C as REST client
    participant E as Engine (flow)
    participant H as Python host
    participant F as hello.declarative
    C->>E: GET /api/event/http/declarative
    E->>H: POST /api/event (envelope bytes, x-ttl, trace headers)
    H->>F: (headers, body) on the bus
    F-->>H: reply body
    H-->>E: reply envelope (status, exec_time, annotations)
    E-->>C: flow output mapping
```

- **Headers** — the task's `input.header -> header` mapping arrives as your
  `headers` dict. Reserved keys are cleaned at ingress, and the flow's business
  correlation id arrives as the read-only `my_correlation_id` header.
- **Body** — whatever the task's input mapping sends (`input.body -> *` passes the
  whole body through).
- **Trace** — the engine's trace id and path ride the wire; `get_trace()` sees them,
  and `annotate_trace()` entries return on the reply envelope into the engine's
  telemetry.

## Errors flow into the flow

- `AppException(400, "missing 'text'")` in Python → a 400 envelope → the flow's
  `exception:` task fires with `error.code=400` and `error.message` exactly as for an
  engine function.
- An unexpected Python exception → 500 with message and stack.
- The flow's `ttl` bounds the call: on breach the engine receives the standard 408,
  and the exception path decides what happens next — retries and compensation stay in
  the flow, never in Python ([Rationale](rationale.md)).

## Checklist

1. Host running and healthy (`/livenessprobe` → `OK`).
2. Route registered (`/info/routes` lists it as `public`).
3. Engine `application.properties` names the map; the map names the route and target.
4. The flow task's `process:` names the route. Nothing else changes.
