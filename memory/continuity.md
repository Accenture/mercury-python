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
- **last_session:** 2026-08-22 | agent: Claude Code (2026-08-22-171555)
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

- Engine-mirrored configuration/logging/trace conventions (see the invariant above and
  `instructions.md`); GitHub flow with tests + a CHANGELOG entry per change
  (CONTRIBUTING.md).
  <!-- id: conv-github-flow-changelog | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: working | origin: 2026-08-22-171555 -->

## Open Threads

> Mark completed items `- [x]` and leave them in place — the review sweeps them to
> the archive once older than `archive_window` sessions. Don't archive them by hand.

- [ ] **(vision-bootstrap)** Confirm the Vision in `memory/vision.md` — set the target /
  success criteria / non-goals; then derive the Blueprint.
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
