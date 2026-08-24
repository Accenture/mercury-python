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
2. The configuration file: `resources/application.yml` | `.yaml` | `.properties`, in
   the working directory or next to the application file, or `--config <path>`.
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

List-valued keys accept a comma/space-separated string (engine syntax) or a YAML list.

## Programmatic access

```python
from mercury_composable import app_config

config = app_config()
port = config.get("rest.server.port", 8085)
name = config.get_property("application.name", "application")
config.set("feature.flag", "on")     # runtime override, checked first
```
