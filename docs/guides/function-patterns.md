---
title: Function Writing Patterns
summary: The coding patterns for externalized functions - the handler contract, sync vs
  async, errors, trace context, private functions and composition.
audience: [developer, ai-agent]
keywords: [preload, handler, sync, async, AppException, trace, private, request_sync]
---

# Function Writing Patterns

*Write functions: the day-to-day patterns, with the reasons attached.*

> **At a glance**
>
> - **What** — the `(headers, body)` contract, both handler styles, the portable error
>   contract, trace context, private functions, and composition through PostOffice.
> - **Rule of thumb** — wrapping a blocking library → plain `def`; composing functions
>   or async I/O → `async def`.

## The contract

A function is a handler registered under a route name:

```python
from mercury_composable import Body, preload

@preload(route="my.function", instances=10)
def handler(headers: dict[str, str], body: Body):
    return {"ok": True}
```

- **Input** — the same two-part input as an engine `TypedLambdaFunction`:
  `headers: dict[str, str]` and `body: Body` (any MsgPack value: `None | bool | int |
  float | str | bytes | list | dict`).
- **Output** — return the reply body, or an `EventEnvelope` for full control of status
  and reply headers.
- **Route names** — lowercase letters, digits, period, hyphen, underscore, with at
  least one period (`hello.python`, not `HelloPython`).
- **Statelessness** — anything a handler must keep belongs to the caller's flow model
  or graph state machine, never to module globals.

## Sync or async — both are first-class

Python has two library ecosystems, and a polyglot function must be able to wrap
either:

=== "plain def — the blocking world"

    ```python
    import requests   # or NumPy, pandas, an ML runtime, a DB driver

    @preload(route="quote.fetch", instances=10)
    def fetch_quote(_headers: dict[str, str], body: Body):
        assert isinstance(body, dict)
        response = requests.get(body["url"], timeout=5)   # blocking is SAFE here
        return {"status": response.status_code, "text": response.text[:200]}
    ```

    Plain `def` handlers run in a thread-pool executor, so a blocking call can never
    stall the event loop that hosts every other function. This is the Python analog of
    the Java engine's virtual threads.

=== "async def — the asyncio world"

    ```python
    from mercury_composable import PostOffice

    @preload(route="hello.chain", instances=10)
    async def chain(_headers: dict[str, str], body: Body):
        reply = await PostOffice().request("demo.suffix.helper", body=body,
                                           timeout_ms=5000)
        return reply.body
    ```

    `async def` handlers run on the event loop — the natural fit for asyncio-native
    I/O and for composing sibling functions.

Detection is automatic (`inspect.iscoroutinefunction`); trace context, `instances`,
envelopes and telemetry behave identically in both styles.

## Errors — one portable contract

Raise `AppException(status, message)` for intentional errors:

```python
from mercury_composable import AppException

raise AppException(400, "missing 'text'")
```

On the wire this becomes a normal envelope with status 400 and the message as body —
the flow's exception handler or the graph's `error.*` contract receives it exactly as
it would from an engine function. An unexpected exception becomes status 500 with the
message and a stack trace, mirroring the engines. Handler-level errors always ride
HTTP 200; only transport-level failures (unknown route, private target, timeout,
undecodable envelope) surface as HTTP status codes.

## Trace context and span lineage

Every delivery runs under its caller's trace, and every traced execution mints
its own **span** with the caller's span as its parent - the engines' exact
OpenTelemetry lineage model, so a chain like *user → engine flow → wrapper
function (agent, MCP tool) → engine* stays one connected trace tree:

```python
from mercury_composable import annotate_trace, get_trace

info = get_trace()            # trace_id, trace_path, cid, my_correlation_id,
                              # span_id, parent_span_id - or None
annotate_trace("model", "v3") # rides back on the reply envelope AND the trace record
```

Outbound calls carry the current span (the receiver's parent), the business
correlation-id (`my_cid` tag), and a W3C `traceparent` header when the trace id
is W3C-shaped. Non-RPC executions emit the engines' distributed-trace dataset
on the `distributed.tracing` log stream - the same
`{"trace": {...}, "annotations": {...}}` record the Java engine logs - so a
stdout log-ingest agent (Dynatrace-style) or any log aggregation stitches the
span tree across all four runtimes. RPC round-trips fold into the caller's
view, exactly like the engines.

**Application log context**: with `log.format` json/compact, every log line a
function writes inside a traced request carries a `context` block (the
engines' app-log-context feature, on by default) - the standard trace context
(`cid` = the business correlation-id, `traceId`, `tracePath`, `spanId`,
`parentSpanId`, `service`, `timestamp`) - so application logs and the
distributed-trace records correlate in one aggregation. Customize with your
own `resources/app-log-context.yaml` (`context:` section mapping output keys
to reserved `$tokens` or constants, `${ENV:default}` supported), opt out with
`app.log.context=false`, and add per-request key-values from a handler with
`update_context("tenant", "acme")` (a logging-only sink; reserved keys are
guarded; `None` removes).

Outside a hosted function (batch jobs, tests), establish context explicitly -
including an external OpenTelemetry span to parent onto:

```python
from mercury_composable import trace_context

with trace_context("4bf92f3577b34da6a3ce929d0e0e4736", "BATCH /nightly",
                   cid="order-42", span_id="00f067aa0ba902b7"):
    reply = await po.request("my.function", body={...})
```

## Private functions and composition

`private=True` marks a function callable **in-app only** — the HTTP host answers 403
for it, while a local `PostOffice` (no endpoint) reaches it through the bus:

```python
@preload(route="demo.suffix.helper", instances=10, private=True)
async def suffix_helper(_headers: dict[str, str], body: Body): ...

# async composition
reply = await PostOffice().request("demo.suffix.helper", body=body, timeout_ms=5000)

# sync composition (from a plain-def handler): blocks this worker thread only
reply = PostOffice().request_sync("demo.suffix.helper", body=body, timeout_ms=5000)
```

The sync bridge refuses misuse with teaching errors: on the event loop it says
*await request() instead*; outside a hosted function it points at
`asyncio.run(po.request(...))`. The trace chain rides across the bridge unbroken.

!!! warning "Composition is for leaf-side helpers"
    A public function calling a private formatter is healthy. A function that
    sequences three other functions with retries is a flow wearing a disguise —
    write it as Event Script or a graph instead ([Rationale](rationale.md)).

## Calling remote peers

The same PostOffice, given an endpoint, calls any engine or peer host with the
engines' relay contract (octet-stream envelope, `x-ttl`, trace headers):

```python
async with PostOffice(endpoint="http://peer:8085/api/event") as po:
    reply = await po.request("hello.node", body={"text": "hi"}, timeout_ms=5000)
```

The reply envelope is authoritative in every mode: inspect `reply.get_status()`.
