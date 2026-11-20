- [ ] **(blueprint) The 2026-11-20 sprint: the question record joins the audit log, the agent-question backend serves
  the React UI's reads, and the hosts serve static content (Eric's three items, reviewed and agreed 2026-11-20).**
  The agreed design, in build order. **(1) Static content** - `static.html.folder`, default `resources/public` next to
  the application file, in both hosts: BUILT and MERGED 2026-11-20, PR #73 (the Node twin mercury-nodejs #138),
  [[static-content-resources-public]].
  **(2) The question record** - the main flow records, the channels deliver: every question and advice passes
  `person.ask` and every answer, from any channel, passes `POST /ask/reply`, so the recorder is a private one-instance
  function of the ask-person process behind drop-n-forget local events, the helper's `transcript.audit` pattern
  ([[transcript-audit-store-python]]) - not the helper itself, since a deterministic flow has no helper in its path. The
  table is today's inbox row moved into the audit database: one row per wait upserted on graph, correlation id and
  turn, one row per advice appended, the answer columns written once under the first-decision rule, the change sequence
  kept for the feed, the message as received kept as the audit copy, linked to the transcript by correlation id and
  graph id (no new column). A published store contract (Eric: PostgreSQL out of the box, a memory implementation as the
  pattern for unit tests, the interface published so a field implements another database), and the engines' published
  schema page gains a question section (ADR-0035). The writes order-independent from the start with an `.instances` key:
  the mailbox's ordering replaced by a per-conversation row lock, a monotonic guard on the row and a forward-only mark -
  the same relaxation proposed for `transcript.audit` (follow-up, `llm.transcript.audit.instances`, default 1). `seen`
  leaves the record (a device's state, kept in the UI). The deterministic node is the generalized record only: a flow
  that asks at a stage sends its id as `graph_id` and the stage as `turn`, its continuation an allowlisted entry. The
  agent-question app becomes a reader of the table under the store's read role plus the click, its intake a wake-up of
  connected devices; the reads for the UI, in both packs and in the shared vectors: `GET /questions?before=<seq>` for
  the history's infinite scroll, `GET /transcripts` and `GET /transcripts/{conversation}` over the audit store,
  `GET /questions/config` for the sign-in. **(3) The React UI**, in the Node pack only (mercury-nodejs thread
  `agent-question-react-ui`), deployed into both packs' `resources/public`, same origin, no CORS. Identity `none` and
  `oauth2` only - `proxy` removed here and from the reply endpoint (Eric: the field's installations use 3-legged SSO and
  OAuth 2.0 access tokens): Authorization Code with PKCE in the browser, the bearer on every call, the stream read with
  `fetch` since `EventSource` carries no bearer, the reply page signing in the same way; a Keycloak development
  container plus a mock issuer fixture for the tests; the vanilla page retired in both packs when the UI lands; no
  source maps. RFC-0014 gets a round-4 amendment in mercury-composable before the record is built. Status: step 1 of 3
  built and merged (PR #73); the RFC amendment merged (mercury-composable #660); step 2 BUILT in Python as PR #74, MERGED 2026-11-20
  (the recorder, the contract, the schema, identity `none`/`oauth2` with the page's PKCE sign-in; the agent-question app
  still on its inbox); next the Node twin, then the backend's reads (step 10) and the UI.
  → serves: vision-mercury-python
  <!-- id: bp-question-record-and-ui | created: 2026-11-20 | last_used: 2026-11-20 | uses: 1 | tier: working | origin: 2026-11-20-075621 -->
