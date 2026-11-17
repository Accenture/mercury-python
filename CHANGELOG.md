# Changelog

## Unreleased

### Added

1. **The transcript audit store: a durable, queryable record of every model turn (RFC-0012 in mercury-composable).**
   The LLM helper's transcript store is a working store - its time to live is the transaction's, and `retention` deletes
   a record at the end of the run by default - so an audit had no store to ask. `llm.transcript.audit: postgres` adds
   one beside it. Every turn is recorded asynchronously, off the turn's path: after the compare-and-set accepted a turn,
   `llm.chat` sends one local event to `transcript.audit`, a private composable function of the helper's process with one
   instance, whose mailbox orders the writes and the end-of-run check of a conversation without a lock. The function
   reads the working record - version 2 now, carrying each turn's facts inside the same compare-and-set (trace id and
   path, correlation id, the UTC bounds of the model call, outcome, usage) with the system prompt, the catalog and the
   graph context stored once - and writes the rows PostgreSQL lacks: a conversation row (identity, status, turn count,
   times, token totals, the first-turn snapshot) and a turn row per model turn (the messages the turn sent as `request`,
   the assistant content blocks as `response`, the normalized `calls`), the provider's format as sent and as returned.
   A closing turn sends the integrity check behind its write: the turns the record holds are counted against the rows
   the store holds, the missing ones written, and only then is the working record released to the `retention` it
   carried (`0` deletes it); until then it waits with `llm.transcript.audit.grace` (`1d`). A sweep
   (`llm.transcript.audit.sweep.ms`, 60 s, one replica per interval on Redis) sends a check for every record whose
   audited mark - a third hash field beside `turn` and `record` - lags its turn count: the runs the loop stopped and the
   writes a dying process lost. A failed write is logged by type and SQLSTATE, never by content, and never refuses a turn.
   The `postgres` extra adds psycopg 3 (`pip install 'mercury-composable[llm,postgres]'`); a configured driver that is not
   installed stops the application by name, a database that does not answer at boot is logged and repaired by the sweep.
   `extensions/llm-helper/schema/postgres.sql` and `purge.sql` ship the reference schema and the purge (retention and
   purge are the installation's; `expires_at` is written from `llm.transcript.audit.retention`, `365d`),
   `dev/postgres-up.sh`, `postgres-down.sh` and `audit-purge.sh` the development setup on Colima with pgweb as the SQL
   browser. `llm.chat` records the loop's `x-graph-id` and `x-node` headers at the first turn; `llm.health` reports
   `transcript_audit`. Pinned by `tests/test_transcript_audit.py` (28 tests on the memory store and a recording fake of
   the connection pool) and a live test gated on `LLM_TRANSCRIPT_AUDIT_TEST_URL`; the shared loop vector file is
   unchanged, since the audit is a side effect of a turn and not part of the loop contract. The Node twin follows in
   lockstep. **Upgrade note:** none without `llm.transcript.audit`. A record saved by the previous helper (version 1)
   continues under the new one; its turns before the upgrade carry no facts, and the check logs them as unaudited.

2. **The built-in agent tools: files inside one workspace, the web inside an allowlist, and a clock (RFC-0013 in
   mercury-composable, build plan step 1).** `extensions/agent-tools/agent_tools.py` serves seven composable functions
   an agent loop names in its tool catalog: `file.read`, `file.write` (the one writer, `write: true`, `create` the
   default and refusing an existing file), `file.list` and `file.search` inside `tools.file.root` - every path
   relative to the root, resolved with symbolic links followed and refused outside it, hidden entries invisible unless
   `tools.file.hidden=true`; `web.fetch` and `web.crawl` inside `tools.web.allow` - https unless a host is listed with
   `http://`, the host resolved and checked against the loopback, private, link-local and multicast ranges before the
   request is made to the checked address, every redirect checked again, the body cut at `tools.web.max.bytes`, HTML
   reduced to readable text or Markdown by a small in-house parser, a crawl same-host, breadth-first, `robots.txt`
   honoured, bounded by pages, depth, an include prefix and a delay; and `time.now`. A tool whose sandbox is not
   configured does not register, so an unconfigured tool is a 404 to the loop's pre-flight check; every refusal is a
   400 naming the rule and the key, which the loop returns to the model as the call's error. Each tool ships its
   catalog row - the name the model sees, the description, the closed input schema, the route and the write mark - as a
   constant, as a file under `extensions/agent-tools/catalog/` (shared byte for byte with the Node.js twin) and through
   `python agent_tools.py describe`, which also says what registers under the current configuration. `tools.health`
   reports the registered routes, the workspace and the allowed sites. No new dependency: aiohttp fetches, the standard
   library parses. Pinned by `tests/test_agent_tools.py` (97 tests: a workspace with an escaping link, a fixture site
   served in-process with redirects to private and foreign hosts, the allowlist and address rules without a network).
   The knowledge-graph tools and `file.extract` follow as step 2, the Node.js twin as step 3.

### Fixed

1. **A direct `python extensions/mcp-binding/mcp_binding.py` now connects its servers.** The binding attached its boot
   and shutdown hooks to the platform only when loaded by `mercury-serve`; run directly, the Event API started with no
   server connected and every bound route answered 503. Both entry points attach the hooks now, so a bad server file
   stops the direct run by name as it stops the served one.

### Changed

1. **A `-D` override value is resolved with the same `${ENV_VAR:default}` substitution as a file value - the engines'
   change of the same day.** `AppConfig.get` returned an override as written, so a key moved from `application.yml` to
   a `-D` flag silently lost its substitution and a credential had to travel on the command line in clear. Now the value
   of a `-D` override (and of `AppConfig.set`) - an environment variable with a default, or a reference to another
   configuration key - is resolved the way a file value is, at read time, so `-Dkey='${ENV_VAR}'` keeps the secret out
   of the process list and container `args` arrays, which do no shell expansion. In lockstep with the Node pack.
   **Upgrade note:** a `-D` value that relied on a literal `${...}` is now resolved, as a file value always was; an
   unresolved reference without a default is no value, as in a file.

2. **The transcript audit store's PostgreSQL pool sets a server-side `statement_timeout` from
   `llm.transcript.audit.postgres.timeout.ms`.** The live drive of 2026-11-17 (mercury-composable
   `docs/test-reports/transcript-audit-live-drive.md`) showed a stopped database failing a write by type within the
   connection timeout, as designed, but a paused one - a TCP black hole - holding the pooled connection's statement, and
   with it the audit function's mailbox, for the outage's length. The statement now times out on the server after the
   same `timeout.ms`, so a stalled database behaves like a stopped one: a logged failure, repaired by the sweep. In
   lockstep with the Node pack. **Upgrade note:** none.

## Version 4.12.30, 11/16/2026

The catch-up round with the engines: the pack moves from 4.12.21 to 4.12.30, the number the Java engine released on
2026-11-05 and the Rust port caught up to (a version names content, not a cadence; a pack catches up when functionality
crosses from the engine to the language packs). This release carries the pack's half of the engines' agent loop
(ADR-0033): `llm.chat` serves one model turn of `llm.run` with the transcript in the helper's own store, in memory or on
Redis (Added 2); the LLM helper reaches Claude through AWS Bedrock as well as the Claude API (Added 1); and the new MCP
binding, `extensions/mcp-binding/`, declares a tool of an MCP server as a composable function with the digest of its
input schema pinned in code (Added 3) - all driven live end to end from the Java engine through this pack
(`docs/test-reports/agent-loop-live-drive.md` and `agent-tool-use-live-drive.md` in the engine repository). The host
leaves the trace records a Java or Rust callee leaves, so a span lineage stays continuous from an engine into this pack
(Changed 4); `@preload` takes route aliases (Added 4); the JSON log presentations label the emitting logger `source`
(Changed 5). The optional extras are `llm`, `bedrock`, `redis` and `mcp`; the package itself keeps its three
dependencies. **Upgrade notes:** the LLM helper lives in `extensions/llm-helper/` (Changed 2), and the `resources`
folder is resolved next to the application file, never in the working directory (Changed 1).

### Added

1. **AWS Bedrock as the LLM helper's second backend (`llm.backend: 'bedrock'`).** The helper reaches the same Claude models
   through the SDK's Bedrock client, signed with the AWS credential chain through IAM (`AWS_ACCESS_KEY_ID` and
   `AWS_SECRET_ACCESS_KEY`, a profile, a role) or a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` - no vendor API key, the
   platform-mediated access an enterprise security policy asks for. The new `bedrock` extra adds boto3
   (`pip install 'mercury-composable[llm,bedrock]'`); the region comes from `llm.aws.region` or `AWS_REGION`, and without one
   the helper answers `503 AWS region missing` instead of starting a call. A bare model id is sent with the `anthropic.`
   prefix and an id already spelled for Bedrock as written; server-side refusal fallbacks are never sent there (Bedrock offers
   none); a call whose credential chain is empty is a `503 AWS credential missing` by name. `llm.health` reports `region`
   beside `backend` and `model`, and its check resolves the credential chain the way a call would, without spending a token.
   Bedrock serves Claude on two surfaces and an account may be enabled for one only: `llm.aws.api` selects `mantle` (the
   default, the Anthropic-shaped endpoint with the prefixed ids) or `runtime` (the classic one, every model id sent as written
   - an inference profile id as the account lists it), and `llm.health` names the surface. Certified live on 2026-10-14 against
   an enterprise account, negative controls first (a wrong secret a 401 by name, no credential a 503 by name), then a chat
   reply, a structured verdict and a progressive stream on Haiku 4.5 through the runtime surface, from both packs and through
   the Java engine's shipped triage graph and streaming relay (the engines' `docs/test-reports/llm-helper-certification.md`, Scenario 8, and this pack's own report).
   The routes and the contract are unchanged: the shared vector file gains five Bedrock cases (68), byte-identical with the
   Node pack, and the Bedrock clients, their region and credential checks are pinned by unit tests. **Upgrade note:** none - the
   Claude API backend is the default and behaves as before; `llm.backend` now also accepts `bedrock`.
2. **The loop contract: `llm.chat` serves one model turn of the engines' agent loop (RFC-0011).** A request that carries
   `tools` is a turn of `llm.run`, the agent loop of the Java and Rust engines, which dispatches each tool call to a
   composable-function route. The transcript stays in the helper's own store and the loop holds a reference to it: the
   first turn returns `conversation` and `turn`, a later turn sends them back with `results` (`[{id, output}]` or
   `[{id, error}]`), and the helper appends the one `tool_result` message, in call order, and the model's turn exactly as
   the API returned it, thinking blocks included. The reply carries `status` (`done`, `tool_use`, `refusal` or `cap`),
   `calls [{id, name, input}]`, the cache usage, and `cap` (`max_tokens` or `context`) for a cut-off turn, whose calls
   never run. A stale turn count, or a turn on a run that has ended, is a 409, and each write is a compare-and-set on
   the count, so a duplicated turn can never fork a conversation; an ended or expired one is a 404. The model is pinned
   at the first turn, server-side refusal fallbacks are never sent on a loop turn, and the helper places the cache
   breakpoints (the system prompt, and a top-level one for the growing conversation). `llm.transcript.store` names the
   store: `memory` for one process, `redis` for any number of replicas (the new `redis` extra; one hash per conversation
   under `transcript:{conversation}`, a Lua compare-and-set, a connection from the `llm.transcript.redis.*` keys). Every
   write sets the record's time to live to the turn's `ttl` (default `7d`), and a run the model ends takes `retention`
   (default `0`, which deletes the record). A new shared vector file, `tests/vectors/llm-loop-vectors.json` (66 cases),
   is shared byte for byte with the Node twin, which runs it too (mercury-nodejs #115); the cases run on the memory store, a Redis fake, and a live Redis when
   `LLM_TRANSCRIPT_REDIS_URL` names one. **Upgrade note:** a request without `tools` behaves as before. `tools`,
   `conversation`, `turn`, `results`, `ttl` and `retention` are now fields of the loop contract: on `llm.chat` they need
   `tools`, and on `llm.stream` any of them is a 400. A loop turn needs `llm.transcript.store`; the sample configuration
   sets `memory`.

3. **The MCP binding: `extensions/mcp-binding/`, a composable function per MCP tool (RFC-0011 / ADR-0033, build plan
   step 4; the declaration shape of the engines' `draft-design-specs/agent-tool-use.md`, 2026-11-16).** A library and a
   function host on the official MCP SDK (`mcp` 2.x, the new `mcp` extra: `pip install 'mercury-composable[mcp]'`). A tool
   of an MCP server is declared where every route is declared, in the code:
   `render = bind("mermaid", "validate_and_render_mermaid_diagram", schema_sha256="5ae6…")` names the server, the remote
   tool and the SHA-256 digest of its input schema in canonical JSON and returns the tool as an async callable, and an
   ordinary `@preload` function (`mermaid.render`) calls it - passing the arguments through, shaping them or adding
   behaviour around the call. A route names the capability, never the transport: a tool may start as an MCP tool and later
   be implemented another way, and the graph must not know. Servers are named in `resources/mcp-servers.yml`, each with
   what is environment-specific: its transport (`stdio` with a command this process starts, or `http` with a streamable
   HTTP URL), its credential references (`${VAR}` placeholders resolved from the environment at boot, never logged; a
   stdio subprocess gets the SDK's minimal environment plus the variables listed) and its call timeout. Boot connects
   every server a function binds a tool of, inside the serving loop, lists its tools and refuses to start - by name - when
   a bound server is not configured, a bound tool is missing or its schema no longer matches the pinned digest, so a
   server-side change stops a deploy instead of reaching a model at run time; a configured server nobody binds is not
   connected. A function's body is the tool's arguments (a map, else a 400); its reply is the server's structured content,
   else the text of its content blocks; a result marked `isError` is a 400 carrying the server's text (the agent loop
   returns it to the model as that call's error), a slow call a 408, a protocol failure a 502, a server not connected a
   503. `python mcp_binding.py describe [server]` prints each tool's digest, the `bind` line to paste and a catalog row for
   the graph, for the developer to review: the model reads the graph's descriptions, never the server's. `mcp.health`
   reports each connected server and how many tools its functions bind. Out of the box the default server file names the
   public Mermaid Chart MCP endpoint and the extension's `mermaid.render` binds its one tool, so the binding starts and
   serves a tool with no credential. The shared vector file `tests/vectors/mcp-binding-vectors.json` (version 2; the
   Node.js twin runs the same bytes) pins the digest, the verification of declared bindings and the result mapping; the
   rest of the tests drive a fixture MCP server of this repository over both transports, and the HTTP transport was driven
   live against the Mermaid endpoint. The first shape of this binding (built 2026-11-14, before any release) read a
   `tools` allowlist under each server and registered each tool's route at boot as an alias of a private `mcp.tool`
   function; it is gone, and a leftover `tools` key is refused by name. **With it, `Platform` gains `on_startup(callback)`
   and `on_cleanup(callback)`** - async callbacks run inside the server's event loop before the first request and at
   shutdown, the place for an extension's asynchronous boot work - and `Platform.create_app()`, the hook-bearing
   application `run` serves and a test may serve with aiohttp's runner. **Upgrade note:** none - nothing changes for an
   application that registers no callback.
4. **Route aliases: `@preload` takes one route name or a comma-separated list.** `@preload(route="hello.declarative,
   hello.world")` registers the same function under every name with the same `instances` and visibility - the Java
   engine's `@PreLoad(route = "a.b, c.d")` and the Rust port's `#[preload(route = "a.b, c.d")]`; before, the decorator
   took one name and a list failed as an invalid route name. The value is split on commas and whitespace as the Java
   engine splits it, empty segments are dropped, every name is validated and a repeated name counts once; a value with
   no name is refused with the engine's words (`missing service route(s)`). Each alias has its own mailbox and workers,
   its own line in the startup log and its own row in `/info/routes`, and a handler reads the name it was called as from
   `get_trace().route`, the engines' `my_route`. `FunctionRegistry.register` still takes one route, as the engines'
   `Platform.register` does. The demo app shows it: `hello.world` is an alias of `hello.declarative`.
   **Upgrade note:** none - a single route name behaves as before.

### Changed

1. **The `resources` folder is the one next to the application file; the working directory is never searched.**
   `mercury-serve app.py` reads `resources/application.yml` (`.yaml`, `.properties`) beside `app.py`, and a direct
   `python app.py` beside the script Python ran, wherever the command runs from - the engines' classpath convention: the
   configuration travels with the application and does not depend on the launch directory. Before, the working directory
   was searched first and the application's folder only as a fallback, so a direct `python llm_helper.py` from the
   repository root started with no configuration at all (the default name and port, no health dependencies), and a stray
   `resources/` in the launch directory could shadow the application's own. `--config <file>` still names another file;
   the companion `app-log-context.yaml` follows the same folder; `AppConfig.source` is now the absolute path of the file
   found and `AppConfig.resources_dir` names the folder; the startup log reports `Configuration: <path>`.
   **Upgrade note:** an application that kept `resources/` in the working directory with its application file elsewhere
   passes `--config`, or moves the folder next to the file.
2. **The LLM helper graduates from an example to an extension: `extensions/llm-helper/`.** It is no longer a sample to copy
   but a function host an installation runs to give its engines an LLM, so it moves out of `examples/`, which keeps the
   demo app - in lockstep with the Node twin (mercury-nodejs #114). The files, the routes, the contract and the
   configuration keys are unchanged; the sdist ships the new folder.
   **Upgrade note:** start it as `mercury-serve extensions/llm-helper/llm_helper.py` (or
   `python extensions/llm-helper/llm_helper.py`); a script or a deployment that names the old path changes it.
3. **The Scope allows one subprocess: an MCP server reached over stdio (2026-11-03).** A function still runs in the
   pack's own process. The pack may run a stdio MCP server as its child process, which is the reason the MCP clients of
   the engines' agent loop (RFC-0011) live in the language packs and not in the engines. Nothing starts one yet: the stdio
   MCP binding is a later step. The README's Scope also says the LLM helper is an application on the package, whose
   `llm.chat` is a stateless chat or a loop turn with a configured transcript store. **Upgrade note:** none.
4. **Tracing: the host leaves the records a Java or Rust callee leaves, so a span lineage is continuous from an engine
   into the pack.** A live drive of the engines' agent loop through both packs (2026-11-14) found the pack legs absent
   from the trace: the calling engine's HTTP client leg is in `skip.rpc.tracing` by default and the host suppressed its
   own record for every RPC-served execution, so `llm.chat` and an MCP binding's route appeared only as time inside the
   engine caller's span. Now a traced call arriving at `/api/event` leaves `http.request` (`POST /api/event`, the edge's
   round trip, parented onto the caller's span - the envelope's span, else the `traceparent` header's; the backend's
   SERVER span), `event.api.service` (the ingress, parented onto the edge) and, for an RPC, the function's round-trip
   record with the responder's span and `round_trip`, parented onto the service - the records REST automation,
   `EventApiService` and `InboxBase` leave in the Java engine; a drop-n-forget or streaming function's own record
   parents onto the service; a refusal or a timeout leaves the edge record with its status. A reply now carries the
   responder's span (`WorkerHandler` parity), and the `PostOffice` records a local RPC round trip the way an engine
   inbox does. An untraced call records nothing. **Upgrade note:** a backend shows three more spans per traced call into
   the pack and one per traced local RPC; a dashboard that read the pack's response time off a function's span moves to
   the `http.request` edge record, as for the engines.
5. **The JSON log presentations label the emitting logger and line `source`, as the Java engine's JSON layout does.**
   A line written with `log.format=json` or `compact` carried the logger name and line number under `logger`
   (`"logger": "distributed.tracing:412"`), where the Java engine writes `source`; a polyglot installation that reads
   both in one aggregator now finds the field under one name. The value is unchanged (`name:line`), and so are `time`,
   `level`, `message`, `context` and `exception`; the `text` presentation is untouched. **Upgrade note:** an aggregator
   query or dashboard field that read `logger` from this pack's JSON lines reads `source`.
6. **Code quality: the findings of a local SonarQube and IDE inspection round (2026-11-16) in the package, the
   extensions and the tests, with no behaviour change.** The MCP binding's server parser is split by transport, its
   verification loop no longer shadows the module's `binding`, its suppression comments take the plain `# noqa` form
   with the rationale beside them, a failure inside a server's task is caught as `Exception` (a cancellation still passes
   through), the probe behind `describe` is the public `offered_tools`, and `describe` spells a tool's state without a
   nested conditional. The bus computes an execution's time once and reads a failure's status without a nested
   conditional, the span mapper and the bus rename the names that shadowed `annotations`, the forwarder settings copy
   a fixed header list without a redundant call, and the host's exporter-closing hook binds its narrowed reference. The
   LLM helper's duration pattern uses `\d` and hands the AWS session only the options the client carries. The tests
   split composite assertions, keep one call inside each `pytest.raises`, patch state through `monkeypatch`, and share
   the Event API host fixture through `tests/conftest.py`. **Upgrade note:** none.

7. **Code quality: the cognitive-complexity findings of a second local SonarQube and IDE round (2026-11-16), with no
   behaviour change.** Seven functions the scanner measured above its threshold of 15 are split along their natural
   seams: the Event API host reads the `x-ttl` bound, delivers an RPC and builds an interceptor's envelope in helpers of
   their own, and renders a stream segment through `_classify_segment` - the shape the Node twin already had; the OTLP
   response decoder reads the `partial_success` block and its fields in one helper each; the span mapper computes the
   start time, the status and the attributes apart, and the ISO-8601 parser validates the date-time fields and reads
   the fraction apart; the LLM helper validates one tool, and the shape of a first or a later turn, in helpers of their
   own. Every check runs in the order it did and raises the message it did, so the shared vector files pass unchanged.
   Also: the exporter binds its lazy session through a local the checker can narrow, the MCP binding builds its
   unknown-key list from strings and names the error in its shutdown warning, and the envelope codec drops a `cast` for
   a one-line checker note. **Upgrade note:** none.

## Version 4.12.21, 10/7/2026

The lock-step round with the engines: the pack moves from 4.12.15 to 4.12.21, the number the Java engine and the
Rust port ship in the same round (`conv-ports-adopt-java-release-number` - a version names content, not a
cadence). This release carries the LLM helper app - `llm.chat`, `llm.stream` and `llm.health` on the official
Anthropic SDK, certified in front of both engines with real Claude calls (`docs/test-reports/llm-helper-certification.md`
in the engine repositories) - and the stricter contract and the example layout that came with it; read the two READ
notes under *Changed* before upgrading an example.

### Added

- The **LLM helper app** (`examples/llm-helper/`): a dedicated function host for the AI nodes of the
  agent-orchestration experiment, on the official Anthropic SDK (default model `claude-opus-5-5`).
  `llm.chat` answers once, with optional JSON-schema structured output returned parsed as `data`;
  `llm.stream` relays the model's token batches over the multi-shot reply contract, each batch
  forwarded the moment it is produced and never gathered; `llm.health` reports whether a call could
  be sent, with no network traffic. The contract, the `llm.*` configuration keys, the error contract
  and the backend seam (AWS Bedrock through IAM is the planned second route) are in its README.
- One shared contract file, `tests/vectors/llm-helper-vectors.json`, byte-identical in mercury-nodejs:
  63 cases (request validation, the exact SDK call, replies, errors, streaming) run against a fake
  of the SDK in each pack, so the two helpers cannot drift apart. No token is spent, no credential
  needed. A separate test fails if a token batch is held back.
- A reply that carries nothing usable is an error, never an empty success: a refusal with no text, an
  empty reply, or a structured reply cut off before it is valid JSON is a 422 that names the
  `stop_reason`, the tokens spent and what to raise. A stream that ends before its first token fails
  with a 422 instead of an empty 200.
- Server-side refusal fallbacks (`fallbacks: "default"`) on the models that support them, switchable
  with `llm.fallbacks=off`; `request_id` in every reply and terminal event; usage on the trace
  record (`llm_model`, `llm_stop_reason`, `llm_input_tokens`, `llm_output_tokens`, `llm_request_id`).
- `llm.log.batches` (off by default): log each streamed batch's number, size and arrival time, never its
  text, to tell the hop that holds batches back from the one that forwards them. The certification drive
  used it to show that every batch the helper forwarded reached the engine edge as its own frame. It
  also showed that the cadence of progressive rendering is the API's and differs by model: Haiku 4.5
  streams continuously, Opus 5.5 in bursts about every 600 ms (documented in the README).

### Changed

- The AI nodes moved out of the demo. `examples/demo-app/demo_app.py` is the minimal polyglot demo
  again, with no LLM code; `llm.chat` and `llm.stream` live in `examples/llm-helper/llm_helper.py` on
  the same default port (8086), with the same request surface, so a graph that names those routes (the
  `support-triage` graph) works unchanged.
- **READ - each example app now lives in its own folder**, with its own README and its own `resources/`:
  `examples/demo-app/` and `examples/llm-helper/`. The demo moved from `examples/demo_app.py`, and its
  sample configuration from `examples/resources/application.yml` to
  `examples/demo-app/resources/application.yml`. Start it with
  `mercury-serve examples/demo-app/demo_app.py`; the routes and the default port are unchanged.
- **READ - the contract is stricter than the demo nodes were.** A `params` key outside `provider`,
  `model`, `max_tokens`, `timeout_ms`, `effort` and `stop_sequences` is a 400 (the demo forwarded any
  key to the provider SDK); a `schema` on `llm.stream` is a 400 (it was ignored); on `llm.chat`,
  `params.timeout_ms` bounds the whole call, retries included; provider errors read
  `LLM provider error - {status} {type}: {message} (request_id …)`.
- The `llm` extra is `anthropic>=1,<2` only.

### Removed

- The Gemini provider (`google-genai`) and its selection: the helper serves Claude only, so
  `-Dllm.provider=gemini` is now a 400 that names what is served.

## Version 4.12.15, 9/22/2026

The lock-step round with the engines: the pack moves from 4.12.1 to 4.12.15, the number the Java engine and the
Rust port ship in the same round (`conv-ports-adopt-java-release-number` — a version names content, not a
cadence). Interop evidence for this release: the four-runtime OpenTelemetry certification drive of 2026-09-22 —
this host's forwarder and `llm.stream` node under both engines' edges, one connected trace per request, confirmed
in the backend UI (`docs/test-reports/otel-dynatrace-certification.md`). Off by default; nothing to configure.

### Added

- The **OpenTelemetry trace forwarder** (`mercury_composable.otel`), the lock-step twin of the
  engines' `opentelemetry-forwarder` extension: with `otel.forwarding=true` the host hands
  every distributed-trace dataset it emits to the engines' extension route
  `distributed.trace.forwarder`, where the built-in forwarder maps it to one OpenTelemetry
  span carrying the host's exact W3C ids and exports it over OTLP/HTTP (protobuf) - the same
  `otel.*` keys, span mapping, per-export credential headers, retry policy and failure
  diagnostics as the Java and Rust forwarders. No OpenTelemetry SDK and no new dependency:
  the encoder is the Rust port's hand-written OTLP writer, ported. Off by default.
- The `distributed.trace.forwarder` extension route itself: a function an application
  registers under that name receives every emitted dataset (delivered without a trace, so
  the forwarder's own execution emits none) - the engines' zero-tracing forwarder contract.

### Changed

- The forwarder's span kind now follows the engines' connected-span-tree rule: the record whose
  `service` is `http.request` - an engine edge's round-trip record - is the `SERVER` span, and every
  function execution is `INTERNAL` (a record's `from` no longer decides the kind). A host's own
  records are function executions, so a backend now shows them as `INTERNAL` hops under the
  calling engine's edge span.

- The demo AI nodes' default Gemini model is the stable alias `gemini-flash-latest`: the dated
  `gemini-3.6-flash` id is no longer served (404 on a current key) - the alias moves with the
  provider's current flash model. `llm.model` / `params.model` still pin a specific id.
- The demo `application.yml` carries the `otel.*` keys switched off, wired to the
  `OTLP_API_ENDPOINT`, `OTLP_AUTH_HEADER` and `OTLP_TOKEN` environment variables, so
  `-Dotel.forwarding=true` at run time is enough to export to a backend.

## Version 4.12.1, 9/1/2026

The first PyPI release: `pip install mercury-composable`. The v4.12.0 lock-step line
plus the AI-node demo functions below.

### Added

- `llm.chat` demo function (examples/demo_app.py) - the AI node of the agent-orchestration
  experiment E0: a provider-neutral LLM adapter (Anthropic and Gemini backends behind one
  contract; select with `llm.provider` config / `-Dllm.provider=...` / `params.provider`)
  with structured output (`schema` -> JSON verdicts for graph decision routing;
  additionalProperties defaults closed), usage/stop_reason surfacing, provider-error
  mapping onto the envelope status, and the params.timeout_ms time-budget mapping.
  The SDKs are an optional extra (`pip install "mercury-composable[llm]"`); the package
  itself stays SDK-free (scope fence intact).
- `llm.stream` demo function - the STREAMING AI node: pulls the provider's real token
  stream (Anthropic `messages.stream` or Gemini `generate_content_stream`) and relays
  each token batch over the multi-shot reply contract, so a calling engine renders it
  progressively out its own HTTP edge as SSE. Terminal metadata carries model,
  stop_reason, usage and the trace/business correlation ids; provider errors fail the
  stream in-band. Live-proven end to end with real Gemini tokens (2026-08-31).
- PyPI publication metadata: SPDX license expression (PEP 639), trove classifiers,
  keywords, project URLs, and a constrained sdist (package sources, tests, examples
  and top-level docs only - repo-internal layers never ship).

## 4.12.0 (2026-08-30)

The progressive-rendering milestone release. The version aligns with the Mercury
Composable engine lock-step line (Java and Rust engines, Python and Node.js
language packs all at v4.12.0): token/event streaming end to end with full
OpenTelemetry lineage, business-correlation continuity and application log
context across all four runtimes - useful on its own, and the foundation for
the AI SDLC (agent, MCP and tool adapters as wrapper-side functions with
complete observability).

- **Event streaming** - the platform-wide multi-shot reply contract, both halves.
  Producer: `@preload(..., interceptor=True)` handlers receive the raw envelope
  and stream through `EventStreamWriter` (the engines' exact API - `first`,
  `write`, `write_named`, `close` with trailing metadata, `fail` with the standard
  error key-values); the `/api/event` host answers a caller that accepts
  `text/event-stream` with the platform's hybrid SSE dialect (envelope frames for
  the head, the terminals and non-text segments; raw frames for text tokens),
  refuses a non-accepting caller of a streaming function with the pinned 406, and
  keeps single-shot replies over the capable path byte-identical. Consumer:
  `PostOffice.stream()` (an async iterator yielding the same decoded envelopes an
  engine reply route receives, with the dialect conformance guards) and
  `PostOffice.stream_to()` (the relay form: forward your caller's reply address
  and segments flow through verbatim - engine-parity composition). Under it all,
  the primitive event bus gained the engines' reply_to mechanism: envelope-routed
  delivery to a LOCAL function or per-request reply sink - simple routing, no
  orchestration. Same keep-alive config key as the engines
  (`event.stream.keep.alive`). Engine-identical wire and messages
  (Java PR #299-#301 / Rust PR #216-#218 lineage).
- **Business correlation-id continuity** (the engines' PostOffice parity): the
  client stamps the current context's business correlation-id onto outbound
  events as the engine-managed `my_cid` tag, local bus deliveries inject the
  read-only `my_correlation_id` header view exactly like the HTTP host, and
  `get_trace()` / `trace_context()` carry `my_correlation_id` - so the business
  correlation-id continues across engine⇄wrapper and wrapper⇄wrapper hops.
- **Span lineage** (the engines' telemetry model): every traced execution mints
  a 16-hex span with the caller's span (from the inbound envelope) as its
  parent, outbound events carry the current span so the next hop parents onto
  it (`PostOffice.touch` parity, W3C `traceparent` included), streaming
  segments carry the producer's span, and non-RPC executions emit the engines'
  distributed-trace dataset record on the `distributed.tracing` log stream -
  the same `{"trace": {...}, "annotations": {...}}` shape the Java engine
  logs, so stdout log-ingest agents stitch spans across all four runtimes.
  RPC round-trips are suppressed exactly like the engines (the new `rpc`
  envelope tag rides `request()` calls). `trace_context()` accepts `span_id`
  to parent onto an external OpenTelemetry span. Outbound events and stream
  segments also fill their sender with the executing function's route, and
  the `/api/event` host fills `event.api.service` for an anonymous caller -
  the engines' sender-attribution rules.
- **Application log context** (the engines' app-log-context feature, on by
  default via the packaged `default-log-context.yaml` - the engines' resource
  twin): with `log.format` json/compact, every log line inside a traced
  request carries a `context` block - cid (the business correlation-id),
  traceId, tracePath, spanId, parentSpanId, service, timestamp - so app logs
  and the distributed-trace records correlate end to end. Customize with
  `resources/app-log-context.yaml` (reserved `$tokens` or constants with
  `${ENV:default}`), opt out with `app.log.context=false`, and add
  per-request key-values with `update_context()` (reserved keys guarded).
- Documentation site (mkdocs-material, the engine repo's theme): the three-layer theme
  reference, rationale/design foundations, function-writing patterns, flow and
  knowledge-graph join chapters, a one-page AI agent guide with llms.txt, and
  configuration/HTTP references - published to
  https://accenture.github.io/mercury-python/ by the new CI workflow, which also runs
  the three quality gates (pytest, ruff, basedpyright) on every push and pull request.
- Host polish for engine parity: `GET /` serves the engines' minimal index page linking
  the actuator endpoints (embedded - no static file service by design); actuator JSON
  responses are pretty-printed (the engines' default-serializer presentation); unknown
  paths and non-GET methods answer the engines' error shape
  `{"status": 404, "message": "Resource not found", "type": "error"}`.
- Sync bridge: `PostOffice.request_sync()` / `send_sync()` let a plain `def` handler
  (the synchronous ecosystem - `requests`, NumPy/ML inference, database drivers) call
  sibling or remote functions - the call runs on the host event loop while only the
  handler's worker thread blocks; the trace chain rides across unbroken. Calling the
  bridge from async code, or outside a hosted function, is refused with a teaching
  error. The sync-vs-async handler rationale is now documented (README and the
  registry module).
- Actuator endpoints `/info`, `/info/routes`, `/env`, `/health` and `/livenessprobe` -
  the engines' operational surface, for Kubernetes probes and one-dashboard monitoring
  of polyglot installations. Health check functions are normal registered functions
  speaking the engines' `type=info` / `type=health` interface contract, listed in
  `mandatory.health.dependencies` / `optional.health.dependencies` and called through
  the event bus. `/health` answers `UP` (200) / `DOWN` (400); `/livenessprobe` follows
  the most recent health outcome.
- `log.format` carries the engines' three presentations: `text` (default), `json`
  (pretty-printed) and `compact` (single-line JSONL for log aggregators). A sample
  `examples/resources/application.yml` demonstrates the resources convention and the
  well-known keys.
- Primitive in-process event bus - the single dispatch pipeline: one FIFO mailbox per
  route consumed by `instances` worker tasks (the parameter is faithful); RPC deliveries
  are ttl-bounded with a dead-work skip; drop-n-forget returns the 202-shape ack. The
  HTTP host and the local side of PostOffice are thin ingress adapters over it. No spill
  tier and no queue cap by design - back-pressure belongs to the engines' flows/graphs;
  a leaf host fails fast by deadline.
- PostOffice without an endpoint delivers locally (engine semantics): private routes are
  callable in-app while the wire keeps its 403; headers pass verbatim; the reply envelope
  shape is identical to the remote path.

Repository repurposed for the Mercury Composable **polyglot initiative** (August 2026).

- Lightweight Event-over-HTTP function host (`POST /api/event`) mirroring the engines'
  EventApiService semantics (x-ttl execution bound, x-async drop-n-forget with 202 ack,
  reserved header hygiene, `my_cid` → `my_correlation_id` injection, portable error
  contract, engine-identical error messages).
- Standard event envelope wire format codec, verified against the golden conformance
  vectors shared with the Java and Rust engines; compact format detected and rejected.
- `@preload` function registry with instance-count concurrency limits and private routes.
- `PostOffice` thin client with the engines' relay HTTP contract (octet-stream, x-ttl,
  x-no-stream, trace headers) for calling engine or peer polyglot functions.
- Minimalist utilities in engine-consistent style: `AppConfig` (`${ENV:default}`
  substitution, runtime overrides), logging in the reference log4j2 presentation
  (`log.format=text|json`, `LOG_LEVEL`), and distributed-trace context with reply
  annotations.
- `mercury-serve` developer runner.

The legacy Mercury language-pack implementation (pre-composable, websocket-based) remains
available in the git history prior to this version.
