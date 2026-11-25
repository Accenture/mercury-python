- [x] **Re-verify invariants - the second, COMPLETE 2026-11-25 (Eric: "invariants confirmed").**
  `scope-wrapper-no-orchestration`, `wire-standard-golden-vectors`, `engine-consistent-conventions`,
  `stateless-functions-contract` and `vision-mercury-python` all hold, walked with the REST automation edge in mind: the
  scope fence is unchanged (ingress, never orchestration - a flow binding refused by name) and the engine-consistent
  conventions now carry the REST grammar, its messages and its error body. No wording changed; core stays 4. Lesson: the
  edge added a surface, not an exception. origin: 2026-11-25-034612 (raised); closed 2026-11-25-042423.
  <!-- id: reverify-invariants-20261125 | created: 2026-11-25 | last_used: 2026-11-25 | uses: 2 | tier: active | origin: 2026-11-25-034612 -->
