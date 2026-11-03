- [ ] **Contradiction: the LLM helper's transcript store against `stateless-functions-contract` (and the wording of
  `scope-wrapper-no-orchestration`).** The invariant says a function keeps nothing and anything it must keep belongs to the
  caller's flow model or state machine. RFC-0011's rulings 9 and 27 (Eric, 2026-10-28) put an agent loop's transcript in
  a store the pack owns, so the engine's state machine stays small and no provider block crosses an engine codec; the
  loop contract (2026-11-02, [[llm-chat-loop-contract]]) builds it in `extensions/llm-helper/`. The package
  (`src/mercury_composable`) stays persistence-free, so `scope-wrapper-no-orchestration` still holds for the package, and the
  handler keeps no in-process state (any replica serves any turn). Proposed amendment of `stateless-functions-contract`,
  for Eric's approval: "Functions are stateless in process. What a function must keep belongs to the caller's flow model
  or state machine, except where a design names an external, access-controlled store (the LLM helper's transcript store,
  RFC-0011); intentional errors travel as `AppException(status, message)`." Not edited until Eric rules.
  <!-- id: contradiction-transcript-store-stateless | created: 2026-11-02 | last_used: 2026-11-02 | uses: 1 | tier: working | origin: 2026-11-02-235604 -->
