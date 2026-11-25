---
title: HTTP Surface Reference
summary: The exact protocol and response shapes of the function host - Event API, actuators,
  error signature and content types.
audience: [developer, operator, ai-agent]
keywords: [http, reference, event api, actuator, rest automation, rest.yaml, error shape, content type]
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
| `GET /` | `text/html` | the static folder's `index.html`, else a minimal index page linking the endpoints |
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

## REST automation entries (opt-in)

With `rest.automation: true` ([guide](rest-automation.md)) the entries of `rest.yaml` are served
first, in the engines' grammar; `POST /api/event` and the actuators are the default entries.

| Aspect | Behavior |
|--------|----------|
| Matching | the engines' precedence — the most specific route wins, `{param}` and a trailing `*`; a known path under another method → 405 `Method not allowed`; an unknown path → 404 `Resource not found`; `OPTIONS` → 204 with the entry's CORS `options` lines, 405 without a CORS block |
| Request to the function | the whole request as the `AsyncHttpRequest` map — method, url, headers (lowercased, after the request transform), path and query parameters (a form's fields among them), the body parsed by its content type (JSON → map or list, text types → text, else bytes), cookies, the `session` map an authentication function left; `x-ttl` carries the entry's timeout in ms |
| Reply | a returned value is the body; a returned `EventEnvelope` sets the status and headers (`set-cookie` values split on `\|`); the content type is the function's or negotiated from `Accept` (`text/html` wraps a map in the HTML shell; no `Accept` → no content type); a map or list is pretty-printed JSON with `null` entries omitted |
| Errors | the engines' body below with `content-type: application/json`: an `AppException` from the function or the authentication function (anything but `true` from the latter → 401 `Unauthorized`); 408 `Timeout for N ms` at the entry's timeout (1 s–5 min, 30 s default; an inbound `x-ttl` wins); 503 `Service X not reachable` for an unregistered function; 413 `Upload exceeds N bytes` |
| Correlation and tracing | `X-Correlation-Id` (or `http.correlation.id.header`) always ensured and echoed, delivered as `my_correlation_id`; with `tracing: true` a W3C `traceparent`, else `X-Trace-Id` (`http.trace.id.header`), else a fresh id, and the `http.request` record plus the function's round-trip record |
| `stream: true` | an interceptor's segments rendered progressively: Server-Sent Events (`event:`/`data:` frames, `event: done` at the end, `event: error` in band, `: ping` keep-alives every `event.stream.keep.alive`) for a `text/event-stream` head or `Accept`, chunked text or JSON lines otherwise; the entry's timeout is the idle allowance, the head's `x-ttl` overrides it; a keep-alive that cannot be written ends the stream |
| `upload: true` | a `POST` entry's `multipart/form-data` files in the request map (`filename`, `file_type`, `file_length`, `upload`, `size`, bytes in `contents`), the form's text fields as query parameters, no body; all files together within `rest.upload.max.bytes` (16 MiB) |
| Precedence | a `rest.yaml` entry, then an extension's mounted routes (`Platform.add_routes`), then the static folder (under the `static-content` block's `no-cache-pages` and `filter`), then the error signature below |

## Static content

A `GET` or `HEAD` of any other path is answered with the file it names inside `static.html.folder`
(`resources/public` next to the application file by default; `index.html` for a folder) when there
is one: the content type by extension from the engines' table, an `ETag` (`If-None-Match` answers
304 with no body) and, for HTML, `Cache-Control: no-cache`. A path that names nothing — including a
hidden entry, a link out of the folder or a folder without an index page — falls through to the
error signature below. See the
[guide](config-logging-actuators.md#static-content-the-engines-public-folder).

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

Handler-level errors never use this shape on `/api/event` — they ride the envelope. Behind a
`rest.yaml` entry a function's `AppException` *does* render as this shape, as in the engines.
