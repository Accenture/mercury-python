---
title: HTTP Surface Reference
summary: The exact protocol and response shapes of the function host - Event API, actuators,
  error signature and content types.
audience: [developer, operator, ai-agent]
keywords: [http, reference, event api, actuator, error shape, content type]
---

# HTTP Surface Reference

*Reference: every byte the host serves.*

## POST /api/event — the Event API (envelope wire)

Mirrors the engines' `event.api.service`:

| Aspect | Behavior |
|--------|----------|
| Request body | event envelope bytes ([standard wire format](https://accenture.github.io/mercury-composable/guides/event-envelope-wire-format/)) |
| `x-ttl` header | execution bound in ms (floor 1000) |
| `x-async: true` | drop-n-forget → HTTP 202 with ack envelope `{type: async, delivered: true, time}` |
| Reply | always envelope bytes, `content-type: application/octet-stream` |
| Handler outcome | rides **HTTP 200** with the status inside the envelope (including AppException and unexpected errors) |
| Transport failures | set the HTTP status too: 400 undecodable / missing route field, 403 private target, 404 unknown route (`Route X not found`), 408 timeout (`Timeout for N ms`) |
| Header hygiene | inbound `x-event-api` and `my_*` removed; the `my_cid` tag becomes the read-only `my_correlation_id` header (local bus deliveries inject the same view). Outbound, the client stamps the current context's business correlation-id back onto the event as the `my_cid` tag — the engines' PostOffice parity, so the business correlation-id continues across every hop |
| `accept: text/event-stream` | streaming-capable call to an interceptor target: a streamed reply rides the same call as SSE in the envelope-mode dialect; a single-shot reply stays byte-identical; a streaming reply to a NON-accepting caller → 406 `Streaming function requires a caller that accepts text/event-stream`. See [Event Streaming](streaming.md) |

## Actuator endpoints

All JSON responses are pretty-printed with `content-type: application/json;
charset=utf-8` (the engines' default-serializer presentation).

| Endpoint | Content type | Shape |
|----------|--------------|-------|
| `GET /` | `text/html` | minimal index page linking the endpoints |
| `GET /info` | JSON | `{app{name,version,description}, runtime{language,python,mercury_composable}, origin, time{start,current}, up_time}` |
| `GET /info/routes` | JSON | `{app, routing{public{route: instances}, private{...}}}` |
| `GET /env` | JSON | `{app, env{environment{...}, properties{...}}}` (opt-in lists) |
| `GET /health` | JSON | `{dependency[...], status: UP\|DOWN, origin, name}` — HTTP 200 when UP, 400 when DOWN |
| `GET /livenessprobe` | `text/plain` | `OK`, or HTTP 400 `Unhealthy. Please check '/health' endpoint.` |

Each `/health` dependency entry: `{route, required, ...info-map, status_code,
message}` — the info map comes from the function's `type=info` reply; a missing route
reports `status_code: 404` with `Please check - Route X not found`.

- `origin` — unique instance id, minted once per process: UTC `yyyyMMdd` + 32-hex
  uuid (the Java reference engine's format).
- `up_time` — the engines' rendering (`59 seconds`, `1 minute 1 second`, …).

## Error signature (host-level)

Unknown paths and non-GET methods on known paths answer the engines' shape —
pretty-printed JSON:

```json
{
  "status": 404,
  "message": "Resource not found",
  "type": "error"
}
```

Handler-level errors never use this shape — they ride the envelope on `/api/event`.
