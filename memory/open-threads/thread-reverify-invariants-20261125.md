- [ ] **Re-verify invariants (due 2026-11-25 - 44 sessions since the first re-verify of 2026-11-03):** confirm
  `scope-wrapper-no-orchestration`, `wire-standard-golden-vectors`, `engine-consistent-conventions`,
  `stateless-functions-contract` and the Vision (`vision-mercury-python`, amended by Eric on 2026-11-24 with REST
  automation) still hold, or supersede any that don't (DECAY.md §9). Walk them with the REST automation edge in mind:
  the scope fence is unchanged (ingress, never orchestration - a flow binding refused by name) and the
  engine-consistent conventions now include the REST grammar, its messages and its error body. The review never
  auto-invalidates an invariant; the human confirms (checks this thread off) or supersedes.
  <!-- id: reverify-invariants-20261125 | created: 2026-11-25 | last_used: 2026-11-25 | uses: 1 | tier: working | origin: 2026-11-25-034612 -->
