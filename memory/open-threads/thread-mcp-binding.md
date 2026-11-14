- [ ] **The MCP binding (RFC-0011 / ADR-0033 build plan step 4): `extensions/mcp-binding/`, built 2026-11-14; PR #51 MERGED 2026-11-14 (merge `ea3783d`).**
  The extension, the `Platform` startup and cleanup hooks, the `mcp` extra, the shared vector file (sha256 `4bacc319…`) and
  46 tests; the HTTP transport driven live against a public MCP server (the Mermaid Chart endpoint) the same day. The Node
  twin landed the same day (mercury-nodejs #118, the same vector bytes), and the engine-side drive ran with an MCP tool in a
  graph's catalog (mercury-composable #626 and #627, the `diagram-assistant` example on the Mermaid server). The `@preload`
  shape - every bound route an alias of the one function `mcp.tool` - is the PR from branch `refactor/mcp-binding-preload`
  (2026-11-14; the Node twin of that shape is Eric's call). Next: the pack's catch-up release carries it
  ([[pack-catch-up-4-12-25]]). Fact: [[mcp-binding-route-per-tool]].
  → serves: vision-mercury-python
  <!-- id: mcp-binding | created: 2026-11-14 | last_used: 2026-11-14 | uses: 1 | tier: working | origin: 2026-11-14-073523 -->
