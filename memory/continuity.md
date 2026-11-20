# Continuity — mercury-python

> Shared ground truth for project state across all agents and sessions.
> Update at the end of every session. Never delete — only archive (see `REVIEW.md`).
>
> Each fact carries a metadata footer in an HTML comment, maintained by the review
> ritual — invisible when rendered, read/written by agents:
> `<!-- id: kebab-id | created: YYYY-MM-DD | last_used: YYYY-MM-DD | uses: N | tier: active -->`
> See `.agent/schema.md` for the fields and `memory/decay-policy.md` for the windows.

---

## Project State

- **project:** mercury-python (PyPI: `mercury-composable`)
- **status:** **v4.12.30 PUBLISHED to PyPI 2026-11-16 and VERIFIED** (PR #58 merge `239e741`, the tag `v4.12.30` on the merge
  commit, the GitHub release 09:11:00Z as Latest, CI green on the tag commit; the wheel uploaded 09:22:39Z and the sdist
  09:22:40Z, `mercury-composable` 4.12.30 the latest; the sdist's and the wheel's 22 `src/mercury_composable` files identical
  to the tag's 22 (SHA-256), the wheel's `__version__` 4.12.30, three runtime dependencies and the extras `llm`, `bedrock`,
  `redis` and `mcp`). The 4.12.30 line carries the pack's half of the engines' agent loop (ADR-0033): the loop contract with
  the transcript stores, the Bedrock backend and the MCP binding (the catch-up [[pack-catch-up-4-12-25]], closed) — the
  Python member of the Mercury Composable polyglot initiative: a lightweight Event-over-HTTP function host + thin client,
  repurposed August 2026 (legacy language pack in tagged releases v1.11.39–v2.5.0).
- **last_enabled:** 2026-08-22
- **last_review:** 2026-11-19 | through 2026-11-19-060911.md (cadence — 18 sessions since 2026-11-13-030139; `refresh-metadata`
  refreshed 19 footers, tier changes 13; archived no fact - the two the count called faded are declaration gaps, re-affirmed
  ([[otel-forwarder-python]]: the trace-lineage session 2026-11-14-115310 reasoned from the forwarder's kind rule;
  [[conv-resources-next-to-the-application-file]]: the ask-person build 2026-11-19-054804 placed and loaded the extension's
  `resources` next to its file); swept 5 completed threads (contradiction-transcript-store-stateless 22,
  contradiction-vision-versioning-java-number 23, llm-helper-bedrock-iam 27, otel-forwarder-lockstep 35,
  reverify-invariants-20261103 21) → 2026-Q4; invariants not due (21 of 40); stalled none (bp-publish-interop-gate 34 of 40);
  contradictions none; live facts 30 → 25. Smoke test not run.)
  Prior: 2026-11-13 | through 2026-11-13-030139.md (on command — 5 sessions since 2026-11-03-010131, not yet due; 3 footers
  refreshed, 2 tier changes; archived 1 faded fact `decision-consumer-fork-system-agents` (25) + swept 1 thread
  `ot-consumer-surface` (25) → 2026-Q4; one stale statement corrected; invariants not due (3 of 40); live facts 24 → 22)
  Prior: 2026-11-03 | through 2026-11-03-010131.md (cadence — archived 1 faded + 1 superseded fact, swept 5 threads;
  invariants DUE → [[reverify-invariants-20261103]]; one Contradiction raised)
  Prior: 2026-09-23 | through 2026-09-23-004552.md (cadence — archived 1 faded fact `decision-polyglot-reboot` + swept 3
  completed threads → 2026-Q3; invariants not due (29 sessions, cadence 40, none yet); stalled none) · 2026-09-05 | through
  2026-09-05-211409
- **last_invariant_check:** 2026-11-03 | 2026-11-03-012600.md (COMPLETE - the first re-verify; Eric confirmed all four invariants and
  the Vision as walked in 2026-11-03-012056, two wordings refreshed, core stays 4; the next is due 40 sessions after this one)
- **repo:** ~/sandbox/mercury-python (origin: github.com/Accenture/mercury-python)

## Stack & Tools

> Canonical live home for the current stack — language version, dependencies, tool
> versions. `instructions.md` keeps only a high-level descriptor and points here.

- Python ≥ 3.10; build backend **hatchling**; package `mercury-composable` v4.12.30
  (PyPI 2026-11-16; the catch-up to the engines' 4.12.30 with the agent loop's pack half), wheel from
  `src/mercury_composable`
  <!-- id: stack-python-hatchling | created: 2026-08-22 | last_used: 2026-11-16 | uses: 8 | tier: archive-candidate | origin: 2026-08-22-171555 -->
- Runtime deps: `aiohttp` >=3.10,<4 (Event API host), `msgpack` >=1,<2 (envelope codec),
  `PyYAML` >=6,<7 (config); dev: `pytest` >=8 + `pytest-asyncio` >=0.23 (`asyncio_mode=auto`), `ruff` >=0.9, and
  the helper's backends for its tests (`anthropic[bedrock]`, `redis`, `fakeredis[lua]` >=2.26);
  optional extras, all for the LLM helper (`extensions/llm-helper`; the package itself stays SDK-free): `llm` = `anthropic`
  >=1,<2 (`pip install 'mercury-composable[llm]'`; `google-genai` was removed 2026-10-01 with the Gemini provider, PR #38),
  `bedrock` = `anthropic[bedrock]` >=1,<2 (the AWS Bedrock backend, PR #43), `redis` = `redis` >=5,<9 (the transcript
  store, PR #47), `postgres` = `psycopg[binary,pool]` >=3.2,<4 (the transcript audit store, 2026-11-17), and for the
  built-in agent tools (`extensions/agent-tools`): `graph` = `neo4j` >=6,<7 (the knowledge-graph tools over Bolt) and
  `documents` = `pypdf` >=6,<7 + `python-docx` >=1.1,<2 (`file.extract`), both 2026-11-17, and for the ask-a-person
  main flow (`extensions/ask-person`): `ask` = `PyJWT[crypto]` >=2.9,<3 (the reply page's `oauth2` mode), 2026-11-19
  <!-- id: stack-deps-aiohttp-msgpack | created: 2026-08-22 | last_used: 2026-11-19 | uses: 8 | tier: active | origin: 2026-08-22-171555 -->

## Architectural Invariants

> Hard constraints that must never change. These never decay (treated as `core`).

- **Wrapper only — no orchestration.** This package intentionally contains no flows, no
  graphs, no persistence and no pub/sub broadcast; orchestration lives in the Mercury
  engines. It provides functions, the primitive in-process event bus (route mailboxes +
  workers — dispatch, not orchestration; ratified 2026-08-23), and minimalist foundation
  utilities (README "Scope", amended with the bus). The LLM helper (`extensions/llm-helper/`) is an
  application on the package, not part of it, so its configurable transcript store leaves the
  package persistence-free (re-verified 2026-11-03).
  <!-- id: scope-wrapper-no-orchestration | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: core | origin: 2026-08-22-171555 -->
- **Standard wire format, proven by shared vectors.** The codec implements the standard
  event-envelope wire format, verified against the golden conformance vectors shared with
  the Java and Rust engines (`tests/vectors/vectors.json`); the classic compact format is
  detected and rejected with a teaching error.
  <!-- id: wire-standard-golden-vectors | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: core | origin: 2026-08-22-171555 -->
- **Engine-consistent conventions.** Config keys, `${ENV_VAR:default}` substitution, `-D`
  runtime overrides, `resources/` layout, and the reference log presentation mirror the
  engines so a polyglot installation stays uniform — divergence here is a bug, not a style
  choice. Event API semantics mirror EventApiService (x-ttl bound, x-async 202
  drop-n-forget, reserved-header hygiene, engine-identical error messages).
  <!-- id: engine-consistent-conventions | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: core | origin: 2026-08-22-171555 -->
- **Functions are stateless.** Anything a handler must keep belongs to the caller's flow
  model or state machine; intentional errors travel as `AppException(status, message)` —
  the portable error contract. **The LLM helper's `llm.chat` is configurable by design (Eric,
  2026-11-03):** a stateless chat, or, with `tools`, one turn of a multi-turn tool-use loop whose
  transcript lives in the configured transcript store (`llm.transcript.store`: `memory` for one
  process, `redis` shared by replicas; RFC-0011). That store is part of the function's configured
  contract, not an exception to this rule.
  <!-- id: stateless-functions-contract | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: core | origin: 2026-08-22-171555 -->

## Key Decisions

- **The OpenTelemetry forwarder is a zero-dependency port of the Rust engine's hand-written OTLP encoder, attached
  at the engines' extension route (Eric, 2026-09-22).** `mercury_composable.otel`: with `otel.forwarding=true` the
  bus hands every emitted trace dataset to `distributed.trace.forwarder` — delivered WITHOUT a trace, so the
  forwarder's own execution emits none (the engines' zero-tracing forwarder without a new flag) — and the built-in
  forwarder (private, two workers; an application's own function on the route wins) maps it to one span with the
  host's exact W3C ids and exports it over OTLP/HTTP protobuf, retrying transport failures and 408/429/502/503/504
  on the SDK backoff and re-reading the credential headers per export. Same `otel.*` keys as Java/Rust; deltas:
  `otel.exporter.otlp.connect.timeout` is honoured, scope `mercury-composable-python`. Rejected: the OTel SDKs as an
  optional extra (~ten packages against this package's three dependencies; Dynatrace takes protobuf only, so JSON
  was never an option). Shipped: PR #33, merge `697f5df4` (2026-09-22); Node twin next.
  **Kind rule since 2026-09-22 (PR #36 MERGED, `cbf1f7f6`):** SERVER iff the record's `service` is `http.request` — an
  engine edge's round-trip record — and every function execution is INTERNAL; a record's `from` no longer decides the kind
  (the engines' connected-span-tree fix, mercury-composable/mercury `fix/connected-edge-spans`).
  <!-- id: otel-forwarder-python | created: 2026-09-22 | last_used: 2026-11-19 | uses: 4 | tier: active | origin: 2026-09-22-164128 -->

- **The LLM helper is a dedicated function host on the Anthropic SDK: the engines stay LLM-free, the AI node is a bounded
  function, and one contract is proven by a vector file shared with the Node pack (Eric's rulings, 2026-10-01; PR #38, merge
  `c6cda5ab`).** `examples/llm-helper/llm_helper.py` serves `llm.chat` (one answer, optional JSON-schema structured output
  returned as `data`), `llm.stream` (the model's token batches over the multi-shot reply contract) and `llm.health` (a credential
  check, no network traffic). Claude only: the Gemini provider and `google-genai` are gone (the contract stays provider-neutral
  through `params.provider`, which accepts only `anthropic`). Default model `claude-opus-5-5` (`llm.model`), server-side refusal
  fallbacks on (`llm.fallbacks=off`), and a `Backend` seam (`get_backend()`) for the planned AWS Bedrock route
  (`llm-helper-bedrock-iam`). **The contract:** a `params` allowlist (`provider`, `model`, `max_tokens`, `timeout_ms`, `effort`,
  `stop_sequences`; anything else is a 400 and no sampling parameter is forwarded), a schema on `llm.stream` is a 400, **a reply
  that carries nothing usable is a 422 and never an empty success**, and every failure is an `AppException` whose message the
  Node twin repeats. **Progressive rendering is never buffered** (Eric's requirement): each batch leaves as its own segment the
  moment it arrives, a test pins it (the fake model refuses batch k until the caller holds batches 0..k-1), and the certified
  drives showed the helper and both engines add nothing (batches equal frames, 4-10 ms offset). The cadence a viewer sees is the
  API's and depends on the model (Haiku 4.5 about 25 ms continuous, Sonnet 5.5 about 350 ms bursts, Opus 5.5 about 600 ms
  bursts). **Opus 5.5 thinks first and thinking tokens count against `max_tokens`:** a few hundred can end with no text (the
  422), so the engines' demos ask for 2000, the helper's default is 16000, and Haiku (`llm.model: claude-haiku-4-5`) is the
  documented choice for smooth rendering; the default STAYS Opus (Eric, 2026-10-01). **Proof:** `tests/vectors/llm-helper-vectors.json`
  (byte-identical in mercury-nodejs, 63 cases against SDK fakes) plus `tests/test_llm_helper.py` (86 tests), and a live
  certification through the Java and Rust engines (`docs/test-reports/llm-helper-certification.md`: 124 model calls, every batch
  its own frame, every trace one tree). No prompt or completion text reaches a log; usage rides the trace record.
  **Extended 2026-10-14 (the AI SDLC epic's first sprint, Eric's direction; PR #43 merged `c25ba35`, the Node twin mercury-nodejs #111 merged `def1e3a`, both 2026-10-14):**
  `llm.backend: 'bedrock'` reaches the same models through the SDK's `AsyncAnthropicBedrockMantle` - the AWS credential chain through
  IAM, or a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK`; the region from `llm.aws.region` or `AWS_REGION` (none: `503 AWS region
  missing`); a bare model id prefixed `anthropic.`, an id already spelled for Bedrock kept; no server-side fallbacks (Bedrock offers
  none); an empty credential chain `503 AWS credential missing` by name; `llm.health` reporting `region`. `backend_for(name, client)`
  holds a route's shape, and the vector harness installs its fake under the backend a case names (66 cases). The `bedrock` extra adds
  boto3. **Driven live 2026-10-14 (PR #44 merge `03c5f57`, the Node twin mercury-nodejs #112 merge `0ece23f`, both MERGED 2026-10-14):** negative controls first, then chat, a structured
  verdict and a progressive stream on Haiku 4.5, and the Java engine's shipped triage graph and streaming relay in front of the
  helper - all through IAM, no vendor key. What the drive taught: Bedrock has TWO SURFACES an account may be enabled for separately,
  so `llm.aws.api` selects `mantle` (the default, the Anthropic-shaped endpoint, the `anthropic.` prefix) or `runtime` (the classic
  one, every model id sent as written - an inference profile id as the account lists it); model access is a one-time use case plus a
  per-model agreement (`CreateFoundationModelAgreement`), and a model generation can be gated at the account on top;
  `GetFoundationModelAvailability` tells the four gates apart; the SDK's
  empty-chain error reads `Could not resolve AWS credentials from session` (mapped by its words); Bedrock sends no `request-id`.
  Closed [[llm-helper-bedrock-iam]].
  **Extended 2026-11-02 (RFC-0011 build plan step 1; MERGED 2026-11-03, #46 merge `13cf394` and #47 merge `8f9ae34`):**
  the helper graduates to `extensions/llm-helper/` (Eric), and a request with `tools` is one turn of the engines' agent loop: the transcript stays
  in the helper's store (`llm.transcript.store`: `memory` or `redis`), the loop holds `conversation` and `turn`, a stale or
  ended turn is a 409, the model is pinned at the first turn and no server-side fallback is sent; a second shared vector
  file, `llm-loop-vectors.json`, pins it ([[llm-chat-loop-contract]]).
  <!-- id: llm-helper-app | created: 2026-10-01 | last_used: 2026-11-17 | uses: 7 | tier: archive-candidate | origin: 2026-10-02-001146 -->

- **The MCP binding serves one composable route per allowlisted MCP tool, and the server cannot change a tool under the
  graph (RFC-0011 / ADR-0033 step 4; built 2026-11-14, Python first).** `extensions/mcp-binding/mcp_binding.py` on the
  official MCP SDK 2.x: servers by name in `resources/mcp-servers.yml` (`stdio`, a subprocess of this process with the SDK's
  minimal environment plus the listed `env` references; or `http`, a streamable HTTP URL with `headers`); `${VAR}` resolves at
  boot and is never logged; each allowlisted tool maps to a route and pins the SHA-256 of its input schema in canonical JSON.
  Boot, inside the serving loop before the first request (`Platform.on_startup`), connects each server in its own task,
  lists its tools, and refuses to start by name when a tool is missing or its schema changed; `describe` prints the digests
  and a catalog row the developer reviews, because the model reads the graph's descriptions, never the server's. A route's
  reply is the structured content else the blocks' text; `is_error` is a 400 carrying the text (the loop returns it to the
  model), a timeout 408, a protocol failure 502, a lost session 503. The pure half (digest, verification, result mapping) is
  pinned by `tests/vectors/mcp-binding-vectors.json`, shared byte-identical with the Node twin; the transports by the fixture
  server `tests/fixtures/mcp_test_server.py`. **The shape (2026-11-14, Eric's direction):** every bound route is an alias of
  ONE `@preload` function, `mcp.tool` - private, bound to no tool - registered at boot with the server's `instances` through
  `default_registry.register`, the pack's counterpart of the engines' `Platform.register`; the function reads the route it
  was called as from `get_trace().route`, the pack's counterpart of the engines' read-only `my_route` header (the host strips
  `my_*` headers at ingress and adds none). A generic client - one route, the server and the tool encoded in the call - was
  suggested and set aside the same day: ADR-0033 rejects it as alternative (c), since the catalog cannot certify a target
  hidden in the call; the certifiable form is an optional `headers` map on the catalog row, an engine change in both
  engines, not taken. **How to apply:** a new MCP tool for an agent is a server entry and an allowlist
  row here plus a catalog row on the graph, never engine code; a schema change is a digest the developer re-reviews; a
  credential is a reference in the server file, resolved from this process's environment.
  <!-- id: mcp-binding-route-per-tool | created: 2026-11-14 | last_used: 2026-11-14 | uses: 1 | tier: superseded | superseded-by: mcp-binding-function-per-tool | origin: 2026-11-14-073523 -->

- **A tool of an MCP server is a composable function of the binding, declared in code (Eric's review 2026-11-16; the
  engines' `draft-design-specs/agent-tool-use.md`; supersedes the allowlist-row shape of 2026-11-14).**
  `extensions/mcp-binding/mcp_binding.py` is a library and a function host: `bind(server, tool, schema_sha256=...)` declares
  a binding and returns the tool as an async callable, and an ordinary `@preload` function calls it - the extension's own
  `mermaid.render` binds the one tool of the public Mermaid Chart MCP endpoint named in the default server file, so the
  binding serves a tool out of the box. The server file keeps what is environment-specific (transport, command or URL,
  credential references, timeout) and refuses a `tools` allowlist by name; boot connects every server a function binds a tool
  of, lists its tools and stops the application by name when a bound server is not configured, a tool is not offered or its
  digest changed; a configured server nobody binds is not connected. `mcp.tool` is no longer a route; `mcp.health` stays. A
  route names the capability, never the transport: a tool may start as an MCP tool and later be implemented another way, and
  the graph must not know. The shared vectors are version 2 (`verify_cases` over declared bindings), byte-identical with the
  Node twin. **How to apply:** a new tool is a `bind` line, a few-line `@preload` function, a catalog row on the graph and an
  Event-over-HTTP entry; a schema change is a digest re-reviewed next to the function; a new server is a server-file entry;
  never a route in a YAML file and never engine code.
  <!-- id: mcp-binding-function-per-tool | created: 2026-11-16 | last_used: 2026-11-19 | uses: 5 | tier: active | origin: 2026-11-16-043838 | supersedes: mcp-binding-route-per-tool -->

- **A traced call into the pack leaves the records a Java or Rust callee leaves, so a span lineage is continuous from an
  engine into the pack (2026-11-14; Eric's criterion: for OpenTelemetry to be certified, the span lineage ought to be
  continuous end to end).** The live drive of the engines' agent loop through both packs found the pack legs absent from the
  trace: the calling engine's `async.http.request` leg is in `skip.rpc.tracing` by default, and the host suppressed the
  worker-side record of every RPC-served execution with no callee-side record to replace it, so `llm.chat` and
  `diagram.render.mcp` showed only as time inside the engine's `llm.run` and `diagram.render`. Now `server.py` records, for a
  traced call at `/api/event`, `http.request` (`POST /api/event`, the edge's round trip; parent the caller's span from the
  envelope, else the `traceparent` header; the backend's SERVER span), `event.api.service` (the ingress; parent the edge)
  and, for an RPC, the function's round-trip record (parent the service; the responder's span from the reply, `exec_time`
  and `round_trip`); the relayed envelope carries the service's span, so a drop-n-forget or streaming function parents onto
  it; a refusal or a timeout leaves the edge record with its status; an untraced call records nothing. `bus.py` puts the
  execution's span on the reply (`WorkerHandler` parity) and `client.py` records a local RPC round trip as `InboxBase` does;
  the worker-side record of an RPC-served execution stays suppressed. Re-driven the same day on the `diagram-assistant`
  graph: 20 records per run, every parent owned by a record, in both packs (the engines'
  `docs/test-reports/agent-loop-trace-lineage.md`). PR #53 MERGED 2026-11-14 (merge `7d75573`); the Node twin is
  mercury-nodejs #120. **How to apply:** the rule is who emits a
  record, never whether one exists - every traced execution leaves exactly one; before changing a host or client path, ask
  which side records it in the Java engine and mirror that side; a lineage check parses every record of the trace from every
  process and verifies each parent is a span some record owns.
  <!-- id: trace-records-engine-parity | created: 2026-11-14 | last_used: 2026-11-14 | uses: 1 | tier: archive-candidate | origin: 2026-11-14-115310 -->

- **The transcript audit store: every model turn recorded by a composable function behind a local event, off the
  turn's path, into PostgreSQL beside the working store (RFC-0012 in mercury-composable, Eric's rulings of 2026-11-17;
  built here as PR #59, MERGED 2026-11-17, merge `a2f53dfe3`).** `llm.transcript.audit: postgres` registers `transcript.audit` at boot - private, ONE
  instance, so its mailbox orders a conversation's writes and its check without a lock. The working record is version
  2: beside the messages it carries each turn's facts (trace id and path, correlation id, UTC bounds, status, stop
  reason, request id, usage, `messages: [first request message, response]`), the first turn's system prompt, catalog,
  correlation id and the loop's `x-graph-id` and `x-node`, and the closing `retention`, all saved inside the
  compare-and-set. After a compare-and-set accepted turn N, `llm.chat` sends `{conversation, turn: N}` through a local
  `PostOffice().send`; a closing turn adds `{conversation, check: true}` behind it and the record waits with
  `llm.transcript.audit.grace` (`1d`) instead of `retention`. The function reads the record, writes the rows the store
  lacks in one transaction (the conversation row upserted, the turn rows `ON CONFLICT DO NOTHING`, the totals
  recomputed from the turn rows) and marks the record `audited` - a third hash field the compare-and-set never
  touches; a check releases a closed record to its `retention` first and marks it after, a closing write leaves both to
  its check, an open record is only filled. A sweep (`llm.transcript.audit.sweep.ms`, 60 s, `SET NX PX` on
  `transcript:audit:sweep`) sends a check for every record whose mark lags. Failures are logged by type and SQLSTATE,
  never by message (a constraint violation quotes the row), and never refuse a turn. The rows: `request` is the array
  of messages the turn sent, `response` the array of assistant blocks, `calls` the normalized tool calls; JSON travels
  as text cast to `jsonb` and stamps as text cast to `timestamptz`, so the Node twin runs the same statements.
  `MemoryAuditStore` keeps the reference semantics in the module for tests and parity, not configurable. The schema
  (`schema/postgres.sql`, `purge.sql`) is byte-identical across the packs; retention and purge are the installation's
  (`expires_at` from `llm.transcript.audit.retention`, `365d`; `dev/audit-purge.sh` the sample). **How to apply:** a
  new audit fact is a field of the turn's facts written in `_turn_facts` and a column in both packs' schema and the
  published page, never a second key or a second write; anything the audit must know at the end of a run travels in
  the record, since the check reads nothing else; the shipped yml takes the database credential from `${ENV}` references
  (since #60 a `-D` value resolves them too). **Driven live 2026-11-17** (mercury-composable
  `docs/test-reports/transcript-audit-live-drive.md`): every turn recorded off the turn's path, a stopped database repaired by
  the sweep; #62 added a server-side `statement_timeout` from `.timeout.ms`, because a paused database stalls a pooled
  statement where a stopped one fails it.
  <!-- id: transcript-audit-store-python | created: 2026-11-17 | last_used: 2026-11-17 | uses: 5 | tier: active | origin: 2026-11-17-080036 -->

- **Ask a person, the main flow: the pack carries an agent loop's wait to a person and brings the decision back, knowing
  no channel and keeping no store (RFC-0014 in mercury-composable, build plan step 1; Eric's concept and rulings R1-R8 of
  2026-11-19; built here 2026-11-19).** `extensions/ask-person/ask_person.py` serves `person.ask`: the graph's hold path
  calls it before `graph.suspend` with the graph id, a description, the correlation id, the turn, the waiting calls as the
  loop returned them and the suspension's ttl; the function issues a signed, stateless ticket (`v1.<payload>.<mac>`,
  HMAC-SHA256 over the canonical JSON of `{g, c, t, k, e}`, a previous key verifying after a rotation), builds the resume
  link `<ask.reply.url>/ask/<ticket>` and sends the whole message as one event to every route in `ask.channels` - the
  agent-question app's intake, a chat poster, a mail sender, a topic publisher - best effort, a failure logged by type and
  never failing the hold; `kind: advice` is a notification with no ticket and no link. The link opens the wait on the
  pack's reply page (`GET /ask/{ticket}`, read from the engine by the reference graph's re-advice, never resuming); only
  `POST /ask/reply` with `{ticket, decisions}` resumes, through the `ask.graphs` allowlist (graph id to engine entry,
  never an address a message told it), with `[who at time]` prefixed to each tool call's note so the audit store carries
  who decided. Identity is `ask.reply.auth`: `none` in development, `proxy` (the gateway's trusted header) or `oauth2`
  (PyJWT against the issuer's keys), and a channel app's bearer with `decided_by`. The host gained `Platform.add_routes`
  for an extension's own pages and streams. Pinned by `tests/vectors/ask-person-vectors.json` (72 cases), shared with the
  Node twin, and 100 tests. **How to apply:** the engine waits and the pack delivers and collects - never make a tool wait
  for a person; a new channel is a route in `ask.channels`, never code here; a reply path is always the link; the
  agent-question app (step 3) is a separate application that receives the event and automates the click.
  **Round 4 (Eric's sprint of 2026-11-20; PR #74, MERGED 2026-11-20):** the main flow keeps the question record - the main flow records
  and the channels deliver. `question.audit`, a private function of this process registered at boot with
  `ask.audit.instances` workers behind local requests, writes every wait and advice `person.ask` sends (before the
  channels are told) and every answer the reply endpoint brings back (the stamped decisions, who decided, when, the
  engine's outcome) to the `question` table of the audit database, order-independently: a wait upserts on its key unless
  an older message lands late, an advice appends with no state, the first accepted answer stands; a sweeper expires
  open waits. The contract `QuestionStore` is published - PostgreSQL out of the box (`ask.audit: postgres`,
  `schema/postgres.sql` version 2), memory for the tests (the default, with a boot warning), another database by the
  contract. Identity is `none` or `oauth2` - `proxy` removed, the field's installations use 3-legged SSO and OAuth 2.0
  access tokens - and the reply page signs the person in by Authorization Code with PKCE (a shell, `GET /ask/config`,
  the wait read from `GET /ask/{ticket}/wait`). **How to apply (amended):** "keeping no store" means no state of the
  wait - the record is an audit log, never consulted to render a wait; the agent-question app reads it.
  <!-- id: ask-person-main-flow | created: 2026-11-19 | last_used: 2026-11-19 | uses: 1 | tier: working | origin: 2026-11-19-054804 -->

- **The agent-question app is the first channel of RFC-0014: a store-and-forward inbox for the person an agent asks,
  a standalone application on the package that the main flow knows nothing of (build plan step 3; built here
  2026-11-19).** `extensions/agent-question/agent_question.py` serves `agent.question.inbox`: a wait (an approval or
  a question) is upserted on `(graph_id, correlation_id, turn)` and an advice appended, in memory or in PostgreSQL
  (`question.inbox: postgres`, the audit store's connection keys, `schema/postgres.sql` with the change sequence
  `question_inbox_seq` and the partial unique index; `purge.sql` the installation's retention). The person's side runs
  under the host through `Platform.add_routes`: the page, the replay `GET /questions?since=<seq>` a reconnecting device
  calls first, the stream `GET /questions/events` whose `Last-Event-ID` is the same cursor, the automated click
  `POST /questions/{id}/decide` to the link's own base under the app's bearer with `decided_by`, and `seen` for an
  advice; states open, answered, expired, seen; a sweeper expires waits past their expiry. Identity `question.auth`:
  `none`, `proxy`, `oauth2`. Pinned by `tests/vectors/agent-question-vectors.json` (30 cases) and 48 tests; the Node
  twin follows. **Driven live 2026-11-19 (RFC-0014 step 5, mercury-composable `docs/test-reports/ask-a-person-live-drive.md`):**
  the chain held on every scenario; one defect fixed here - two devices deciding one card at the same moment both reach
  the engine (it resumes at most once), and the row took whichever outcome landed last, so a late `nothing waiting` could
  overwrite the accepted decision: a decision's write now applies when the row is still open or when the engine accepted
  it (`answered` always wins), the losing device gets the row as it stands, vectors version 2 (31 cases). **How to apply:**
  the app is one channel among equals - a chat, mail or a topic adapter takes the same
  event and keeps its own buffer; the app never talks to the engine, only to the reply endpoint the link names; a row is
  never deleted by the app; a new card state is a change to `state_after` and the schema's check constraint in both
  packs; a wait decided elsewhere stays open on a card until a device tries it (410 → expired) or it expires - the
  acknowledgement advice beside it tells the person.
  **The person's side (Eric, 2026-11-19):** the page is one vanilla file, byte-identical in both packs - since 2026-11-19
  a sticky header with the count of open cards, *Waiting for you* and a scrollable *History* panel. This app is the
  backend in both packs; the front-end that replaces the page, a small React app pointed at either backend, is built and
  maintained in the Node pack only (mercury-nodejs thread `agent-question-react-ui`), and this pack points to it - never a
  second front-end here.
  **Round 4 (Eric's sprint of 2026-11-20; PR #75, MERGED 2026-11-20):** the app reads the question record and writes nothing - the backend
  of the React UI. Its inbox is gone with its schema; the main flow's recorder writes the `question` table and this app
  reads it and the transcript audit tables under a reader's role (`question.store: postgres`; `memory` for the tests,
  where nothing writes it at run time). The intake wakes the feed, which polls the record's change sequence
  (`question.poll`, 1 s) and tells every connected device; `GET /questions?before=<seq>` pages the history newest first
  for the infinite scroll, `GET /transcripts` and `GET /transcripts/{conversation}` browse the audit store,
  `GET /questions/config` tells the UI how to sign in; the click answers with the row as the recorder left it; `seen`
  and the sweeper are gone; identity `none` or `oauth2` (`proxy` removed). Vectors version 3 (`intake_cases`,
  `click_cases`, `feed_cases`). **How to apply (amended):** this app never writes the record - a new fact of a row is
  the recorder's and the schema's; a new read for the UI is an endpoint here, in both packs and in the vectors.
  **2026-11-20 (PR #76):** the generated page is gone - `GET /questions/app` retired; the React UI's bundle (mercury-nodejs
  `extensions/agent-question-ui`, #141) is committed in `resources/public` and served at `/` by the host
  ([[static-content-resources-public]]), the engines' actuator endpoints beside it for Kubernetes probes (Eric).
  <!-- id: agent-question-app | created: 2026-11-19 | last_used: 2026-11-19 | uses: 1 | tier: working | origin: 2026-11-19-071444 -->

- **The host serves static web content from `resources/public` next to the application file - the engines' `public`
  folder (Eric, 2026-11-20; PR #73, the Node twin mercury-nodejs #138).** `static.html.folder` is honoured as the Java
  engine's key with the Rust engine's location as its default, under the resources rule
  ([[conv-resources-next-to-the-application-file]]): a GET or HEAD that no host path, extension route or function claims
  is answered with the file it names, `index.html` for a folder (so a folder's `index.html` replaces the embedded
  landing page at `/`), the content type from the engines' table by extension, an ETag with 304 on `If-None-Match`,
  `Cache-Control: no-cache` for HTML; a hidden segment, a link out of the folder or anything that is not a regular file
  is not found, in the engines' error shape; `file:` prefixes an absolute path, an empty value turns the service off;
  `AppConfig.app_dir` names the application file's folder. Withdraws the 2026-08-24 note that the wrappers serve no
  static files (session 2026-08-24-015208): a pack application now ships a web front-end beside its functions as the
  engines' Playground does - the first is the agent-question app's React UI, built in the Node pack and deployed into
  both packs' `resources/public`, served by each backend from its own origin, so no CORS ([[bp-question-record-and-ui]]).
  Not ported: the Java engine's static-content filter, its `no-cache-pages` list and its `mime.types` section.
  **How to apply:** a web bundle goes into `resources/public` next to the application file and nothing else is
  configured; a host path or an extension route always wins over a file of the same name; a relative folder is never
  the working directory's.
  <!-- id: static-content-resources-public | created: 2026-11-20 | last_used: 2026-11-20 | uses: 1 | tier: working | origin: 2026-11-20-075621 -->

## Conventions

- **Quality gates (adopted 2026-08-23, Eric's IDE review round): ruff + basedpyright +
  pytest, config lives in pyproject.toml.** ruff: line-length 100, py310, isort extend.
  basedpyright: standard mode + reportMissingParameterType=error, TESTS INCLUDED (Eric's
  ruling — test signatures are annotated; handlers take `(dict[str, str], Body)`).
  `agent-skills/` excluded from both (tool-managed by agent-memory — style fixes belong
  upstream). Run: `uvx ruff check .` / `uvx basedpyright` / `.venv/bin/pytest -q`.
  Unused contract params take the underscore prefix; deliberate suppressions carry
  rationale comments (PyBroadException / noqa only where the rule actually fires).
  <!-- id: conv-python-quality-gates | created: 2026-08-23 | last_used: 2026-11-19 | uses: 10 | tier: active | origin: 2026-08-23-005709 -->
- Engine-mirrored configuration/logging/trace conventions (see the invariant above and
  `instructions.md`); GitHub flow with tests + a CHANGELOG entry per change
  (CONTRIBUTING.md).
  <!-- id: conv-github-flow-changelog | created: 2026-08-22 | last_used: 2026-11-17 | uses: 6 | tier: archive-candidate | origin: 2026-08-22-171555 -->
- **Each example app lives in a folder of its own, with a README and its own `resources/` (Eric, 2026-10-01; PR #38).**
  `examples/demo-app/` (the minimal polyglot app: `hello.python`, `hello.declarative`, `hello.chain`, `hello.sync.chain`,
  `hello.tokens`, the private `demo.suffix.helper` and `demo.health`) and `examples/llm-helper/`; run one as
  `mercury-serve examples/<app>/<file>.py` (the sample config is read from the `resources` folder next to the app file). The
  Node pack mirrors the layout. The demo and the helper share the default port 8086, so give one of them another with
  `-Drest.server.port` to run both. The demo stays provider-free and credential-free: no LLM code goes into it.
  **2026-11-02 (Eric):** the LLM helper graduates from an example to an extension, `extensions/llm-helper/` (same folder
  shape: a README and its own `resources/`), so `examples/` holds the demo app; merged as #46 (`13cf394`, 2026-11-03),
  and the Node pack followed the same day (mercury-nodejs #114, `62958b5`).
  <!-- id: examples-one-folder-per-app | created: 2026-10-01 | last_used: 2026-11-17 | uses: 6 | tier: active | origin: 2026-10-02-001146 -->

- **The `resources` folder is resolved next to the application file - the file `mercury-serve` is given, or the script Python
  ran (`python app.py`) - and the working directory is never searched (Eric's rule, 2026-10-14; PR #45 merge `a3c5a26`).** The engines' classpath
  convention: the configuration travels with the application and does not depend on the launch directory. `AppConfig(path, argv,
  app_dir)` resolves `resources/application.yml|yaml|properties` under `app_dir`, defaulting to `main_script_dir()`
  (`__main__.__file__`; an interactive session has no script and loads nothing); the CLI passes the app's folder; `--config` names
  another file; the companion `app-log-context.yaml` lives in `resources_dir`, the one folder beside an explicit file or next to
  the app; `source` is the absolute path found and the startup log reports `Configuration: <path>`. Found when a direct
  `python llm_helper.py` from the repository root started unconfigured during the Bedrock drive ([[llm-helper-app]]); pinned by
  subprocess tests that run from a decoy working directory. Lock-step with the Node pack (`process.argv[1]`, mercury-nodejs #113 merge `f4132d4`).
  Upgrade: a layout that kept `resources/` in the working directory with the app elsewhere passes `--config` or moves the folder.
  <!-- id: conv-resources-next-to-the-application-file | created: 2026-10-14 | last_used: 2026-11-19 | uses: 2 | tier: active | origin: 2026-10-14-015420 -->

- **The legacy language pack's releases v1.11.39 to v2.5.0 live here as tags and GitHub releases; their branches are gone
  (Eric, 2026-11-13).** Before the August 2026 repurposing this repository held the legacy Mercury language pack for
  Python, whose releases were cut as `release/*` branches; tags began at `v4.12.0`. Each branch tip now has an annotated
  tag `vX.Y.Z`, the tagger date the release commit's, and a GitHub release titled with the tag, its notes the version's
  CHANGELOG section after a line saying it was published retroactively, not marked latest: 13 tags and releases, 17 of
  each in all, `v4.12.21` still Latest. 1.11.39's section is read from the CHANGELOG at `v1.12.4` (added after its tag);
  1.12.31 never had one, and its notes say so. The 13 branches were then deleted; GitHub keeps only `main` and
  `gh-pages`. Twins: mercury-composable `historical-releases-tags-and-pages`, mercury
  `historical-releases-tags-and-pages-rust`. **How to apply:** find a legacy release by its tag or release page, never a
  branch; a release published after the fact passes `--latest=false`.
  <!-- id: historical-releases-tags-and-pages-python | created: 2026-11-13 | last_used: 2026-11-13 | uses: 1 | tier: archive-candidate | origin: 2026-11-13-025101 -->

## Open Threads

> Open Threads live **one per file** in `memory/open-threads/` (`thread-<id>.md`;
> filename = the thread's fact id) so concurrent thread work never merge-conflicts
> (v4.39.0). List that directory to see them; unchecked `- [ ]` threads are the live
> workstreams and never decay. Mark a completed thread `- [x]` in its file and leave
> it — the review sweeps it to the archive once older than `archive_window` sessions.
> Don't archive by hand. See `.agent/schema.md`.


## User Preferences

(none recorded yet — record ONLY what the user explicitly states; never infer)

## Team / Members

(none recorded yet)
