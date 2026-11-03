- [x] **Contradiction: the LLM helper's transcript store against `stateless-functions-contract`** - RESOLVED 2026-11-03,
  NOT a contradiction (Eric): `llm.chat` is configurable by design, a stateless chat by default or, with `tools`, one
  turn of a multi-turn tool-use loop whose transcript lives in the configured transcript store (RFC-0011). The
  invariant and the scope fence now say so, and the package itself stays persistence-free. Lesson: a configurable
  contract is not an exception to the rule it configures; state it in the rule so the next review does not re-raise it.
  origin: 2026-11-02-235604; closed 2026-11-03-012056.
  <!-- id: contradiction-transcript-store-stateless | created: 2026-11-02 | last_used: 2026-11-03 | uses: 2 | tier: active | origin: 2026-11-02-235604 -->
