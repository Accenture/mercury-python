---
title: Configuration, Logging & Actuators
summary: The engines' operational conventions in Python - resources folder, -D overrides,
  three log formats, actuator endpoints and Kubernetes probes.
audience: [developer, operator]
keywords: [configuration, resources, log format, actuator, health, kubernetes, livenessprobe]
---

# Configuration, Logging & Actuators

*Write functions: run them the way engine apps run.*

> **At a glance**
>
> - **What** — one configuration style, one log presentation, one operational surface
>   across Java, Rust, Python and Node.js apps.
> - **For** developers wiring an app and operators monitoring a polyglot estate.

## Configuration — the engines' conventions

Configuration lives in the `resources` folder (`resources/application.yml`, `.yaml`
or `.properties`), in the working directory or next to the application file. Values
support `${ENV_VAR:default}` substitution; `-Dkey=value` command-line arguments are
runtime overrides checked first on every read — the same syntax as the Java engine's
JVM system properties and the Rust port's `-D` arguments:

```bash
mercury-serve app.py -Drest.server.port=8090 -Dlog.format=compact
```

See the worked sample
[`examples/resources/application.yml`](https://github.com/Accenture/mercury-python/blob/main/examples/resources/application.yml)
and the full key table in the [Configuration Reference](configuration-reference.md).

## Logging — one aggregation, three presentations

Log lines follow the Java reference engine's pattern, so a polyglot installation reads
one way in the aggregator:

```text
2026-08-24 10:15:30.123 INFO  my_app:42 - Loaded PUBLIC hello.python, instances=10
```

`log.format` carries the engines' three presentations: `text` (default), `json`
(pretty-printed) and `compact` (single-line JSONL for log aggregators). The level
comes from the `LOG_LEVEL` environment variable when set, else `log.level`.

## Distributed tracing — the OpenTelemetry forwarder (opt-in)

Every traced, non-RPC execution emits the engines' distributed-trace dataset on the
`distributed.tracing` log stream — `{"trace": {...}, "annotations": {...}}`, the record the
Java engine logs — so a log aggregation already stitches the span tree across all four
runtimes. The forwarder is the second sink, for backends that take spans directly:
Dynatrace, Splunk, an OpenTelemetry Collector.

```yaml
otel.forwarding: true
otel.exporter.otlp.endpoint: 'https://<tenant>.live.dynatrace.com/api/v2/otlp/v1/traces'
otel.exporter.otlp.headers: 'Authorization=Api-Token ${DT_TOKEN}'
otel.service.name: 'my-python-functions'
```

With `otel.forwarding=true` (the switch is the only thing that turns it on — `false` by
default, and `-Dotel.forwarding=true` at run time is enough), the host also hands each
dataset to the engines' extension route `distributed.trace.forwarder`, where the built-in
forwarder registers itself at start-up (private, two workers; an application that registers
its own function on that route wins). Each dataset becomes **one OpenTelemetry span carrying
the host's exact W3C trace, span and parent-span ids** — the same mapping as the engines'
forwarders, so one trace spans the engine that called and the function here — and is
exported over OTLP/HTTP as protobuf, one span per request. There is no OpenTelemetry SDK
and no new dependency: the encoder is the engines' own hand-written OTLP writer, ported.

Request headers are re-read on every export, so a credential a start-up bootstrap publishes
with `config.set(...)` after the forwarder started takes effect without a restart; header
**names** appear in the log, never values. A transport failure or a 408/429/502/503/504
answer is retried five times on the OpenTelemetry SDK's backoff (1 s growing by 1.5×);
any other rejection is logged once with the status, the backend's message and a hint
(`HTTP 401 ... | the backend rejected the credential itself - check otel.exporter.otlp.headers`).
The start-up line confirms the configuration:
`OpenTelemetry trace forwarder ready - service=..., OTLP endpoint=..., compression=none, credential headers=[...]`.

Differences from the engines: only `none` compression is honoured (a warning otherwise);
`otel.exporter.otlp.connect.timeout` **is** honoured here (aiohttp's connect timeout); the
instrumentation scope is `mercury-composable-python`, so a backend shows which runtime
produced a span. The keys are listed in the [configuration reference](configuration-reference.md).

## Actuators — the engines' operational surface

The host serves the engines' endpoints on the same port as `/api/event`:

| Endpoint | Purpose |
|----------|---------|
| `GET /` | minimal index page linking the endpoints below |
| `GET /info` | app identity, runtime, origin id, start time, uptime |
| `GET /info/routes` | registered routes split by visibility, with instance counts |
| `GET /env` | selected environment variables and configuration parameters (opt-in lists) |
| `GET /health` | dependency health checks — `UP` (HTTP 200) or `DOWN` (HTTP 400) |
| `GET /livenessprobe` | `OK` while the last health outcome was good, else HTTP 400 |

JSON responses are pretty-printed (the engines' default-serializer presentation) with
`application/json; charset=utf-8`; unknown paths answer the engines' error shape —
see the [HTTP Surface Reference](http-surface-reference.md).

### Health check functions — the engines' interface contract

A health check is a normal registered function (usually private) listed in
`mandatory.health.dependencies` / `optional.health.dependencies`. The actuator calls
it through the event bus, first with header `type=info` (an advisory identity map
merged into its dependency entry), then with `type=health` (a status text or map; a
non-200 reply marks the dependency down):

```python
@preload(route="demo.health", instances=5, private=True)
async def health_check(headers: dict[str, str], _body: Body):
    if headers.get("type") == "info":
        return {"service": "demo.service", "href": "http://127.0.0.1"}
    return "demo.service is running fine"
```

Optional dependencies never change the overall status; mandatory ones decide
`UP`/`DOWN`, and the most recent outcome drives `/livenessprobe`.

## Kubernetes wiring

```yaml
livenessProbe:
  httpGet: { path: /livenessprobe, port: 8086 }
readinessProbe:
  httpGet: { path: /health, port: 8086 }
```

The pod presents exactly like an engine pod — one dashboard shape for the whole
polyglot estate.
