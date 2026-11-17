- [ ] **(blueprint) The essential built-in tools for agent loops, RFC-0013 build plan step 1 and 2 here, Python first
  (Eric: "proceed with essential tools", 2026-11-17; the spec is mercury-composable
  `draft-design-specs/agent-essential-tools.md`, settled, rulings R1-R16).** **Step 1 BUILT 2026-11-17** as PR #63 (merge `55db6f4`, MERGED 2026-11-17):
  `extensions/agent-tools/agent_tools.py`, the seven routes, the catalog files, `describe`, `tools.health`, README,
  98 tests on a workspace and a fixture site, a live https fetch through the pinned-address path; six defaults set
  without a ruling are listed in the session log (hidden entries invisible to every file tool, the resolved address
  pinned, a write bounded by `tools.file.max.bytes`, no `strict` on the rows, the statuses, row names = routes with
  `_`). In passing: the MCP binding's direct run never attached its boot hook (fixed; the Node binding attaches its
  hooks on both paths - checked 2026-11-17, nothing to do there). The engines' page `docs/guides/agent-tools.md`
  landed as mercury-composable #644. **Step 2 BUILT 2026-11-17** as PR #64 (branch
  `feat/agent-tools-graph`): the four graph tools over the Bolt driver (`graph` extra; a `Transaction` seam, a fake for the
  tests, Neo4j and Memgraph schema procedures), `file.extract` behind the `documents` extra, `dev/graph-up.sh` /
  `graph-down.sh`, 161 tests and a live round trip on the development Neo4j; eight defaults set without a ruling in
  the session log. Next: the engines' page and spec for step 2, then the Node twin. The plan as settled - step 1: `extensions/agent-tools/agent_tools.py` (the layout of `extensions/mcp-binding`: a module docstring, a
  `README.md`, `resources/application.yml`, a `describe` command printing every catalog row, `platform.on_startup` for the
  gated registration) with the file tools inside `tools.file.root` (`file.read`, `file.write` marked `write: true` with
  `create` the default, `file.list`, `file.search`; paths resolved with symlinks followed and refused outside the root;
  caps `tools.file.max.bytes` 256 KiB, `max_entries` 200/2000, `max_results` 100/1000, `tools.file.line.max.chars` 500,
  hidden entries off), the web tools inside `tools.web.allow` (`web.fetch` with `as` text|markdown|raw, `web.crawl` same
  host breadth-first with `robots.txt`, `max_pages` 10/50, `max_depth` 2, `include`, `tools.web.crawl.delay.ms` 500,
  `tools.web.page.max.chars` 20000; https unless the host is listed with its scheme; private, loopback, link-local and
  multicast addresses refused unless `tools.web.allow.private=true`; redirects re-checked; `tools.web.max.bytes` 1 MiB,
  `tools.web.timeout.ms` 10 s; a Mercury user agent; HTML to text in-house, no dependency) and the clock `time.now`
  (registers unless `tools.time=false`); a tool whose sandbox is not configured does not register; results are text,
  capped, logged by route, status and size only; the catalog rows as constants and as files under
  `extensions/agent-tools/catalog/` (`file_read.json`, ...; the loop's row keys: `name`, `description`, `input_schema`,
  `route`, `strict`, `write`), pinned by a test and shared byte for byte with the Node twin later; a fixture web server
  in the tests; README; CHANGELOG. Step 2: the four graph tools over the Bolt driver (`graph` extra `neo4j>=6,<7`), a fake
  driver for the tests, `dev/graph-up.sh`/`graph-down.sh` (Neo4j community on Colima), a gated live test, `file.extract`
  behind the `documents` extra (`pypdf`, `python-docx`). Then the engines' docs page "Built-in tools" (mercury-composable,
  plus its skill inventory `files.list`), the Node twin (step 3), the Playground graphs and the recipes (steps 4-5, in the
  engine repo). RFC-0012 is done and promoted (ADR-0035, mercury-composable #643).
  → serves: vision-mercury-python
  <!-- id: bp-agent-tools-python | created: 2026-11-17 | last_used: 2026-11-17 | uses: 1 | tier: working | origin: 2026-11-17-094509 -->
