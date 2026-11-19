# Ask a person - the main flow

An agent loop can wait for a person: a tool call marked `write: true` waits under the loop's default mode, and the
**ask row** of the loop spec's section 7.3 lets the model put a question to a person and carry on in the same
conversation with the answer. The graph suspends durably on its state store and resumes at most once. What was missing
is the person: nothing carried a waiting call to anyone, and nothing brought a decision back except a request a
developer typed. This extension is the **main flow** of RFC-0014 (mercury-composable,
`draft-design-specs/ask-a-person-channel.md`): it carries the wait out and brings the decision in, knows no channel,
and keeps no store - the engine's at-most-once resume stays the authority.

- **`person.ask`** is a composable function the graph's hold path calls before it suspends. The message names the
  graph (`graph_id`, `description`, `node`), the transaction (`correlation_id`, `turn`), the waiting `calls` exactly as
  the loop returned them, and the suspension's `ttl`. The function issues a signed, stateless **ticket**, builds the
  **resume link** and sends the whole message as one event to every route in `ask.channels` - the agent-question app's
  intake, a chat poster, a mail sender, a topic publisher - knowing nothing of what a route does with it. A channel
  that fails is logged by type and never fails the hold. A third kind, **`advice`**, is a notification with no decision
  (a status update of a long run, an acknowledgement after a response): no ticket, no link.
- **The resume link**, `<ask.reply.url>/ask/<ticket>`, opens the wait on this process's reply page. A `GET` shows the
  current wait, read from the engine by the reference graph's re-advice (a request without decisions), and never
  resumes - link unfurlers and mail scanners click links. Only a submit resumes: **`POST /ask/reply`** with
  `{ticket, decisions}` - a person's browser, or a channel app on the person's behalf under its own bearer with
  `decided_by` - verifies the ticket, maps the graph id to the engine's entry through the `ask.graphs` allowlist (never
  to an address a message told it), prefixes who decided and when to each tool call's note, sends the resume (the same
  entry, the correlation id in `x-correlation-id`, `{turn, decisions}`) and returns the engine's outcome to the invoker.

The Node.js twin (mercury-nodejs `extensions/ask-person`) serves the same route and the same page, pinned by the
same vector file.

## Run

```bash
pip install mercury-composable                   # the open and the proxy modes add no dependency
pip install 'mercury-composable[ask]'            # + PyJWT for the oauth2 mode of the reply page
export ASK_TICKET_KEY='a long random secret'
mercury-serve extensions/ask-person/ask_person.py -Dask.ticket.key='${ASK_TICKET_KEY}'
python extensions/ask-person/ask_person.py       # the same without mercury-serve
python extensions/ask-person/ask_person.py inspect v1.eyJ...   # read a ticket (a check, never a resume)
```

The extension reads `resources/application.yml` next to `ask_person.py`, whichever directory the command runs from;
`-Dkey=value` overrides any key, and a value may carry `${ENV_VAR:default}` references. It listens on port 8091 by
default (`rest.server.port`). The boot stops the application by name when the ticket key is not set, the reply URL is
not an http or https base, the auth mode is unknown, or `oauth2` is chosen without PyJWT or without an issuer or a JWKS
URL; it warns when no channel or no graph is configured.

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
`{ticket, link, expires_at, delivered}` for a wait and `{delivered}` for an advice, where `delivered` lists each channel
route with `ok` and the status it answered; `expires_at` is `requested_at` plus `ttl`, the suspension's own expiry. The
event a channel receives is the message plus `ticket`, `link` and `expires_at`.

## The ticket

`v1.<payload>.<mac>`: the payload `{"g": graph_id, "c": correlation_id, "t": turn, "k": kind, "e": expiry}` as
canonical JSON (keys sorted, no spaces, Unicode kept) and its HMAC-SHA256 under `ask.ticket.key`, both base64url without
padding. Opaque and clean - no question text, no business data, no person - and stateless: the reply endpoint verifies
it with the key alone, so every channel works without a store. `ask.ticket.previous-key` verifies the tickets issued
before a rotation. A ticket is accepted as many times as it is presented: single use is the engine's at-most-once
resume, and the second presentation gets `nothing waiting`. An expired ticket (410) is refused without a call to the
engine; a malformed one or another version is a 400, one no key signs a 403.

## The reply page and endpoint

- `GET /ask/{ticket}` renders the wait: the graph and the turn from the ticket, the calls from the engine's re-advice
  - approve or refuse with a note per waiting call, an answer (the options offered, or free text) for a question, a
  call marked `wait: false` shown as running on resume. A wait the engine no longer holds (`nothing-waiting`, HTTP 410)
  renders as expired; an engine that has moved on shows its reply.
- `POST /ask/reply` takes `{"ticket": "...", "decisions": [...], "decided_by": "..."}`: `{id, approved, note?}` for a
  tool call, `{id, answer}` for a question - the shapes the loop accepts. Each tool call's note is prefixed
  `[<who> at <time>]`, so the audit store carries who decided without a new field; an answer travels unchanged. The
  engine's status and JSON body come back as they are; a graph that does not answer or answers outside the contract is a
  502 `{"type": "error", "message": ...}`; an expired ticket is a 410 `{"type": "expired"}`.
- **Who may answer** (`ask.reply.auth`): `none` in development - no sign-in, `decided_by` is taken as given or the
  person is `anonymous`; `proxy` - the installation's SSO, a trusted header set by the gateway (`ask.reply.auth.header`,
  `x-forwarded-user`) names the person; `oauth2` - a bearer access token validated against the issuer's published keys
  (`ask.reply.auth.oauth2.issuer`, `.audience`, or `.jwks-url`; PyJWT). In every mode a channel app may present the
  bearer `ask.reply.auth.token` and name the person in `decided_by`.
- **The allowlist** `ask.graphs` maps a graph id to the engine entry the resume is sent to; a ticket for a graph that is
  not listed is a 404, and the endpoint never posts to an address a message told it.

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
| `ask.reply.auth` | `none` | `none`, `proxy` or `oauth2` |
| `ask.reply.auth.header` | `x-forwarded-user` | with `proxy`: the header that names the signed-in person |
| `ask.reply.auth.oauth2.issuer` / `.audience` / `.jwks-url` | - | with `oauth2` |
| `ask.reply.auth.token` | - | the bearer a channel app presents when it invokes the link |
| `ask.graphs` | `[]` | `{id, entry}` entries: the graphs a reply may resume (or `id=url,...`) |
| `ask.engine.timeout` | `60s` | how long the engine may take to answer a re-advice or a resume |

## Tests

`tests/test_ask_person.py` runs the shared vectors (`tests/vectors/ask-person-vectors.json`: the ticket issued, verified,
tampered, expired and rotated; the message validated and the event built; the re-advice and the resume requests; the
outcomes) and drives the function and the page in-process: channels that take, refuse and miss the event; the reply
endpoint against a fake engine; the three auth modes with a signed token; the host hook that mounts the routes before
the catch-all.
