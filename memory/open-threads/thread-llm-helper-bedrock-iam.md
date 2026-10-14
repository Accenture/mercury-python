- [x] **AWS Bedrock through IAM as the LLM helper's second backend. CLOSED 2026-10-14.** Built in PR #43 (merged `c25ba35`) and
  driven live the same day with Eric's temporary IAM credentials and no vendor key: negative controls first (401 and 503 by
  name), then chat, a structured verdict and a progressive stream on Haiku 4.5 through the classic runtime surface, and the Java
  engine's shipped triage graph and streaming relay in front of the helper; PR #44 carries the surface switch `llm.aws.api`, the
  SDK's credential wording mapped, and the report. Lesson: an account may be enabled for one Bedrock surface and one model
  generation only - the gates are the per-model agreement, the one-time use case and the account's own enablement, told apart by
  `GetFoundationModelAvailability` - and the negative control is what found the mapping gap. origin: 2026-10-02-001146.
  → serves: vision-mercury-python
  <!-- id: llm-helper-bedrock-iam | created: 2026-10-01 | last_used: 2026-10-01 | uses: 1 | tier: working | origin: 2026-10-02-001146 -->
