- [x] **The MCP binding (RFC-0011 / ADR-0033 build plan step 4) — CLOSED 2026-11-16: released in v4.12.30.** Outcome:
  `extensions/mcp-binding/` built 2026-11-14 (#51, #52; the Node twin mercury-nodejs #118), reshaped 2026-11-16 to a
  `@preload` function per tool bound in code through `bind(server, tool, schema_sha256)` (#55; the fact
  [[mcp-binding-function-per-tool]]), driven live against the public Mermaid Chart MCP server from the Java engine (the
  engines' `docs/test-reports/agent-tool-use-live-drive.md`), published to PyPI in v4.12.30 as the `mcp` extra. Lesson: a
  route names the capability, never the transport - a tool may start as an MCP tool and later be implemented another way,
  and the graph must not know. Origin: 2026-11-14-073523; the record in 2026-11-16-090518.md.
  → serves: vision-mercury-python
  <!-- id: mcp-binding | created: 2026-11-14 | last_used: 2026-11-14 | uses: 1 | tier: working | origin: 2026-11-14-073523 -->
