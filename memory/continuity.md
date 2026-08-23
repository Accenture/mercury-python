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
- **status:** pre-release 0.1.0 (unreleased) — the Python member of the Mercury Composable
  polyglot initiative: a lightweight Event-over-HTTP function host + thin client, repurposed
  August 2026 (legacy language pack in git history only)
- **last_enabled:** 2026-08-22
- **last_session:** 2026-08-23 | agent: Claude Code (2026-08-23-031558)
- **last_review:** (none yet)
- **last_invariant_check:** (none yet)
- **repo:** ~/sandbox/mercury-python (origin: github.com/Accenture/mercury-python)

## Stack & Tools

> Canonical live home for the current stack — language version, dependencies, tool
> versions. `instructions.md` keeps only a high-level descriptor and points here.

- Python ≥ 3.10; build backend **hatchling**; package `mercury-composable` v0.1.0
  (unreleased), wheel from `src/mercury_composable`
  <!-- id: stack-python-hatchling | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->
- Runtime deps: `aiohttp` >=3.10,<4 (Event API host), `msgpack` >=1,<2 (envelope codec),
  `PyYAML` >=6,<7 (config); dev: `pytest` >=8 + `pytest-asyncio` >=0.23 (`asyncio_mode=auto`)
  <!-- id: stack-deps-aiohttp-msgpack | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->
- Developer runner: `mercury-serve` console script (`mercury_composable.cli:main`);
  examples run via `mercury-serve app.py --port <n>` with `-D` overrides
  <!-- id: stack-mercury-serve-cli | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->

## Architectural Invariants

> Hard constraints that must never change. These never decay (treated as `core`).

- **Wrapper only — no orchestration.** This package intentionally contains no event bus,
  no flows, no graphs and no orchestration; those live in the Mercury engines. It provides
  functions plus minimalist foundation utilities (README "Scope").
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
  the portable error contract.
  <!-- id: stateless-functions-contract | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: core | origin: 2026-08-22-171555 -->

## Key Decisions

- **Polyglot reboot (August 2026):** instead of re-porting the full composable foundation,
  this repo restarts as a lightweight Event-over-HTTP wrapper; the pre-composable
  websocket-based language pack remains in git history only (CHANGELOG 0.1.0).
  <!-- id: decision-polyglot-reboot | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->
- **Two-audience root fork (Eric, 2026-08-22):** root `AGENTS.md` routes contributors to
  `memory/PROTOCOL.md` and consumers (developers writing polyglot functions — the "AI
  grammar" path) to `README.md`, which carries the quick start, function contract, and
  wire-format guide.
  <!-- id: decision-consumer-fork-readme | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->

## Conventions

- **Quality gates (adopted 2026-08-23, Eric's IDE review round): ruff + basedpyright +
  pytest, config lives in pyproject.toml.** ruff: line-length 100, py310, isort extend.
  basedpyright: standard mode + reportMissingParameterType=error, TESTS INCLUDED (Eric's
  ruling — test signatures are annotated; handlers take `(dict[str, str], Body)`).
  `agent-skills/` excluded from both (tool-managed by agent-memory — style fixes belong
  upstream). Run: `uvx ruff check .` / `uvx basedpyright` / `.venv/bin/pytest -q`.
  Unused contract params take the underscore prefix; deliberate suppressions carry
  rationale comments (PyBroadException / noqa only where the rule actually fires).
  <!-- id: conv-python-quality-gates | created: 2026-08-23 | last_used: 2026-08-23 | uses: 1 | tier: working | origin: 2026-08-23-005709 -->
- Engine-mirrored configuration/logging/trace conventions (see the invariant above and
  `instructions.md`); GitHub flow with tests + a CHANGELOG entry per change
  (CONTRIBUTING.md).
  <!-- id: conv-github-flow-changelog | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->

## Open Threads

- [ ] (feature — design RATIFIED by Eric 2026-08-23; **IMPLEMENTED same day on
  `feature/primitive-event-bus`, commit `957d6b7`, tests 40/40 incl. the 8 bus pins +
  live wire proof (chain → private via bus; wire → private = 403); node twin `da8ce4c`
  39/39; PENDING Eric's PR gate**)
  **Primitive in-process event bus — the single dispatch pipeline.** Ratified shape:
  per-route FIFO mailbox (asyncio.Queue; node = queue + worker loops) with
  **instances = N worker tasks** (replaces the semaphore — the parameter becomes faithful);
  two operations only: `deliver` (RPC with ttl → 408 envelope; dead-work skip when the
  caller's future already expired) and `publish` (drop-n-forget, returns the 202-shape
  ack). The HTTP host becomes thin ingress (hygiene + 403-private, then bus); PostOffice
  WITHOUT an endpoint = local ingress reaching private AND public routes (engine
  semantics — `private` becomes faithful: in-app only); with endpoint = wire client,
  unchanged. **No spill tier / no queue cap (Eric's ruling): back-pressure belongs to the
  tier that owns recovery — the engine's flows/graphs; a leaf host fails fast by deadline
  rather than hoarding work.** Caveats recorded: in-memory = in-flight events die with the
  process (durability was never this layer's contract); send() has no ttl valve (engine
  parity; per-route cap only on field demand). Bus class stays INTERNAL (developers touch
  preload/PostOffice only). Scope fence amended: + "primitive in-process event bus, no
  orchestration/flows/persistence/broadcast". Pins: local RPC public+private, FIFO order,
  instances=2 concurrency, local 408, unregistered-route error, trace-chained
  hosted→local-private. README boundary statement: leaf-side composition here; workflow
  processing = Event Script / Knowledge Graph.
  <!-- id: thread-primitive-event-bus | created: 2026-08-23 | last_used: 2026-08-23 | uses: 1 | tier: working | origin: 2026-08-23-005709 -->

- [ ] (feature — Eric's directive 2026-08-23, **IMPLEMENTED same day on
  `feature/primitive-event-bus`, commits `56c002c` + `a674198` (IDE/Sonar round);
  node twin `342a854` + `7a8b12c`; PENDING Eric's PR gate together with the bus**)
  **Actuator endpoints — the engines' operational surface for Kubernetes PODs.**
  GET `/info`, `/info/routes`, `/env`, `/health`, `/livenessprobe` on the Event API port;
  shapes mirror the Rust engine's actuator (the approved minimalist port of Java
  `ActuatorServices`). Health check functions are normal registered functions speaking
  the engines' `type=info` / `type=health` interface contract (Eric's ruling), listed in
  `mandatory.health.dependencies` / `optional.health.dependencies` and called through the
  event bus; `/health` = UP 200 / DOWN 400 (Java parity); `/livenessprobe` follows the
  most recent health outcome. Engine formats verbatim (origin = UTC yyyyMMdd + 32-hex
  uuid per the Java reference; elapsed-time boundary quirks pinned). Documented deltas:
  no `/info/lib`, no XML, no info cache. One async `handle()` dispatcher (S7503-clean,
  mirrors the node twin). 10 pins + live demo drives on both wrappers.
  Relates [[thread-primitive-event-bus]]; serves [[bp-publish-interop-gate]].
  <!-- id: thread-actuator-endpoints | created: 2026-08-23 | last_used: 2026-08-23 | uses: 1 | tier: working | origin: 2026-08-23-031558 -->
> Mark completed items `- [x]` and leave them in place — the review sweeps them to
> the archive once older than `archive_window` sessions. Don't archive them by hand.

- [ ] **(blueprint) Publish behind the interop gate.** The wrapper is complete and green
  (tests incl. the shared golden vectors; cross-wrapper interop proven; the
  composable-example flow executed a Python function unchanged) but **unreleased** — the
  Vision's "releasable on its own cadence" is unmet until 0.1.0 ships to PyPI with
  protocol-compat versioning and the interop gate green per release (design P5/D6).
  Publishing itself is Eric-gated (ownership, cadence, supply-chain posture).
  → serves: vision-mercury-python
  <!-- id: bp-publish-interop-gate | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-173136 -->

- [x] **(vision-bootstrap)** Vision ratified by Eric, 2026-08-22 — drafted from the
  ratified polyglot design (D0–D8 + same-day refinements): tiny Event-over-HTTP wrapper,
  engines own orchestration, protocol-compat releases, the scope fence as non-goals.
  First Blueprint gap derived (publish behind the interop gate). Detail:
  2026-08-22-173136.
  <!-- id: ot-vision-bootstrap | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->
- [ ] **Dedicated consumer AI surface (optional).** The root fork points consumers at
  `README.md` for now (Eric, 2026-08-22). If the team wants a dedicated version-matched
  surface later (family pattern: mercury-composable's `system/AGENTS.md`), author it and
  retarget the fork's consumer link.
  <!-- id: ot-consumer-surface | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->

## User Preferences

(none recorded yet — record ONLY what the user explicitly states; never infer)

## Team / Members

(none recorded yet)
