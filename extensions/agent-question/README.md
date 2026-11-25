# The agent-question app

The first channel of RFC-0014 (mercury-composable, `draft-design-specs/ask-a-person-channel.md`, Part II) and, since
round 4 of the paper (section 21.3), **the backend of the React UI** maintained in the Node.js pack: a reader of the
**question record** the main flow keeps in the audit database, a feed of its changes for a person's device, a browser
of the transcripts the LLM helper writes beside it, and the automated click that answers a wait. The main flow -
`person.ask` in [`extensions/ask-person`](../ask-person/README.md) - records every message and answer itself and knows
nothing of this app: it sends the message event with the resume link to the routes it is configured with, and this
application is one of those routes. It is a standalone application on the package, run in its own process.

- **Intake.** `agent.question.inbox` is a composable function reached over Event-over-HTTP like any peer function. It
  writes nothing - the record is the recorder's (`question.audit` in the ask-person process) - it checks the event and
  wakes the feed, so a connected device sees the row the moment it is recorded.
- **The person's side** is `resources/rest.yaml` - the engines' REST automation (RFC-0018), served by the pack's host
  in front of this process's private functions, with `rest.automation: true` in `application.yml`: `GET /questions/config`
  (`question.config`) tells the UI how to sign in (the auth mode, the title and, under `oauth2`, the issuer, the public
  client id, the scopes and the audience); `GET /questions?since=<seq>` (`question.list`) is the **replay** a
  reconnecting device calls first - every row that changed since its cursor, oldest change first, with the cursor to
  continue from; `GET /questions?before=<seq>&limit=<n>` the **history's older page**, newest change first, with the
  cursor of the page before it - the infinite scroll; `GET /questions/events` (`question.events`, a `stream: true`
  entry) the **stream** a connected device holds, each `question` event a changed row whose `seq` is the cursor,
  `Last-Event-ID` the same cursor, so a browser's reconnect misses nothing (the engines' Server-Sent Events dialect: an
  event name and the data, no `id:` line); `GET /transcripts?before=<iso>&limit=<n>&correlation_id=&graph_id=` and
  `GET /transcripts/{conversation}` (`transcript.list`, `transcript.show`) browse the **transcript audit store** - the
  conversations newest first, one conversation with its turns as the model exchanged them. The person selects which
  card to answer, in any order; the card's button **automates the click**: `POST /questions/{id}/decide`
  (`question.decide`) posts `{ticket, decisions, decided_by}` to the main flow's reply endpoint - the link's own base,
  under this app's bearer - and answers with the row as the recorder then left it and the engine's outcome; the feed
  carries the change to every device. Every entry but the sign-in configuration names `question.identity` as its
  authentication function: it resolves who is at the UI once, by the configured mode, and the functions read `who` and
  `via` from the request's session map. An error is the engines' standard body, `{"status", "message", "type": "error"}`.
- **The feed** reads the record's change sequence: a poll every `question.poll` (1 s) and a wake-up from the intake or a
  click, whichever comes first. A wait is `open` until the engine accepts the decision (`answered`) or finds nothing
  waiting (`expired`); an advice has no state - whether a person has read it is the device's, kept by the UI. The
  record is never written here; the recorder's sweeper expires waits and the installation's purge retires rows.

This app is the backend in both packs. Its front-end - the React UI with the question-and-answer panel, the history with
infinite scroll and the transcript browser - is built and maintained in the Node.js pack only, so there is one app to
maintain in one place (Eric, 2026-11-19): mercury-nodejs `extensions/agent-question-ui`. Its bundle is deployed into
this app's `resources/public` and committed here, which the host serves from the same origin at the application's root
(`static.html.folder`): open `http://127.0.0.1:8092/`. The engines' actuator endpoints (`/info`, `/info/routes`, `/health`, `/env`, `/livenessprobe`) stay at their paths
beside the bundle - a host path always wins over a file - so a container manager's probes work unchanged, and the UI
offers them as a menu. The development page this app used to generate at `/questions/app` is retired; a change to the UI is a build in the Node.js pack and a commit of the bundle here.

The Node.js twin (mercury-nodejs `extensions/agent-question`) serves the same routes, pinned by the same vector file.

## Run

```bash
pip install 'mercury-composable[postgres]'
POSTGRES_PASSWORD=... extensions/llm-helper/dev/postgres-up.sh    # the development database, once
extensions/ask-person/dev/schema-apply.sh                        # the question record's schema, safe to repeat
extensions/llm-helper/dev/schema-apply.sh                        # the transcript audit store's schema
mercury-serve extensions/agent-question/agent_question.py -Dquestion.store=postgres -Dquestion.store.postgres.ssl=false
open http://127.0.0.1:8092/                                      # the React UI, served from resources/public
```

Point the main flow at it: in the ask-person extension, `ask.channels: [agent.question.inbox]`,
`ask.channel.endpoints: [{route: agent.question.inbox, url: http://127.0.0.1:8092/api/event}]` and `ask.audit: postgres`
on the same database. The extension reads `resources/application.yml` next to `agent_question.py`, whichever directory
the command runs from; `-Dkey=value` overrides any key. It listens on port 8092 by default. The boot stops the
application by name when the store is `postgres` without the driver, when the database does not answer, or when the
auth mode is unknown; `question.store: memory` is for the tests - nothing writes a memory store at run time, and the
boot says so.

## The row

What the feed and the replay carry (the row the main flow's recorder writes, `extensions/ask-person/schema/postgres.sql`):

| Field | Meaning |
| --- | --- |
| `id`, `seq` | the row, and the change sequence a replay reads from (bumped on every write) |
| `kind`, `graph_id`, `correlation_id`, `turn` | from the message; a wait upserts on the last three, an advice appends |
| `ticket`, `link`, `expires_at` | a wait's, from the message; null for an advice |
| `description`, `summary`, `details`, `calls` | what the card shows; `calls` is the approval block as the loop returned it |
| `state` | `open`, `answered`, `expired` for a wait; null for an advice |
| `requested_at`, `updated_at` | the message's time and the row's last change |
| `answered_at`, `answered_by`, `decisions`, `outcome` | the click: who, when, the stamped decisions, and the engine's answer |

A question joins its transcript by `correlation_id` and `graph_id`, which the transcript rows carry: `GET /transcripts?correlation_id=<cid>`.

## Identity

`question.auth`: `none` in development - no sign-in, the UI offers a name field the decision carries as
`decided_by`; `oauth2` - a bearer access token validated against the issuer's published keys
(`question.auth.oauth2.issuer`, `.audience`, or `.jwks-url`; PyJWT, the `ask` extra). The UI signs a person in by
Authorization Code with PKCE - a public client in the browser - from the issuer and the client id
(`question.auth.oauth2.client-id`, `.scopes`) that `GET /questions/config` names; the field's installations use
3-legged SSO and OAuth 2.0 access tokens, so there is no gateway-header mode. The app passes the person to the main
flow as `decided_by` under its own bearer (`question.reply.token`, the reply endpoint's `ask.reply.auth.token`).

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `question.store` | `memory` | `memory` (tests) or `postgres`: the audit database the record and the transcripts are in |
| `question.store.postgres.host` / `.port` / `.database` / `.username` / `.password` / `.ssl` / `.timeout.ms` | the audit store's defaults | the connection; the password a `${POSTGRES_PASSWORD}` reference; a reader role suffices |
| `question.poll` | `1s` | how often the feed looks for rows the record gained, beyond the intake's wake-up |
| `question.auth` | `none` | `none` or `oauth2` |
| `question.auth.oauth2.issuer` / `.audience` / `.jwks-url` | - | with `oauth2`: the token's validation |
| `question.auth.oauth2.client-id` / `.scopes` | - / `openid profile email` | with `oauth2`: the UI's sign-in (PKCE) |
| `question.reply.token` | - | the bearer presented to the reply endpoint |
| `question.reply.timeout` | `10s` | how long the reply endpoint may take |
| `question.page.title` | `Agent questions` | the UI's title, told through `GET /questions/config` |

## Tests

`tests/test_agent_question.py` runs the shared vectors (`tests/vectors/agent-question-vectors.json`: the event the intake
accepts or refuses, the request the click sends, the pages the replay and the history read) and drives the app
in-process as the reader of the record: the intake waking the feed, the replay and the history pages and the stream, the
transcripts browsed, a decision forwarded to a fake reply endpoint and the row re-read as the recorder left it, the two
auth modes with the UI's configuration, the PostgreSQL reader's statements on a recording fake of the pool, and - gated
on `AGENT_QUESTION_TEST_URL` - the schemas and the rows on a live PostgreSQL.
