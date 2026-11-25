---
title: REST Automation
summary: Serve HTTP endpoints from rest.yaml - the engines' REST automation in the pack - so a
  Python utility application is designed and deployed as an engine application is, with no web
  framework of its own.
audience: [developer, operator, ai-agent]
keywords: [rest automation, rest.yaml, http endpoint, router, cors, authentication, upload,
  streaming, static content, AsyncHttpRequest, full-service application server]
---

# REST Automation

*Serve HTTP: `rest.yaml` is the router, your functions are the handlers.*

> **At a glance**
>
> - **What** — opt in with `rest.automation: true` and the host serves the entries of
>   `rest.yaml`: the engines' REST automation, where the file *is* the router. An entry maps a
>   URL and its methods to a function, with CORS, header transforms, an authentication function,
>   a timeout, tracing, a streaming reply, a bounded upload and static content.
> - **For** the utility applications of the field. A Python script that today brings its own web
>   framework, error body and deployment becomes an application designed, configured and deployed
>   in one consistent manner with the engines' applications — functions, a `rest.yaml` and a web
>   bundle, no framework. Ingress, never orchestration: a flow binding is refused by name.
> - **Grammar** — the engines' [REST automation guide](https://accenture.github.io/mercury-composable/guides/rest-automation/)
>   and [grammar](https://accenture.github.io/mercury-composable/guides/rest-automation/rest-grammar/)
>   apply unchanged, with the keys, the messages and the error body of the engines. This page says
>   what a pack serves, what it refuses, and how a function reads the request.

## Turn it on

```yaml
# resources/application.yml
rest.automation: true
# yaml.rest.automation: classpath:/rest.yaml   # the default - resources/rest.yaml next to the application file
```

One location is read: `classpath:` is the `resources` folder next to the application file (the
[resources convention](config-logging-actuators.md#configuration-the-engines-conventions)),
`file:` an absolute path, a relative path a file under the application file's folder. A missing or
empty file degrades to the default entries with a warning, as in the engines; an invalid entry
fails the start with the engines' message (`rest[0] missing 'service'`,
`rest[0] unknown cors id 'nope'`). With the switch off the host is exactly what it was.

## The file is the router

```yaml
rest:
  - service: "hello.rest"
    methods: ['GET']
    url: "/api/hello/{name}"
    timeout: 10s
    cors: cors_1
    headers: header_1
    tracing: true

cors:
  - id: cors_1
    options:
      - "Access-Control-Allow-Origin: ${api.origin:*}"
      - "Access-Control-Allow-Methods: GET, OPTIONS"
      - "Access-Control-Allow-Headers: Origin, Authorization, X-Correlation-Id, Accept, Content-Type"
      - "Access-Control-Max-Age: 86400"
    headers:
      - "Access-Control-Allow-Origin: ${api.origin:*}"

headers:
  - id: header_1
    request:
      drop: ['Upgrade-Insecure-Requests', 'cache-control', 'accept-encoding', 'connection']
    response:
      add:
        - "Cache-Control: no-cache, no-store"
```

`${key:default}` in any value resolves against the configuration, as in the engines. At the end
of the boot the host announces every live entry — `REST [GET] /api/hello/{name} -> hello.rest` —
and skips, with a warning, an entry whose function is not registered: never a live URL without a
function behind it.

**Matching** is the engines': the most specific route wins (literal segments over `{param}`), a
trailing `*` matches the rest of the path and `v*` a versioned segment, a known path under another
method is 405, an unknown path 404 in the error shape below, and `OPTIONS` is answered with 204 and
the `options` lines of the entry's CORS block (405 without one). `OPTIONS` is never listed in
`methods`.

## The function behind an entry

The function receives the **whole request** as its body — the engines' `AsyncHttpRequest` map,
never the bare HTTP body — and reads it with the engines' accessors:

```python
from mercury_composable import AppException, AsyncHttpRequest, Body, EventEnvelope, preload

@preload(route="hello.rest", instances=10)
async def hello_rest(headers: dict[str, str], body: Body):
    request = AsyncHttpRequest(body)
    name = request.path_parameter("name") or "world"          # from url: /api/hello/{name}
    if request.query_parameter("lang") == "fr":               # the first value of ?lang=
        return {"greeting": f"Bonjour, {name}"}
    if name == "nobody":
        raise AppException(404, "no such person")              # the standard error body on the wire
    return (EventEnvelope().set_status(200).set_header("x-source", "demo")
            .set_body({"greeting": f"Hello, {name}", "method": request.method}))
```

| Accessor | Reads |
|----------|-------|
| `method`, `url`, `query_string` | the method, the path, the raw query string |
| `header(name)`, `headers` | the request headers after the entry's request transform (names lowercased); `x-ttl` carries the entry's timeout in milliseconds |
| `path_parameter(name)`, `path_parameters` | the `{param}` segments |
| `query_parameter(name)`, `query_parameters(name)`, `query_parameters_map` | the first value, every value of a repeated key, the whole map; a form's fields (`application/x-www-form-urlencoded`) land here too |
| `body` | the parsed body: a map or list for `application/json`, text for the text types, bytes for anything else, `None` when there is none |
| `cookie(name)`, `cookies` | the cookies |
| `session_info(name)`, `session` | what the entry's [authentication function](#authentication) left |
| `remote_ip`, `is_secure`, `target_host`, `timeout_seconds` | the client, `X-Forwarded-Proto: https`, the `Host` header, the entry's timeout (a caller's `x-ttl` wins) |
| `files()`, `is_file`, `content_length` | an [upload's](#upload-the-bounded-form) files, whether there are any, their bytes together |

**The reply** is the function's: a returned value is the body; a returned `EventEnvelope` sets the
status and the headers too (`set_status(201)`, `set_header("Location", url)`, a `set-cookie`
header with `|` between cookies becomes one header each); a raised `AppException(status, message)`
is the error, rendered as the engines' standard body
`{"status": 404, "message": "no such person", "type": "error"}`. The response content type is the
function's own `content-type` header when it set one, else negotiated from `Accept` (`text/html`
wraps a map in the engines' HTML shell; no `Accept` means no content type). A map or list renders as
pretty-printed JSON with `null` entries omitted — the engines' serializer; a function whose client
reads a `null` serializes the text itself and sets `content-type: application/json`, as the
agent-question app does.

## What the edge does

The edge mirrors the engines, so a `rest.yaml` written for the Rust engine's subset loads here
unchanged — a flow binding apart — and behaves the same:

- **Correlation** — a business correlation id is always ensured (`X-Correlation-Id`, or the name in
  `http.correlation.id.header`), delivered to the function as the read-only `my_correlation_id`
  header and echoed on the response.
- **Tracing** — with `tracing: true` the edge starts a trace from a W3C `traceparent` first, else
  the trace-id header (`http.trace.id.header`, `X-Trace-Id`), else a fresh id, and leaves the
  `http.request` record and the function's round-trip record the engines leave, so the
  [OpenTelemetry forwarder](config-logging-actuators.md#distributed-tracing-the-opentelemetry-forwarder-opt-in)
  exports the request as a SERVER span with the function beneath it. An entry may rename its
  headers for a legacy caller with `trace.id.header`, `correlation.id.header` and
  `traceparent.header`.
- **Timeout** — the entry's `timeout` (1 s to 5 min, 30 s by default; the `/api/event` default
  entry keeps the engines' 60 s) rides the request as `x-ttl` in milliseconds and answers 408
  `Timeout for N ms` when it expires; an inbound `x-ttl` wins.
- **Headers** — the entry's `headers` block drops, keeps or adds request and response headers
  (`keep` takes precedence over `drop`, the Java rule); its `cors` block's `headers` lines ride
  every response.
- **Errors** — 404 `Resource not found`, 405 `Method not allowed`, 401 `Unauthorized`, 408 and
  503 `Service X not reachable`, all in the engines' body and `content-type: application/json`.

## Authentication

```yaml
rest:
  - service: "profile.lookup"
    methods: ['GET']
    url: "/api/profile/{id}"
    authentication: "api.key.check"
```

The authentication function is an ordinary function that receives the same request map before the
entry's function does. It returns `True` to let the request through — the headers of its reply
become the request's `session` map — and raises `AppException(401, ...)` to refuse; anything other
than `True` is 401 `Unauthorized`, an unregistered function 503.

```python
@preload(route="api.key.check", instances=10, private=True)
async def api_key_check(_headers: dict[str, str], body: Body):
    request = AsyncHttpRequest(body)
    who = lookup(request.header("authorization"))          # your own check
    if who is None:
        raise AppException(401, "sign-in required - a bearer access token")
    return EventEnvelope().set_body(True).set_header("who", who)

@preload(route="profile.lookup", instances=10, private=True)
async def lookup(_headers: dict[str, str], body: Body):
    request = AsyncHttpRequest(body)
    return {"id": request.path_parameter("id"), "for": request.session_info("who")}
```

Register the functions behind entries `private=True` when the edge is their only caller: a private
function is not reachable over `POST /api/event`, so nobody can forge a `session` map by calling
the function directly. The ask-person extension's `ask.identity` and `ask.reply.identity` and the
agent-question app's `question.identity` are the shipped examples — a person by the configured
mode, or a channel app under its bearer.

## Streaming entries

```yaml
rest:
  - service: "lines.stream"
    methods: ['POST']
    url: "/api/stream/lines"
    stream: true
    timeout: 30s
```

A `stream: true` entry renders an interceptor's segments progressively — the pack's
[streaming contract](streaming.md) at the HTTP edge: Server-Sent Events when the head or the
caller's `Accept` says `text/event-stream` (`event:` and `data:` lines, `event: done` at the end,
`event: error` with the standard error body in band), chunked text or JSON lines otherwise.

```python
from mercury_composable import EventEnvelope, EventStreamWriter, preload

@preload(route="lines.stream", instances=10, interceptor=True)
async def lines_stream(_headers: dict[str, str], event: EventEnvelope):
    out = EventStreamWriter.from_request(event)
    out.first(200, "text/event-stream")
    for line in produce(AsyncHttpRequest(event.body)):
        out.write_named("line", line)
    out.close({"lines": 3})
```

The entry's `timeout` is the **idle allowance** between segments, not a total bound; the head may
override it with `first(status, content_type, ttl_seconds)`. While the producer is quiet the edge
writes a keep-alive comment every `event.stream.keep.alive` (30 s), and a keep-alive that cannot be
written ends the stream and closes its reply sink. A long-lived producer — a device's feed that is
quiet for as long as nothing changes — opens the stream at once with a bare first segment
(`out.write(None)` commits the head and writes no frame), follows its source from a task of its
own so the function's workers are never held, and stops when `out.connected` turns false. The
agent-question app's `question.events` is the shipped example.

## Upload: the bounded form

```yaml
rest:
  - service: "llm.upload"
    methods: ['POST']
    url: "/api/llm/upload"
    upload: true
    timeout: 60s
```

`upload: true` on a `POST` entry is the pack's bounded form of the engines' streaming upload. The
files of a `multipart/form-data` body arrive in the request map under the engines' keys
(`filename`, `file_type`, `file_length`, the `upload` tags, `size`) with their bytes in `contents`,
read with `request.files()` as `UploadedFile(name, content_type, size, tag, content)`; the form's
text fields become query parameters and the body stays absent, as in the Java engine. All the files
of one upload together may not exceed `rest.upload.max.bytes` (16 MiB by default) — 413
`Upload exceeds N bytes` the moment they do, before anything is delivered; a malformed body is 400.
A non-multipart body on such an entry, and a multipart body on an entry without the flag, dispatch
as any other body. The LLM helper's `llm.upload` — documents saved into the attachments root for a
model to read — is the shipped example.

## Static content

A `GET` or `HEAD` no entry claims is served from the
[static folder](config-logging-actuators.md#static-content-the-engines-public-folder), under the
`static-content` block of `rest.yaml`: the `no-cache-pages` (default `/` and `/index.html`) are
served `Cache-Control: no-cache, no-store`, and the `filter` function sees every matching static
request first — its reply headers ride the response, a status other than 200 (a 302 to a sign-in
page) passes through, and a filter that fails lets the file be served.

```yaml
static-content:
  no-cache-pages: ['/', '/index.html']
  filter:
    path: ['/*']
    exclusion: ['*.css']
    service: 'http.request.filter'
```

## Default entries and precedence

`POST /api/event` and the actuators (`/info`, `/info/routes`, `/env`, `/health`, `/livenessprobe`)
are the engines' **default entries**, merged when the file does not claim their URLs, so a user
entry for `/api/event` can attach `authentication`, CORS or another timeout to the Event API
itself. Precedence on one server and one port: a `rest.yaml` entry, then an extension's mounted
routes (`Platform.add_routes`, the escape hatch for what an entry cannot express — a WebSocket),
then the static folder, then the engines' error shape.

## What a pack refuses, and what it defers

Refused **by name at load**, so the grammar itself shows the scope fence: a flow binding (`flow:`
or `service: http.flow.adapter` — flows run in the engines), the HTTP relay (an `http(s)://`
service) and the A/B dual service (a list of services). Each is a load failure with a message that
says so; nothing is silently ignored. Deferred, as in the Rust engine, until a field request:
`custom.content.types`, `mime.types`, XML bodies, request-body streaming and several `rest.yaml`
locations (the first of a comma-separated list is read, with a warning).

## The shipped examples

Every HTTP-facing piece of this repository is written this way, so the applications are the worked
examples:

| Application | `rest.yaml` | What it shows |
|-------------|-------------|---------------|
| `examples/demo-app` | `GET /api/hello/{name}` → `hello.rest` | a path parameter, a query parameter, CORS and a header transform |
| `extensions/llm-helper` | `POST /api/llm/upload` → `llm.upload` | the bounded upload: documents saved for a model to read |
| `extensions/ask-person` | the reply page and endpoint behind `ask.page`, `ask.wait`, `ask.reply`, `ask.config` | an HTML page with its status, JSON endpoints, two authentication functions (a person, or a channel app) leaving `who` and `via` in the session |
| `extensions/agent-question` | the person's side behind `question.*` and `transcript.*` | the device stream as a streaming entry that opens at once and follows a feed, one authentication function on five entries, a response header transform |

## Testing an entry

Serve the application as `mercury-serve` does — the configuration with the switch on and the file
named, the host built from the default registry — and call it with any HTTP client:

```python
from aiohttp import web
from mercury_composable import app_config, default_registry
from mercury_composable.server import EventApiServer

config = app_config()
config.set("rest.automation", True)
config.set("yaml.rest.automation", "file:/path/to/resources/rest.yaml")
runner = web.AppRunner(EventApiServer(default_registry).create_app())
await runner.setup()
await web.TCPSite(runner, "127.0.0.1", 0).start()
```

The pure half — the parser and its messages, the matching, the header transforms, the body
dispatch, the negotiation, the request model, the response mapping — is pinned by
`tests/vectors/rest-automation-vectors.json`, shared byte for byte with the Node.js pack, so both
packs answer the same statuses and messages for the same inputs.

## See also

- The engines' [REST Automation](https://accenture.github.io/mercury-composable/guides/rest-automation/),
  its [grammar](https://accenture.github.io/mercury-composable/guides/rest-automation/rest-grammar/)
  and [AI agent guide](https://accenture.github.io/mercury-composable/guides/rest-automation/ai-agent-guide/)
  — the source of truth for `rest.yaml`.
- [HTTP Surface Reference](http-surface-reference.md) — the exact response shapes.
- [Event Streaming](streaming.md) — the streaming contract behind `stream: true`.
- [Configuration, Logging & Actuators](config-logging-actuators.md) — the resources convention and
  the static folder.
- [AI Agent Guide](ai-agent-guide.md) — the pre-write checklist for an entry and its function.
