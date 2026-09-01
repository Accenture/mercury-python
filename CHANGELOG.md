# Changelog

## Unreleased

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
