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
- **status:** v4.12.0 merged to main (the progressive-rendering milestone, engine lock-step
  version line; PyPI publish still pending) — the Python member of the Mercury Composable
  polyglot initiative: a lightweight Event-over-HTTP function host + thin client, repurposed
  August 2026 (legacy language pack in git history only)
- **last_enabled:** 2026-08-22
- **last_session:** 2026-08-30 | agent: Claude Code (2026-08-30-045556)
- **last_review:** (none yet)
- **last_invariant_check:** (none yet)
- **repo:** ~/sandbox/mercury-python (origin: github.com/Accenture/mercury-python)

## Stack & Tools

> Canonical live home for the current stack — language version, dependencies, tool
> versions. `instructions.md` keeps only a high-level descriptor and points here.

- Python ≥ 3.10; build backend **hatchling**; package `mercury-composable` v4.12.0
  (merged 2026-08-30, engine lock-step version line; PyPI publish pending), wheel from
  `src/mercury_composable`
  <!-- id: stack-python-hatchling | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->
- Runtime deps: `aiohttp` >=3.10,<4 (Event API host), `msgpack` >=1,<2 (envelope codec),
  `PyYAML` >=6,<7 (config); dev: `pytest` >=8 + `pytest-asyncio` >=0.23 (`asyncio_mode=auto`)
  <!-- id: stack-deps-aiohttp-msgpack | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->
- Developer runner: `mercury-serve` console script (`mercury_composable.cli:main`);
  examples run via `mercury-serve app.py --port <n>` with `-D` overrides
  <!-- id: stack-mercury-serve-cli | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->

## Architectural Invariants

> Hard constraints that must never change. These never decay (treated as `core`).

- **Wrapper only — no orchestration.** This package intentionally contains no flows, no
  graphs, no persistence and no pub/sub broadcast; orchestration lives in the Mercury
  engines. It provides functions, the primitive in-process event bus (route mailboxes +
  workers — dispatch, not orchestration; ratified 2026-08-23), and minimalist foundation
  utilities (README "Scope", amended with the bus).
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

- [x] (feature — **MERGED 2026-08-30 as
  [PR #21](https://github.com/Accenture/mercury-python/pull/21) true merge `bfca7e4`
  carrying `d50986a`; tree verified; v4.12.0 milestone, all four repos lock-step)
  **The progressive-rendering round: event streaming (engines' envelope-mode SSE
  contract, reply_to bus mechanism, stream/stream_to consumers), business
  correlation-id continuity, full span lineage with the engines' distributed-trace
  dataset on stdout, app-log-context with the packaged default template, sender
  attribution.** Lessons: detach long-lived workers from the creating task's
  contextvars; install the log-context config before its own warning logs; RPC legs
  emit no dataset (engine parity). origin: 2026-08-30-045556.
  <!-- id: ot-streaming-telemetry-round-20260830 | created: 2026-08-30 | last_used: 2026-08-30 | uses: 1 | tier: working | origin: 2026-08-30-045556 -->

- [x] (P4 docs — **SHIPPED and LIVE 2026-08-24**, same day as plan ratification)
  **Documentation site "Composable for Python"** — engine Material theme, 13 files
  incl. the one-page AI agent guide + llms.txt; live at accenture.github.io/mercury-python.
  [PR #20](https://github.com/Accenture/mercury-python/pull/20) merge `0bc97f7` carrying
  `a8ebde2` (tree verified, branches deleted); ci.yml maiden run green — the wrapper-CI
  gap is closed. Lesson: mermaid on a new site verifies structurally against the
  engine's live pages when the sandbox can't render CDN JS. Remaining P4 = engine
  repos (polyglot chapter, ADR-0016, interop extension). Relates
  [[bp-publish-interop-gate]]. origin: 2026-08-24-152125
  <!-- id: thread-docs-site | created: 2026-08-24 | last_used: 2026-08-24 | uses: 1 | tier: working | origin: 2026-08-24-152125 -->

- [x] (feature — Eric's three loose ends 2026-08-24; **MERGED same day as
  [PR #19](https://github.com/Accenture/mercury-python/pull/19), true merge `035b636`
  carrying `da60593` (tree verified, branches deleted both ends); node twin merged in
  its quality PR #88**) **Actuator polish: engine-parity index page, pretty
  JSON, host error shape.** `GET /` = the engines' minimal Welcome page (embedded — no
  static file service by design); actuator JSON pretty-printed (SimpleMapper default);
  unknown paths/non-GET → `{"status", "message", "type": "error"}` with
  `Resource not found` (SimpleHttpUtility signature, Java insertion order). Live-proven
  byte-symmetric with node. Relates [[thread-actuator-endpoints]].
  <!-- id: thread-actuator-polish | created: 2026-08-24 | last_used: 2026-08-24 | uses: 1 | tier: working | origin: 2026-08-24-015208 -->

- [x] (feature — Eric's directive 2026-08-24 after ratifying the sync-vs-async design;
  **MERGED same day as [PR #18](https://github.com/Accenture/mercury-python/pull/18),
  true merge `1888a48` carrying branch head `af039db` (4 commits: bridge + import hoist
  + static _run_sync + unshadow); tree verified identical, branches deleted both
  ends**) **Sync bridge: `PostOffice.request_sync()/send_sync()` from plain-def
  handlers.** The bus stamps the host loop into a contextvar before dispatching sync
  handlers; the bridge submits the same coroutines via `run_coroutine_threadsafe`,
  blocking only the worker thread. Durable subtlety: **contextvars do not cross
  run_coroutine_threadsafe** — the bridge re-establishes the caller's TraceInfo inside
  the submitted task (same object), keeping the trace chain unbroken. Teaching errors:
  on-loop call → "await request() instead"; off-host → use asyncio.run. Rationale docs
  (requests/NumPy named, virtual-threads analog) in README + registry.py per Eric.
  4 pins + hello.sync.chain wire proof. Relates [[thread-primitive-event-bus]].
  <!-- id: thread-sync-bridge | created: 2026-08-24 | last_used: 2026-08-24 | uses: 1 | tier: working | origin: 2026-08-24-004715 -->

- [x] (feature — RATIFIED + IMPLEMENTED + **MERGED 2026-08-23 as
  [PR #17](https://github.com/Accenture/mercury-python/pull/17), true merge `f38ac17`
  carrying branch head `1931f01`; tree verified identical, branches deleted both ends;
  one PR with [[thread-actuator-endpoints]]**) **Primitive in-process event bus — the
  single dispatch pipeline.** `instances`/`private` faithful; deliver + publish only; the
  HTTP host and local PostOffice = thin ingress adapters. Durable ruling: NO spill tier /
  NO queue cap — back-pressure belongs to the engines' flows/graphs (scope fence:
  instructions.md). Full design, pins and wire proofs: origin log.
  <!-- id: thread-primitive-event-bus | created: 2026-08-23 | last_used: 2026-08-23 | uses: 1 | tier: working | origin: 2026-08-23-005709 -->

- [x] (feature — Eric's directive, IMPLEMENTED + **MERGED 2026-08-23 in the same
  [PR #17](https://github.com/Accenture/mercury-python/pull/17) as the bus**)
  **Actuator endpoints — the engines' operational surface for Kubernetes PODs.**
  /info, /info/routes, /env, /health, /livenessprobe; health check functions speak the
  engines' `type=info`/`type=health` contract through the bus; UP 200 / DOWN 400;
  liveness follows the last health outcome. Durable lesson: engine `log.format` json =
  PRETTY-printed, compact = single-line JSONL (the JsonAppender/CompactAppender pair).
  Detail: origin log. Relates [[thread-primitive-event-bus]].
  <!-- id: thread-actuator-endpoints | created: 2026-08-23 | last_used: 2026-08-23 | uses: 1 | tier: working | origin: 2026-08-23-031558 -->
> Mark completed items `- [x]` and leave them in place — the review sweeps them to
> the archive once older than `archive_window` sessions. Don't archive them by hand.

- [ ] **(blueprint) Publish behind the interop gate.** The wrapper is complete and green
  and now versioned **v4.12.0 on main** (the milestone merge, 2026-08-30 — the version
  aligns with the engine lock-step line, superseding the 0.1.0 plan), with the interop
  gate green (the progressive-rendering interop report). The Vision's "releasable on its
  own cadence" is unmet until it ships to PyPI; publishing itself stays Eric-gated
  (ownership, cadence, supply-chain posture; design P5/D6).
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
