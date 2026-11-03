- [ ] **The loop contract in the LLM helper: `llm.chat` serves one turn of the engines' agent loop, with a transcript
  store (RFC-0011 build plan step 1, mercury-composable `agent-loop-llm-run`).** Built 2026-11-02 on two stacked branches:
  `refactor/llm-helper-extension` (`5fddcdc`, the helper moves to `extensions/llm-helper/`, Eric) and
  `feat/llm-chat-loop-contract` (`db6a5f1`: the request and reply fields, the append check and compare-and-set, the
  memory and Redis stores, 66 shared cases in `tests/vectors/llm-loop-vectors.json`). Next: Eric opens the two PRs in
  order (the second rebased onto `main` after the first merges); then the Node twin in mercury-nodejs (the same move, the
  same vector file byte-identical). Not built: a Redis cluster client, the grammar-budget fallback of design-spec section 12
  (dropping `strict` when the combined grammar is too large: its API error is not yet observed), and `decision` lifted into
  the reply (the loop's job in step 2). The design spec takes the small adjustments listed in this session's log.
  → serves: vision-mercury-python
  <!-- id: llm-chat-loop-contract | created: 2026-11-02 | last_used: 2026-11-02 | uses: 1 | tier: working | origin: 2026-11-02-235604 -->
