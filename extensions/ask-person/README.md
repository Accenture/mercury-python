# Ask a person - the main flow

An agent loop can wait for a person: a tool call marked `write: true` waits under the loop's default mode, and the
**ask row** of the loop spec's section 7.3 lets the model put a question to a person and carry on in the same
conversation with the answer. The graph suspends durably on its state store and resumes at most once. What was missing
is the person: nothing carried a waiting call to anyone, and nothing brought a decision back except a request a
developer typed. This extension is the **main flow** of RFC-0014 (mercury-composable,
`draft-design-specs/ask-a-person-channel.md`): it carries the wait out and brings the decision in, knows no channel,
and holds no state of the wait - the engine's at-most-once resume stays the authority. Since round 4 of the paper it
also keeps the **question record**: what was asked, advised and decided, by whom and with what outcome, as part of the
transcript audit log, whatever channel delivered the message.

- **`person.ask`** is a composable function the graph's hold path calls before it suspends. The message names the
  graph (`graph_id`, `description`, `node`), the transaction (`correlation_id`, `turn`), the waiting `calls` exactly as
  the loop returned them, and the suspension's `ttl`. The function issues a signed, stateless **ticket**, builds the
  **resume link**, writes the message to the question record and sends it as one event to every route in
  `ask.channels` - the agent-question app's intake, a chat poster, a mail sender, a topic publisher - knowing nothing of
  what a route does with it. A channel that fails is logged by type and never fails the hold. A third kind,
  **`advice`**, is a notification with no decision (a status update of a long run, an acknowledgement after a
  response): no ticket, no link.
- **The resume link**, `<ask.reply.url>/ask/<ticket>`, opens the wait on this process's reply page. The page reads the
  current wait from the engine by the reference graph's re-advice (a request without decisions, `GET /ask/{ticket}/wait`)
  and never resumes - link unfurlers and mail scanners click links. Only a submit resumes: **`POST /ask/reply`** with
  `{ticket, decisions}` - a person's browser, or a channel app on the person's behalf under its own bearer with
  `decided_by` - verifies the ticket, maps the graph id to the engine's entry through the `ask.graphs` allowlist (never
  to an address a message told it), prefixes who decided and when to each tool call's note, sends the resume (the same
  entry, the correlation id in `x-correlation-id`, `{turn, decisions}`), writes the answer to the question record and
  returns the engine's outcome to the invoker.
- **The question record** is written by `question.audit`, a private composable function of this process behind local
  events, off the request path: a wait upserted on `(graph_id, correlation_id, turn)`, an advice appended, the answer
  with the stamped decisions, who decided, when and the engine's outcome. The main flow records and the channels
  deliver, so a chat that keeps only its recent messages or a topic with no browser of its history loses nothing to the
  audit, and the agent-question app's UI shows what any channel delivered.

The Node.js twin (mercury-nodejs `extensions/ask-person`) serves the same routes, the same page and the same record,
pinned by the same vector file.

## Run

```bash
pip install mercury-composable                   # the open mode adds no dependency
pip install 'mercury-composable[ask]'            # + PyJWT for the oauth2 mode of the reply page and endpoint
pip install 'mercury-composable[postgres]'       # + psycopg for the question record in PostgreSQL
export ASK_TICKET_KEY='a long random secret'
mercury-serve extensions/ask-person/ask_person.py -Dask.ticket.key='${ASK_TICKET_KEY}'
python extensions/ask-person/ask_person.py       # the same without mercury-serve
python extensions/ask-person/ask_person.py inspect v1.eyJ...   # read a ticket (a check, never a resume)
```

For the record in PostgreSQL, on the development database the LLM helper's dev scripts start:

```bash
POSTGRES_PASSWORD=... extensions/llm-helper/dev/postgres-up.sh    # once; the container mercury-transcripts
extensions/ask-person/dev/schema-apply.sh                         # schema/postgres.sql, safe to repeat
mercury-serve extensions/ask-person/ask_person.py -Dask.ticket.key='${ASK_TICKET_KEY}' \
  -Dask.audit=postgres -Dask.audit.postgres.ssl=false
```

The extension reads `resources/application.yml` next to `ask_person.py`, whichever directory the command runs from;
`-Dkey=value` overrides any key, and a value may carry `${ENV_VAR:default}` references. It listens on port 8091 by
default (`rest.server.port`). The boot stops the application by name when the ticket key is not set, the reply URL is
not an http or https base, the auth mode or the record store is unknown, `oauth2` is chosen without PyJWT or without an
issuer or a JWKS URL, or `postgres` without psycopg; it warns when no channel or no graph is configured, when the record
is in memory, and when the record's database does not answer at start (the writes then fail by type until it does).

## The message

```json
{
  "kind": "approval",
  "graph_id": "order-assistant",
  "description": "Customer-support assistant: a refund waits for a supervisor",
  "correlation_id": "order-1002-case",
  "turn": 2,
  "node": "assist",
  "summary": "issue_refund for ORD-1002, 174.00 USD: 'Shipment lost in transit'",
  "calls": [{"id": "toolu_01...", "name": "issue_refund", "route": "demo.refund.issue", "wait": true,
             "input": {"order_id": "ORD-1002", "amount": 174.0, "reason": "Shipment lost in transit"}}],
  "ttl": "2d",
  "requested_at": "2026-11-19T09:12:04.118Z"
}
```

| Field | A wait (`approval`, `question`) | An `advice` |
| --- | --- | --- |
| `kind` | `approval` (a call needs `approved`) or `question` (an ask row) | `advice` |
| `graph_id`, `description`, `correlation_id`, `summary` | required text | required text |
| `node` | optional: the loop node that waits | optional |
| `turn` | required, a whole number: the turn that waits | optional: the turn it follows |
| `calls` | required: the approval block as the loop returned it | refused |
| `ttl` | required: the suspend node's time to live (`30m`, `12h`, `2d`, or whole seconds) | refused |
| `details` | - | optional text, the body |
| `requested_at` | optional ISO-8601; the default is now | optional |

The function cross-checks `graph_id` against the engine's `x-graph-id` header when one is present. It answers
`{ticket, link, expires_at, delivered, recorded}` for a wait and `{delivered, recorded}` for an advice, where
`delivered` lists each channel route with `ok` and the status it answered and `recorded` says whether the question record
took the message; `expires_at` is `requested_at` plus `ttl`, the suspension's own expiry. The event a channel receives
is the message plus `ticket`, `link` and `expires_at`. A flow that asks a person at a stage sends the same message with
its id as `graph_id` and the stage as `turn`; the record has one shape for an AI node and a deterministic node.

## The ticket

`v1.<payload>.<mac>`: the payload `{"g": graph_id, "c": correlation_id, "t": turn, "k": kind, "e": expiry}` as
canonical JSON (keys sorted, no spaces, Unicode kept) and its HMAC-SHA256 under `ask.ticket.key`, both base64url without
padding. Opaque and clean - no question text, no business data, no person - and stateless: the reply endpoint verifies
it with the key alone, so every channel works without a store. `ask.ticket.previous-key` verifies the tickets issued
before a rotation. A ticket is accepted as many times as it is presented: single use is the engine's at-most-once
resume, and the second presentation gets `nothing waiting`. An expired ticket (410) is refused without a call to the
engine; a malformed one or another version is a 400, one no key signs a 403.

## The reply page and endpoint

- `GET /ask/{ticket}` is the page: a ticket that cannot be used is told so there (expired, tampered, malformed, a graph
  not in the allowlist), before any sign-in. Otherwise the page's script reads `GET /ask/config`, signs the person in
  when the installation asks for it (below), reads the wait from `GET /ask/{ticket}/wait` and renders it: the graph and
  the turn from the ticket, the calls from the engine's re-advice - approve or refuse with a note per waiting call, an
  answer (the options offered, or free text) for a question, a call marked `wait: false` shown as running on resume. A
  wait the engine no longer holds (`nothing-waiting`, HTTP 410) renders as expired; an engine that has moved on shows
  its reply.
- `GET /ask/{ticket}/wait` is that read as JSON, under the same identity as the reply: `{payload: {g, c, t, k},
  advice}` with the engine's re-advice, 410 `{"type": "nothing-waiting"}`, or the engine's own refusal. It re-advises
  and never resumes.
- `POST /ask/reply` takes `{"ticket": "...", "decisions": [...], "decided_by": "..."}`: `{id, approved, note?}` for a
  tool call, `{id, answer}` for a question - the shapes the loop accepts. Each tool call's note is prefixed
  `[<who> at <time>]`; the audit store carries it when the note reaches the model, which the loop does for a refused
  call, and the question record carries the decision itself in every case. The engine's status and JSON body come back
  as they are; a graph that does not answer or answers outside the contract is a 502 `{"type": "error", "message":
  ...}`; an expired ticket is a 410 `{"type": "expired"}`.
- **Who may answer** (`ask.reply.auth`): `none` in development - no sign-in, `decided_by` is taken as given or the
  person is `anonymous`; `oauth2` - a bearer access token validated against the issuer's published keys
  (`ask.reply.auth.oauth2.issuer`, `.audience`, or `.jwks-url`; PyJWT). Under `oauth2` the page signs the person in by
  Authorization Code with PKCE, a public client in the browser: `GET /ask/config` tells it the issuer, the client id
  (`ask.reply.auth.oauth2.client-id`), the scopes and the audience, the token is kept for the tab and sent as a bearer
  on the wait and the reply; the issuer must allow the page's URL as a redirect (`<ask.reply.url>/ask/*`). The field's
  installations use 3-legged SSO and OAuth 2.0 access tokens, so there is no gateway-header mode. In every mode a
  channel app may present the bearer `ask.reply.auth.token` and name the person in `decided_by`.
- **The allowlist** `ask.graphs` maps a graph id to the engine entry the resume is sent to; a ticket for a graph that is
  not listed is a 404, and the endpoint never posts to an address a message told it.

## The question record

The row (`schema/postgres.sql`; the Node.js twin's is byte-identical):

| Column | Meaning |
| --- | --- |
| `id`, `seq` | the row, and the change sequence a feed reads from (`question_seq`, bumped on every write) |
| `kind`, `graph_id`, `correlation_id`, `turn` | from the message; a wait upserts on the last three, an advice appends |
| `ticket`, `link`, `expires_at` | a wait's; null for an advice |
| `description`, `summary`, `details`, `message` | what the message said; `message` is the event as sent, the audit copy |
| `state` | a wait's: `open`, `answered`, `expired`; null for an advice - whether a person read it is the device's |
| `requested_at`, `updated_at` | the message's time and the row's last change |
| `answered_at`, `answered_by`, `decisions`, `outcome` | the answer: when, who, the stamped decisions, the engine's status and body |

The writes are order-independent, so `question.audit` may run several instances (`ask.audit.instances`): a wait is
upserted unless the row holds a newer `requested_at` (an older message landing late never overwrites a newer one), an
advice is appended, and an answer applies while the row is open or when the engine accepted the decision - the first
accepted decision stands, a late `nothing waiting` never overwrites it. The sweeper (`ask.audit.sweep`) marks open
waits past their expiry `expired`. A question joins its transcript (the LLM helper's `transcript` table) by
`correlation_id` and `graph_id`. Rows are never deleted here; `schema/purge.sql` is the installation's retention.

**The contract** (`QuestionStore` in `ask_person.py`) is published so a field keeps the record on another database:
`record(event, now)` (a wait upserted or an advice appended, the row as it stands), `answer(key, decisions, who, state,
outcome, now)` (the first-decision rule; None when no wait was recorded), `expire(now)`, `since(seq, limit)`,
`counts()`. PostgreSQL is the reference implementation; the memory store carries the same semantics for unit tests
(`ask.audit: memory`, the default - a restart forgets and no other process reads it). The agent-question app reads the
record under the store's read role; it is the backend of the React UI maintained in the Node.js pack.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `ask.enabled` | `true` | `false` answers every call with a 503 |
| `ask.channels` | `[]` | the routes that receive the message event (a YAML list, or `a,b` as a `-D` value) |
| `ask.channel.endpoints` | `[]` | `{route, url}` entries for routes served by another process (or `route=url,...`) |
| `ask.channel.timeout` | `5s` | how long one channel may take |
| `ask.ticket.key` | - | the HMAC key, a `${ASK_TICKET_KEY}` reference; required |
| `ask.ticket.previous-key` | - | verifies tickets issued before a rotation |
| `ask.reply.url` | - | the public base of the resume link; required |
| `ask.reply.auth` | `none` | `none` or `oauth2` |
| `ask.reply.auth.oauth2.issuer` / `.audience` / `.jwks-url` | - | with `oauth2`: the token's validation |
| `ask.reply.auth.oauth2.client-id` / `.scopes` | - / `openid profile email` | with `oauth2`: the page's sign-in (PKCE) |
| `ask.reply.auth.token` | - | the bearer a channel app presents when it invokes the link |
| `ask.graphs` | `[]` | `{id, entry}` entries: the graphs a reply may resume (or `id=url,...`) |
| `ask.engine.timeout` | `60s` | how long the engine may take to answer a re-advice or a resume |
| `ask.audit` | `memory` | the question record: `memory` (tests) or `postgres` |
| `ask.audit.postgres.host` / `.port` / `.database` / `.username` / `.password` / `.ssl` / `.timeout.ms` | the audit store's defaults | the connection; the password a `${POSTGRES_PASSWORD}` reference |
| `ask.audit.sweep` | `30s` | how often open waits past their expiry are marked expired |
| `ask.audit.instances` | `1` | the recorder's workers; more is safe |
| `ask.audit.timeout` | `5s` | how long a write may take before the hold or the reply goes on without it |

## Tests

`tests/test_ask_person.py` runs the shared vectors (`tests/vectors/ask-person-vectors.json`: the ticket issued, verified,
tampered, expired and rotated; the message validated and the event built; the re-advice and the resume requests; the
outcomes; the row a message becomes, the state an answer leaves and the record's transitions) and drives the function
and the page in-process: channels that take, refuse and miss the event, the record written before they are told; the
reply endpoint against a fake engine, the answer recorded with who decided; the two auth modes with a signed token and
the page's configuration; the PostgreSQL store's statements on a recording fake of the pool, and - gated on
`ASK_AUDIT_TEST_URL` - the schema and the rows on a live PostgreSQL; the host hook that mounts the routes before the
catch-all.
