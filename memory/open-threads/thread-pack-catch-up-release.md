- [ ] **Next pack release: catch up to the Java number and carry the LLM helper.** The pack is published at 4.12.15 while the
  engines shipped 4.12.20. `main` holds, unreleased, the LLM helper app (`examples/llm-helper`, PR #38), the demo-app move and the
  stricter `llm.chat` / `llm.stream` contract (two READ items in the CHANGELOG's Unreleased section). The release is tagged at the
  Java number it catches up to, never an intermediate one; Eric decides when, and tag and publish are his steps. **Prepared
  2026-10-07 (Eric: "the right time for release v4.12.21 for the 4 repos"):** `release/4.12.21` (head `953ba1a`) carries the bump
  and the CHANGELOG cut `## Version 4.12.21, 10/7/2026`; ruff clean, pytest 187; the PR #42 MERGED 2026-10-07
  02:16:34Z as merge `acb1f78` (identical to the branch head outside `memory/`, CI green). Next, Eric's gates: tag `v4.12.21`, publish to PyPI.
  → serves: vision-mercury-python
  <!-- id: pack-catch-up-release | created: 2026-10-01 | last_used: 2026-10-01 | uses: 1 | tier: working | origin: 2026-10-02-001146 -->
