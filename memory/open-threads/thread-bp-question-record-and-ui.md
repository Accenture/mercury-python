- [x] **(blueprint) The 2026-11-20 sprint: the question record joins the audit log, the agent-question backend serves the
  React UI's reads, and the hosts serve static content.** CLOSED 2026-11-20: all three items built and MERGED in both
  packs - static content #73 / mercury-nodejs #138, the record #74 / #139, the backend's reads #75 / #140, the React
  UI's bundle #76 (merge `5cc9fccaf`) with the UI itself in mercury-nodejs #141 (`344b34f07`) - and the UI driven live
  through both backends and a Keycloak issuer (mercury-composable `docs/test-reports/agent-question-ui-live-drive.md`,
  PR #661, open at close). Lesson: a stream a browser holds must end when the application stops, or a container
  manager's grace period ends it ([[platform-on-shutdown-hook]]). Follow-ups outside this thread: the reply page's own
  sign-in against an issuer; `llm.transcript.audit.instances`. The narrative: logs 2026-11-20-075621 to 2026-11-20-094019.
  → serves: vision-mercury-python
  <!-- id: bp-question-record-and-ui | created: 2026-11-20 | last_used: 2026-11-20 | uses: 1 | tier: working | origin: 2026-11-20-075621 -->
