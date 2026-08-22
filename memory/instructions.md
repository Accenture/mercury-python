# Agent Instructions — mercury-python

## What This Project Is

The **Python member of the Mercury Composable polyglot initiative** (repurposed August
2026): a deliberately **lightweight wrapper of the engines' Event-over-HTTP protocol**, so
decoupled Python functions can be orchestrated by the Mercury Composable engines (Java, and
the official Rust port) from Event Script flows and MiniGraph knowledge graphs — with no
orchestration code in Python at all. It provides an Event API host (`POST /api/event`), a
thin `PostOffice` client, the standard event-envelope wire-format codec, and the engines'
minimalist utilities (config, logging, distributed-trace context). Orchestration stays in
the engines by design. The legacy pre-composable language pack lives in git history only.

**Type:** Library — polyglot function host + client (PyPI package `mercury-composable`)
**Primary language:** Python (async-first; sync handlers run in a thread-pool executor)
**Framework / stack:** aiohttp host, MsgPack envelope codec — see continuity `## Stack & Tools`

> High-level only. The precise dependency list and current versions live in
> `memory/continuity.md` → `## Stack & Tools` (the live source of truth) — keep this
> section enduring and don't duplicate them here.

## Repository Structure

- `src/mercury_composable/` — the package: `server.py` (Event API host), `client.py`
  (`PostOffice`), `envelope.py` (wire-format codec), `registry.py` (`@preload`),
  `config.py` (`AppConfig`), `log.py`, `trace.py`, `exceptions.py` (`AppException`),
  `cli.py` (`mercury-serve`); `py.typed` ships type information.
- `tests/` — pytest suite; `tests/vectors/vectors.json` holds the **golden conformance
  vectors shared with the Java and Rust engines** (the wire-compatibility proof).
- `examples/demo_app.py` — minimal runnable function app.
- Root `README.md` — the consumer-facing guide (quick start, function contract,
  configuration, wire compatibility); the root `AGENTS.md` fork routes consumers there.

## Two audiences, two paths

Root `AGENTS.md` forks readers: **contributors** follow `memory/PROTOCOL.md` (this memory
layer); **consumers** — developers writing polyglot functions against this package — start
at `README.md` and never load contributor memory.

## Conventions Observed

- **Engine consistency is the house style:** configuration keys (`application.name`,
  `rest.server.port`, `log.format`, `log.level`), `${ENV_VAR:default}` substitution,
  `-D` runtime overrides, `resources/` config layout, and the Java reference engine's log
  presentation are all mirrored deliberately — a polyglot installation must stay uniform.
- Intentional errors raise `AppException(status, message)` — the portable error contract
  on the wire; handlers are stateless.
- Contribution flow (CONTRIBUTING.md): standard GitHub flow, write tests, update
  `CHANGELOG.md` with each change.

## Tone & Style

- Be concise unless detail is explicitly requested.
- Prefer prose over bullet lists for explanations.
- When suggesting code changes, match the existing style and patterns in this repo.
- Always check `memory/continuity.md` for prior decisions before suggesting
  architectural changes.

## Core Rules

1. Never modify files outside the project scope without asking.
2. Follow the existing code style — do not reformat files unnecessarily.
3. When in doubt about a pattern or convention, ask rather than assume.
4. Record all significant decisions in the session log and continuity file.
5. If you see a TODO, open thread, or obvious issue, note it in continuity.md.

## Testing

pytest ≥8 with pytest-asyncio (`asyncio_mode = auto`), `testpaths = ["tests"]`. Wire-format
changes must stay green against the shared golden vectors (`tests/vectors/vectors.json`) —
they are the cross-engine compatibility contract, not ordinary fixtures.

## CI / CD

No package build/test workflow yet (pre-release). The agent-memory advisory CI floor
(`.github/workflows/agent-memory.yml`) is installed; it checks the memory layer only.

## Editing These Instructions

Only modify this file if the user explicitly asks to change the project
description, rules, or conventions. Treat it as stable configuration.
