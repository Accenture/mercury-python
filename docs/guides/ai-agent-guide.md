---
title: AI Agent Guide
summary: The complete authoring grammar for Python polyglot functions on one page -
  contract, registration, config keys, endpoints and error rules, for deterministic generation.
audience: [ai-agent]
keywords: [ai agent, grammar, contract, deterministic, preload, postoffice]
---

# AI Agent Guide

**Purpose: generate a correct Python polyglot function from this page alone.**
Humans: the narrative versions live in [Function Writing Patterns](function-patterns.md)
and the [join chapters](join-event-script.md). Orchestration (flows, graphs) is
authored on the engine — use the
[engine AI guides](https://accenture.github.io/mercury-composable/guides/ai-developer-guide/).

## Pre-write checklist

1. Route name: lowercase `[a-z0-9._-]`, at least one period. Example: `order.enrich`.
2. Handler style: wraps blocking library (`requests`, NumPy, DB driver) → plain `def`;
   asyncio I/O or calls sibling functions → `async def`.
3. Function is stateless. State belongs to the calling flow/graph.
4. Orchestration-shaped logic (sequencing, retries, branching) → STOP; author an
   Event Script flow or MiniGraph graph on the engine instead.

## The contract

```python
from mercury_composable import AppException, Body, PostOffice, annotate_trace, \
    get_logger, get_trace, platform, preload

log = get_logger(__name__)

@preload(route="order.enrich", instances=10)            # private=True -> in-app only
def handler(headers: dict[str, str], body: Body):       # or: async def
    # 1. validate; intentional errors = AppException(status, message)
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise AppException(400, "missing 'id'")
    # 2. work (blocking is safe in plain def - executor thread)
    # 3. optional telemetry
    annotate_trace("source", "python")                  # rides back on the reply
    # 4. return the reply body (or an EventEnvelope for status/header control)
    return {"id": body["id"], "enriched": True}

if __name__ == "__main__":
    platform.run()
```

Rules:

- `headers: dict[str, str]`; `body: Body` = `None|bool|int|float|str|bytes|list|dict`.
- Return value = reply body. Return `EventEnvelope` only when setting status/headers.
- `raise AppException(status, message)` → envelope status + message (portable error).
  Unexpected exception → 500 + message + stack. Never return HTTP-shaped dicts.
- `get_trace()` → `TraceInfo(trace_id, trace_path, cid)` or `None`.
- Reserved inbound header `my_correlation_id` = the caller's business correlation id
  (read-only). Never send headers named `my_*` or `x-event-api`.

## Composition (calling other functions)

```python
# async handler:
reply = await PostOffice().request("other.route", body={...}, timeout_ms=5000)
# plain-def handler (sync bridge; blocks only this worker thread):
reply = PostOffice().request_sync("other.route", body={...}, timeout_ms=5000)
# drop-n-forget twins: send / send_sync -> 202 ack envelope
# remote peer or engine:
async with PostOffice(endpoint="http://host:8085/api/event") as po: ...
```

- `request_sync` on the event loop → RuntimeError (use `await request()`).
- `request_sync` outside a hosted function → RuntimeError (use `asyncio.run(...)`).
- Always check `reply.get_status()`; errors are envelopes, not exceptions.
- Local calls reach `private=True` routes; the wire cannot (403).

## Run + configure

```bash
mercury-serve app.py                      # config: resources/application.yml
mercury-serve app.py -Dkey=value          # runtime override (engine syntax)
```

Well-known keys (full table: [Configuration Reference](configuration-reference.md)):
`application.name`, `rest.server.port` (default 8085), `log.format`
(text|json|compact), `log.level`, `info.app.version`, `info.app.description`,
`show.env.variables`, `show.application.properties`,
`mandatory.health.dependencies`, `optional.health.dependencies`.

## Health check function (engine interface contract)

```python
@preload(route="my.health", instances=5, private=True)
async def health(headers: dict[str, str], _body: Body):
    if headers.get("type") == "info":
        return {"service": "my.dependency", "href": "http://backend"}
    return "my.dependency is running fine"      # non-200 reply marks it DOWN
```

List the route in `mandatory.health.dependencies` (or `optional.…`).

## HTTP surface (served by the host, no code needed)

`POST /api/event` (envelope wire) · `GET /` `/info` `/info/routes` `/env` `/health`
`/livenessprobe`. Shapes: [HTTP Surface Reference](http-surface-reference.md).

## Engine-side wiring (for completeness; authored on the engine)

```yaml
# application.properties:  yaml.event.over.http=classpath:/event-over-http.yaml
event.http:
  - route: 'order.enrich'
    target: 'http://python-host:8086/api/event'
```

Flow task `process: 'order.enrich'` or graph node
`{"skill": "graph.task", "task": "order.enrich", ...}` (engines ≥ v4.11.11 for
graph.task). Details: [Join an Event Script Flow](join-event-script.md) ·
[Join a Knowledge Graph](join-knowledge-graph.md).

## DO / DON'T

| DO | DON'T |
|----|-------|
| plain `def` for blocking libraries | block inside `async def` |
| `AppException` for intentional errors | return `{"status": 400, ...}` dicts |
| keep functions stateless | cache business state in module globals |
| compose one or two leaf helpers | re-implement flows/retries in Python |
| let deadlines fail fast (408 envelope) | swallow timeouts and hoard work |
