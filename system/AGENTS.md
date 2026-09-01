# Mercury Composable for Python — consumer starting point

> **Contributors working in this repository:** follow the root [AGENTS.md](../AGENTS.md)
> (the agent-memory protocol) first — this file does not replace it. This scoped guide
> serves AI tools that **consume the Python wrapper as a dependency** and need the fastest
> correct starting point. It lives at the same path as the engine repos' consumer guide
> (`system/AGENTS.md`), so one tool convention finds every Mercury repo.

## What this package is

`mercury-python` is a lightweight Event-over-HTTP function host: write decoupled functions
in Python and let Mercury Composable engines (Java, Rust) orchestrate them from Event Script
flows and MiniGraph knowledge graphs. Orchestration stays in the engines; this package
contributes functions plus the engines' operational conventions (configuration, logging,
trace, actuators).

## Starting point for consumer AI tools

The quickest path to a working function:

1. **`docs/llms.txt`** — the machine-readable map of this documentation site.
2. **AI Agent Guide** (`docs/guides/ai-agent-guide.md`) — the complete authoring grammar
   on one page: contract, registration, composition, config keys, error rules.
3. **Getting Started** (`docs/guides/getting-started.md`) — a running function in five
   minutes, called from an engine flow.

For Mercury engine questions (Event Script flows, Knowledge Graph models, REST automation,
Kafka integration), use the engine's `docs/llms.txt` or ai-contract-provider skill — this
wrapper's docs cover only the Python function surface.

## What lives in this repo

| Path | Role |
|------|------|
| `src/` | the `mercury` package — `Platform`, `PostOffice`, `EventEnvelope`, actuator routes |
| `examples/` | runnable reference functions |
| `docs/` | the MkDocs guide site source |
| `tests/` | unit and integration tests |

## Key references (repo-relative)

- `docs/llms.txt` — machine-readable documentation map; start here for keyword lookup
- `docs/guides/ai-agent-guide.md` — AI agent authoring grammar (the authoritative contract)
- `docs/guides/function-patterns.md` — (headers, body) contract, async, composition
- `docs/guides/join-event-script.md` — `yaml.event.over.http`, what the function sees
- `docs/guides/join-knowledge-graph.md` — `graph.task` to a Python target
- `docs/guides/configuration-reference.md` — every well-known config key
- `docs/guides/http-surface-reference.md` — `/api/event` protocol, actuator shapes

## Efficient lookup

**For "how do I write / configure X" questions, start with the guide — not the source.**
`docs/llms.txt` maps every guide page. Find the right page there first; read only the
relevant section. Fall back to source only when the guide is genuinely silent on the
specific behavior or you need to verify a subtle invariant. Guide-first costs 3–5× fewer
tokens than source discovery.

When source reveals a genuine gap, raise an issue or PR against the upstream OSS project:
- **Wrapper docs:** github.com/Accenture/mercury-python
- **Engine guides:** github.com/Accenture/mercury-composable
