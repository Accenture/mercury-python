- [ ] **(blueprint) The audit of a single call in the LLM helper: RFC-0015 build plan step 1, Python first, the Node
  twin in lockstep (the maintainer, 2026-11-24: "proceed with implementation for RFC 0015 and 0016"; a field request,
  issue #77).** **Step 1 BUILT 2026-11-24 as PR #81 (merge `5a4a0be`, MERGED 2026-11-24):** with
  `llm.transcript.audit.single: true`, a call without `tools` on `llm.chat` or `llm.stream` saves a closed one-turn
  version 2 record under a new reference with the audit's grace and sends `{conversation, check: true}`;
  `transcript.audit` is unchanged, the sweep repairs a failed write, the reply never waits for the save
  (`_audit_single`, `_save_single`, `audit_single_idle`). The row has `tools` null, which tells it from a loop's.
  - **Proof:** `tests/vectors/llm-audit-vectors.json` (new, 25 cases, written from the contract's statements and run
    through the save, the check event and the function behind its mailbox) and six tests in
    `tests/test_transcript_audit.py`; sixteen mutations of the code and the file each caught by a test.
  - **Defaults set without a ruling (RFC-0015's open questions):** the switch off by default; `llm.stream` recorded
    under the same switch; `model` the model that answered; a stop reason outside the loop's table recorded as `done`;
    the reply not waiting for the save; the switch without `llm.transcript.store` stopping the application at start.
  - **The Node twin MERGED 2026-11-24** (mercury-nodejs #144), from the same vector file with no case changed;
    RFC-0015's entry records both steps (mercury-composable #669 and #670).
  - **Next:** the schema page in
    mercury-composable; the reader's filter on the kind of row (`tools IS NULL`) in the agent-question backend and
    its UI; a live drive with the database stopped and stalled.
  → proposal: RFC-0015 (mercury-composable)
  → serves: vision-mercury-python
  <!-- id: bp-llm-single-call-audit-python | created: 2026-11-24 | last_used: 2026-11-24 | uses: 1 | tier: working | origin: 2026-11-24-055310 -->
