- [x] **(blueprint) The transcript audit store in the LLM helper: RFC-0012 build plan step 1, Python first, the Node
  twin in lockstep.** Built 2026-11-17, PR #59 MERGED 2026-11-17 (merge `a2f53dfe3`): record version 2 with each
  turn's facts, the store protocol's audit half on the memory and Redis stores, the `transcript.audit` function with
  one instance behind two local events (the write after every compare-and-set, the check behind a closing write), the
  sweep, PostgreSQL on psycopg 3 (the `postgres` extra), `schema/postgres.sql` and `purge.sql`, the `dev/` scripts on
  Colima with pgweb, 28 tests and a live test. Verified live on the container. The schema page (mercury-composable
  #639), the Node twin (mercury-nodejs #126) and the engines' headers (#641, mercury #414) MERGED 2026-11-17. **Step 4
  DRIVEN 2026-11-17 through this helper:** three `order-assistant` conversations on Haiku 4.5 (one with PostgreSQL paused,
  one stopped - the writes failed by type, the sweep repaired the open record 46 s after the restart) and the first two
  turns of a conversation the Node helper then resumed from this helper's record; every turn in PostgreSQL with the
  graph and the node, the Redis record released after each check (the report: mercury-composable
  `docs/test-reports/transcript-audit-live-drive.md`, PR #642). Follow-up here: a server-side `statement_timeout` and
  pgweb's restart policy (PR #62 MERGED 2026-11-17, merge `c60a43b17`). **CLOSED 2026-11-17:** the pack's half of RFC-0012
  is built, merged (#59, #62) and driven live; RFC-0012's promotion to an ADR is tracked in mercury-composable. Lesson: a
  store's outage has two shapes - a stopped server fails fast, a stalled one does not - and a bound belongs on both the
  connection and the statement. The engines' memory tracks the
  design under `transcript-audit-store` (RFC-0012, settled 2026-11-17).
  → serves: vision-mercury-python
  <!-- id: bp-transcript-audit-python | created: 2026-11-17 | last_used: 2026-11-17 | uses: 1 | tier: working | origin: 2026-11-17-080036 -->
