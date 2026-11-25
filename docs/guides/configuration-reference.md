---
title: Configuration Reference
summary: Every well-known configuration key, the resolution order, and the substitution syntax.
audience: [developer, operator, ai-agent]
keywords: [configuration, reference, keys, substitution, overrides]
---

# Configuration Reference

*Reference: the complete key table.*

## Resolution

1. `-Dkey=value` command-line overrides (and programmatic `AppConfig.set`) — checked
   first on every read, the engines' `f:setConfig` analog.
2. The configuration file: `resources/application.yml` | `.yaml` | `.properties` next to
   the application file (the file `mercury-serve` is given, or the script `python app.py`
   ran), never in the working directory; or `--config <path>`.
3. `${ENV_VAR:default}` substitution inside values: environment first, then a base
   configuration key of that name, then the default.

## Well-known keys (shared with the engines)

| Key | Meaning | Default |
|-----|---------|---------|
| `application.name` | application identity in logs, `/info` and `/health` | `application` |
| `rest.server.port` | Event API + actuator port | `8085` |
| `log.format` | `text`, `json` (pretty-printed) or `compact` (single-line JSONL) | `text` |
| `log.level` | log level; the `LOG_LEVEL` environment variable wins | `INFO` |
| `info.app.version` | version reported by `/info` | package version |
| `info.app.description` | description reported by `/info` | `application.name` |
| `show.env.variables` | opt-in list of environment variables shown by `/env` | (empty) |
| `show.application.properties` | opt-in list of configuration keys shown by `/env` | (empty) |
| `mandatory.health.dependencies` | routes of health check functions that decide `/health` | (empty) |
| `optional.health.dependencies` | health check routes reported but never affecting status | (empty) |
| `rest.automation` | REST automation on: the entries of `rest.yaml` served in front of the functions ([guide](rest-automation.md)) | `false` |
| `yaml.rest.automation` | the one location of `rest.yaml`: `classpath:` the resources folder, `file:` an absolute path, a relative path under the application file's folder | `classpath:/rest.yaml` |
| `rest.upload.max.bytes` | the bound on the files of one upload entry, together (413 beyond it) | `16777216` |
| `http.trace.id.header` | the trace-id header a traced entry reads when no W3C `traceparent` arrives (`trace.id.header` overrides it per entry) | `X-Trace-Id` |
| `http.correlation.id.header` | the business correlation-id header, always ensured and echoed (`correlation.id.header` per entry) | `X-Correlation-Id` |
| `http.traceparent.header` | a custom name for the W3C `traceparent` header, read only when the standard one is absent (`traceparent.header` per entry) | `traceparent` |
| `event.stream.keep.alive` | the keep-alive comment interval of a Server-Sent Events reply (`/api/event` and streaming entries); `0` disables | `30s` |
| `static.html.folder` | the folder of static web content the host serves for the paths nothing else claims — relative to the application file's folder ([guide](config-logging-actuators.md#static-content-the-engines-public-folder)) | `resources/public` |
| `otel.forwarding` | the OpenTelemetry forwarder's master switch ([guide](config-logging-actuators.md#distributed-tracing-the-opentelemetry-forwarder-opt-in)) | `false` |
| `otel.exporter.otlp.endpoint` | the OTLP/HTTP traces URL including the signal path | `http://localhost:4318/v1/traces` |
| `otel.exporter.otlp.headers` | request headers, `k=v` or `k: v`, comma-separated (re-read per export) | (none) |
| `otel.exporter.otlp.timeout` | per-export timeout in milliseconds | `10000` |
| `otel.exporter.otlp.connect.timeout` | connect timeout in milliseconds | the client default |
| `otel.exporter.otlp.compression` | only `none` is honoured (a warning otherwise) | `none` |
| `otel.service.name` | the `service.name` resource attribute of exported spans | `application.name` |

List-valued keys accept a comma/space-separated string (engine syntax) or a YAML list.

## Programmatic access

```python
from mercury_composable import app_config

config = app_config()
port = config.get("rest.server.port", 8085)
name = config.get_property("application.name", "application")
config.set("feature.flag", "on")     # runtime override, checked first
```
