# The agent-question app

The first channel of RFC-0014 (mercury-composable, `draft-design-specs/ask-a-person-channel.md`, Part II): a
**store-and-forward inbox** for a person whose device is not always connected, standing beside a chat, a mail server
or a topic as one channel among equals. The main flow - `person.ask` in [`extensions/ask-person`](../ask-person/README.md)
- knows nothing of it: it sends one message event with the resume link to the routes it is configured with, and this
application is one of those routes. It is a standalone application on the package, run in its own process.

- **Intake.** `agent.question.inbox` is a composable function reached over Event-over-HTTP like any peer function. A
  wait (`kind` `approval` or `question`) is **upserted** on `(graph_id, correlation_id, turn)` - a re-advice for the same
  turn updates the card rather than adding one - and an **advice** (a status update, an acknowledgement, an outcome) is
  **appended**, since those accumulate. Write first: the row is committed before any push to a browser.
- **The person's side**, under the pack's host: `GET /questions/app` is the page - a sticky header with the person's
  name and the count of open cards, the cases that wait for a decision or a look first, and the closed cases in a
  scrollable History panel that keeps its place while new cards arrive; `GET /questions?since=<seq>` the
  **replay** a reconnecting device calls first - every row that changed since its cursor, waits and advices alike, with
  the cursor to continue from; `GET /questions/events` the **stream** a connected device holds, each event carrying a
  changed row, `Last-Event-ID` the same cursor, so a browser's reconnect misses nothing. The person selects which card
  to answer, in any order; the card's button **automates the click**: `POST /questions/{id}/decide` posts
  `{ticket, decisions, decided_by}` to the main flow's reply endpoint - the link's own base, under this app's bearer -
  records the outcome on the row and tells every connected device. `POST /questions/{id}/seen` marks an advice read.
- **States.** A wait is `open` until the engine accepts the decision (`answered`) or finds nothing waiting
  (`expired`); a refused decision or an endpoint that did not answer leaves it `open` with the outcome shown. An
  advice is `open` until the person marks it `seen`. Open waits past their expiry are marked `expired` by a sweeper
  and before every replay. The engine stays the authority: a second device on the same card gets `nothing waiting`,
  and the row records the first: an acceptance always wins the row, and a `nothing waiting` that lands after one
  never overwrites it (the live drive of 2026-11-19). Rows are never deleted here; `schema/purge.sql` is the
  installation's.

A richer person's side - a small React app over the same three endpoints and the same stream - is planned in the
Node.js pack only, so there is one app to maintain in one place (Eric, 2026-11-19): use it from mercury-nodejs
`extensions/agent-question` against this app's inbox when it lands; the vanilla page here stays as the fallback.

The Node.js twin (mercury-nodejs `extensions/agent-question`) serves the same route, the same page and the same
schema, pinned by the same vector file.

## Run

```bash
mercury-serve extensions/agent-question/agent_question.py        # the inbox in memory, no sign-in: development
open http://127.0.0.1:8092/questions/app
```

Point the main flow at it: in the ask-person extension, `ask.channels: [agent.question.inbox]` and
`ask.channel.endpoints: [{route: agent.question.inbox, url: http://127.0.0.1:8092/api/event}]`. For a deployment:

```bash
pip install 'mercury-composable[postgres]'
POSTGRES_PASSWORD=... extensions/llm-helper/dev/postgres-up.sh    # the development database the helper also uses
extensions/agent-question/dev/schema-apply.sh                    # schema/postgres.sql, safe to repeat
mercury-serve extensions/agent-question/agent_question.py -Dquestion.inbox=postgres -Dquestion.inbox.postgres.ssl=false
```

The extension reads `resources/application.yml` next to `agent_question.py`, whichever directory the command runs
from; `-Dkey=value` overrides any key. It listens on port 8092 by default. The boot stops the application by name when
the inbox is `postgres` without the driver, when the database does not answer, or when the auth mode is unknown; with
the inbox in memory it warns that a restart forgets every card.

## The row

What a message event becomes (the replay and the stream carry rows in this shape):

| Field | Meaning |
| --- | --- |
| `id`, `seq` | the row, and the change sequence a replay reads from (bumped on every write) |
| `kind`, `graph_id`, `correlation_id`, `turn` | from the message; a wait upserts on the last three |
| `ticket`, `link`, `expires_at` | a wait's, from the message; null for an advice |
| `description`, `summary`, `details`, `calls` | what the card shows; `calls` is the approval block as the loop returned it |
| `state` | `open`, `answered`, `expired` (a wait) or `seen` (an advice) |
| `requested_at`, `updated_at` | the message's time and the row's last change |
| `answered_at`, `answered_by`, `decisions`, `outcome` | the click: who, when, what, and the reply endpoint's answer |

## Identity

`question.auth`: `none` in development - no sign-in, the page offers a name field the decision carries as
`decided_by`; `proxy` - the installation's SSO, a trusted header set by the gateway (`question.auth.header`,
`x-forwarded-user`) names the person; `oauth2` - a bearer access token validated against the issuer's published keys
(`question.auth.oauth2.issuer`, `.audience`, or `.jwks-url`; PyJWT, the `ask` extra). The app passes the person to the
main flow as `decided_by` under its own bearer (`question.reply.token`, the reply endpoint's `ask.reply.auth.token`).

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `question.inbox` | `memory` | `memory` or `postgres` |
| `question.inbox.postgres.host` / `.port` / `.database` / `.username` / `.password` / `.ssl` / `.timeout.ms` | the helper's audit store's defaults | the connection; the password a `${POSTGRES_PASSWORD}` reference |
| `question.sweep` | `30s` | how often open waits past their expiry are marked expired |
| `question.auth` | `none` | `none`, `proxy` or `oauth2` |
| `question.auth.header` | `x-forwarded-user` | with `proxy` |
| `question.auth.oauth2.issuer` / `.audience` / `.jwks-url` | - | with `oauth2` |
| `question.reply.token` | - | the bearer presented to the reply endpoint |
| `question.reply.timeout` | `10s` | how long the reply endpoint may take |
| `question.page.title` | `Agent questions` | the page's title |

## Tests

`tests/test_agent_question.py` runs the shared vectors (`tests/vectors/agent-question-vectors.json`: the row a message
becomes, the state an outcome leaves, the request the click sends, the inbox's transitions on a scripted sequence) and
drives the app in-process: the intake on the bus, the replay and the stream, a decision forwarded to a fake reply
endpoint with the outcome recorded and broadcast, an advice marked seen, the proxy mode, the PostgreSQL inbox's
statements on a recording fake of the pool, and - gated on `AGENT_QUESTION_TEST_URL` - the schema and the rows on a live
PostgreSQL.
