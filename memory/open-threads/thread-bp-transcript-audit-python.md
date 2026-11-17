- [ ] **(blueprint) The transcript audit store in the LLM helper: RFC-0012 build plan step 1, Python first, the Node
  twin in lockstep.** Built 2026-11-17 on `feat/transcript-audit-store` (the PR pending): record version 2 with each
  turn's facts, the store protocol's audit half on the memory and Redis stores, the `transcript.audit` function with
  one instance behind two local events (the write after every compare-and-set, the check behind a closing write), the
  sweep, PostgreSQL on psycopg 3 (the `postgres` extra), `schema/postgres.sql` and `purge.sql`, the `dev/` scripts on
  Colima with pgweb, 28 tests and a live test. Verified live on the container. Open: the PR's merge; the schema page in
  mercury-composable; the Node twin (mercury-nodejs) with the byte-identical schema files; the engines' `x-graph-id` and
  `x-node` (step 3); the `order-assistant` drive with an injected write failure (step 4). The engines' memory tracks the
  design under `transcript-audit-store` (RFC-0012, settled 2026-11-17).
  → serves: vision-mercury-python
  <!-- id: bp-transcript-audit-python | created: 2026-11-17 | last_used: 2026-11-17 | uses: 1 | tier: working | origin: 2026-11-17-080036 -->
