- [x] (feature — **MERGED 2026-09-01 as
  [PR #22](https://github.com/Accenture/mercury-python/pull/22) true merge `44caf9a6`
  carrying `1fa5f70`; tree verified; branches deleted both ends) **AI nodes llm.chat +
  llm.stream — provider-neutral LLM adapters (agent-orchestration E0).** One contract, two
  editions (Anthropic + Gemini as optional extras `mercury-composable[llm]`);
  schema-constrained verdicts for graph decision routing; token streams over the
  multi-shot reply contract; Gemini AFC opted out (no tool surface — the graph decides,
  the model advises). Live-proven from the engine's support-triage graph (its PR #304).
  Lesson: PyCharm validates monkeypatch attr-name literals regardless of target typing —
  route the name through a helper parameter. origin: 2026-09-01-022620.
  <!-- id: ot-llm-ai-nodes | created: 2026-09-01 | last_used: 2026-09-01 | uses: 1 | tier: working | origin: 2026-09-01-022620 -->
