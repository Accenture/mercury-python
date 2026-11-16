- [ ] **Next pack release: catch up to the Java number (4.12.30 as of 2026-11-13) with the Bedrock backend, its two surfaces and the
  resources-folder rule.** HELD on 2026-10-14 until the higher AI models were available on Bedrock; **the hold was LIFTED on
  2026-10-29 (Eric, recorded in mercury-composable): the Claude API backend serves every current model, so the pack releases
  when its work is ready** and adopts whatever Java number is current then (every port adopts the Java number on catch-up).
  Content on main since 4.12.21: PRs #43, #44 and #45 (the Node twins: mercury-nodejs #111, #112, #113), and since
  2026-11-03 #46 (the helper's move to `extensions/`) and #47 (the loop contract, [[llm-chat-loop-contract]]), whose Node
  twins merged the same day as mercury-nodejs #114 (`62958b5`) and #115 (`d6a0bd9`). Since 2026-11-14: #51 to #53 (the MCP binding and the trace records, [[mcp-binding]] and
  [[trace-records-engine-parity]]) and, 2026-11-16, #54 (route aliases in `@preload` and the JSON log `source` label,
  the Java engine's shapes; the Node twin mercury-nodejs #121) and #55 (the MCP binding's function-per-tool shape, the
  twin mercury-nodejs #122), and #56 (the code-quality round of the local SonarQube scan, the twin mercury-nodejs #123) and #57 (the
  cognitive-complexity findings of the same scan, the twin mercury-nodejs #124). The previous catch-up's close record is [[pack-catch-up-release]]. **PREPARED 2026-11-16 as v4.12.30 on branch `release/4.12.30`** (the version, the
  CHANGELOG section, the gates green; the Node twin in lockstep); Eric's gates: PR, merge, tag, PyPI. **MERGED and TAGGED 2026-11-16:** PR #58 (opened by Claude Code at Eric's request) merge
  `239e741` 09:09:50Z, the tag `v4.12.30` on the merge commit, the GitHub release 09:11:00Z as Latest, CI green; the PyPI
  publication PENDING at 09:16Z.
  → serves: vision-mercury-python
  <!-- id: pack-catch-up-4-12-25 | created: 2026-10-14 | last_used: 2026-11-13 | uses: 6 | tier: working | origin: 2026-10-14-015420 -->
