- [ ] **Re-verify invariants (due):** confirm scope-wrapper-no-orchestration, wire-standard-golden-vectors,
  engine-consistent-conventions, stateless-functions-contract and the Vision (vision-mercury-python) still hold, or supersede any
  that don't (DECAY.md §9). The first re-verify: `last_invariant_check` was unset and 40 session files exist (cadence 40).
  **Walked against the live tree 2026-11-03** (suite: 351 passed, 64 live-Redis cases skipped):
  - `scope-wrapper-no-orchestration` HOLDS: the wheel is `src/mercury_composable` alone, with no store client, subprocess,
    file write, flow, graph or broadcast in it; the LLM helper is in `extensions/` (sdist teaching surface, not the wheel).
    Wording refreshed: the helper is an application on the package.
  - `wire-standard-golden-vectors` HOLDS: `tests/vectors/vectors.json` is byte-identical with Java platform-core's 7 vectors.
  - `engine-consistent-conventions` HOLDS: config, `${ENV:default}` and `-D` tests in the config suite, `x-ttl` and
    `x-async` in the server suite, `resources/` next to the application file (2026-10-14, both packs).
  - `stateless-functions-contract` HOLDS, wording refreshed per Eric's ruling: `llm.chat` is configurable by design,
    a stateless chat or a tool-use loop turn with its transcript in the configured store
    ([[contradiction-transcript-store-stateless]], closed).
  - The Vision HOLDS after the two 2026-11-03 amendments (versioning, and the package-only persistence clause). A forward
    note: "Never subprocess or embedded-interpreter execution" means functions are never run that way (Option A); RFC-0011
    step 4 binds MCP servers over stdio, a child process of the pack, so its wording is settled before step 4.
  - Rust finding, outside this repo: its `envelope-vectors/vectors.json` has 6 vectors and lacks `standard-trace-context`
    (added in Java #213, 2026-07-21).
  Walked 2026-11-03-012056; awaits Eric's confirmation.
  <!-- id: reverify-invariants-20261103 | created: 2026-11-03 | last_used: 2026-11-03 | uses: 3 | tier: working | origin: 2026-11-03-010131 -->
