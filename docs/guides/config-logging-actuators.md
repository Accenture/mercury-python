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
