- [x] **The loop contract in the LLM helper: `llm.chat` serves one turn of the engines' agent loop, with a transcript
  store (RFC-0011 build plan step 1).** MERGED 2026-11-03: #46 (merge `13cf394`, the helper moved to `extensions/llm-helper/`)
  and #47 (merge `8f9ae34`, the contract, the memory and Redis stores, 66 cases in `tests/vectors/llm-loop-vectors.json`).
  Lesson: a contract file shared across packs is generated from the contract's statements, never from the code under test,
  and each rule it pins is proven by a negative control. The Node twin and the open items are tracked in mercury-composable
  `agent-loop-llm-run`. Origin 2026-11-02-235604.md; closed 2026-11-03-001129.md.
  → serves: vision-mercury-python
  <!-- id: llm-chat-loop-contract | created: 2026-11-02 | last_used: 2026-11-03 | uses: 1 | tier: working | origin: 2026-11-02-235604 -->
