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
- **status:** **v4.12.1 PUBLISHED to PyPI 2026-09-01** (`pip install mercury-composable` —
  the first public package; tag v4.12.1; the 4.12.1 line carries the llm.chat/llm.stream
  AI nodes and the publication metadata incl. the constrained sdist) — the Python member
  of the Mercury Composable polyglot initiative: a lightweight Event-over-HTTP function
  host + thin client, repurposed August 2026 (legacy language pack in git history only)
- **last_enabled:** 2026-08-22
- **last_review:** (none yet)
- **last_invariant_check:** (none yet)
- **repo:** ~/sandbox/mercury-python (origin: github.com/Accenture/mercury-python)

## Stack & Tools

> Canonical live home for the current stack — language version, dependencies, tool
> versions. `instructions.md` keeps only a high-level descriptor and points here.

- Python ≥ 3.10; build backend **hatchling**; package `mercury-composable` v4.12.0
  (merged 2026-08-30, engine lock-step version line; PyPI publish pending), wheel from
  `src/mercury_composable`
  <!-- id: stack-python-hatchling | created: 2026-08-22 | last_used: 2026-09-01 | uses: 3 | tier: active | origin: 2026-08-22-171555 -->
- Runtime deps: `aiohttp` >=3.10,<4 (Event API host), `msgpack` >=1,<2 (envelope codec),
  `PyYAML` >=6,<7 (config); dev: `pytest` >=8 + `pytest-asyncio` >=0.23 (`asyncio_mode=auto`);
  optional extras: `llm` = `anthropic` >=1,<2 + `google-genai` >=2,<3 (the AI-node provider
  SDKs — `pip install 'mercury-composable[llm]'`, added 2026-09-01)
  <!-- id: stack-deps-aiohttp-msgpack | created: 2026-08-22 | last_used: 2026-09-01 | uses: 2 | tier: active | origin: 2026-08-22-171555 -->
- Developer runner: `mercury-serve` console script (`mercury_composable.cli:main`);
  examples run via `mercury-serve app.py --port <n>` with `-D` overrides
  <!-- id: stack-mercury-serve-cli | created: 2026-08-22 | last_used: 2026-09-01 | uses: 2 | tier: active | origin: 2026-08-22-171555 -->

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
  <!-- id: decision-polyglot-reboot | created: 2026-08-22 | last_used: 2026-08-22 | uses: 2 | tier: archive-candidate | origin: 2026-08-22-171555 -->
- **Two-audience root fork (Eric, 2026-08-22):** root `AGENTS.md` routes contributors to
  `memory/PROTOCOL.md` and consumers (developers writing polyglot functions — the "AI
  grammar" path) to `README.md`, which carries the quick start, function contract, and
  wire-format guide.
  <!-- id: decision-consumer-fork-readme | created: 2026-08-22 | last_used: 2026-08-22 | uses: 1 | tier: archive-candidate | origin: 2026-08-22-171555 -->

## Conventions

- **Quality gates (adopted 2026-08-23, Eric's IDE review round): ruff + basedpyright +
  pytest, config lives in pyproject.toml.** ruff: line-length 100, py310, isort extend.
  basedpyright: standard mode + reportMissingParameterType=error, TESTS INCLUDED (Eric's
  ruling — test signatures are annotated; handlers take `(dict[str, str], Body)`).
  `agent-skills/` excluded from both (tool-managed by agent-memory — style fixes belong
  upstream). Run: `uvx ruff check .` / `uvx basedpyright` / `.venv/bin/pytest -q`.
  Unused contract params take the underscore prefix; deliberate suppressions carry
  rationale comments (PyBroadException / noqa only where the rule actually fires).
  <!-- id: conv-python-quality-gates | created: 2026-08-23 | last_used: 2026-08-24 | uses: 3 | tier: active | origin: 2026-08-23-005709 -->
- Engine-mirrored configuration/logging/trace conventions (see the invariant above and
  `instructions.md`); GitHub flow with tests + a CHANGELOG entry per change
  (CONTRIBUTING.md).
  <!-- id: conv-github-flow-changelog | created: 2026-08-22 | last_used: 2026-08-24 | uses: 2 | tier: active | origin: 2026-08-22-171555 -->

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
