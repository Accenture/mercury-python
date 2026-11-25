# LLM helper

A dedicated function host that gives a Mercury engine an LLM: `llm.chat`, `llm.stream` and
`llm.health`, on the official Anthropic SDK. The engines stay LLM-free. A graph's `graph.task`
node or a flow's task names the route like any other function, the engine reaches this app
through declarative Event-over-HTTP, and the certified graph decides control flow while the
model advises within it.

```text
  caller ── REST (SSE / JSON) ──> Java or Rust engine ── Event-over-HTTP ──> llm-helper ──> Claude
            Layer 1 service,         flow or graph task                       this app
            flow, or graph
```

`llm.chat` is also the model turn of the engines' agent loop (`llm.run`): see The loop contract.

The Node.js twin ([mercury-nodejs `extensions/llm-helper`](https://github.com/Accenture/mercury-nodejs/tree/main/extensions/llm-helper))
speaks the same contract, the loop contract included (mercury-nodejs #115). Both run the shared
`llm-helper-vectors.json` and `llm-loop-vectors.json`, so they cannot drift apart.

## Run it

```bash
pip install 'mercury-composable[llm]'      # the SDK is this app's one dependency
export ANTHROPIC_API_KEY=...               # from the environment, never from a config file
mercury-serve extensions/llm-helper/llm_helper.py
python extensions/llm-helper/llm_helper.py   # the same without mercury-serve
```

Either way the helper reads `resources/application.yml` next to `llm_helper.py`, whichever directory the
command runs from; `-Dkey=value` overrides any key.

On AWS Bedrock instead (see Backends), the `bedrock` extra adds boto3, which signs each call with the
AWS credential chain, and a region is required:

```bash
pip install 'mercury-composable[llm,bedrock]'
export AWS_REGION=us-east-1                # or llm.aws.region; the AWS credential chain signs each call
mercury-serve extensions/llm-helper/llm_helper.py -Dllm.backend=bedrock
```

If the account is enabled for Bedrock's classic runtime and not for its Anthropic-shaped endpoint (the default
surface answers `403 ... not available for this account` on a model the console's playground runs), switch the
surface and name the model as an inference profile id, exactly as the account lists it:

```bash
mercury-serve extensions/llm-helper/llm_helper.py -Dllm.backend=bedrock -Dllm.aws.api=runtime \
  -Dllm.model=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

To serve an agent loop from more than one replica, keep the transcripts in Redis (see The transcript store);
the `redis` extra adds the client:

```bash
pip install 'mercury-composable[llm,redis]'
export REDIS_PASSWORD=...                  # from the environment, like every credential
mercury-serve extensions/llm-helper/llm_helper.py -Dllm.transcript.store=redis
```

To keep a durable, queryable record of every model turn beside the working store, add the audit store (see The
audit store); the `postgres` extra adds the driver, and `dev/postgres-up.sh` runs a PostgreSQL with the schema and
pgweb as the SQL browser for development:

```bash
pip install 'mercury-composable[llm,redis,postgres]'
export POSTGRES_PASSWORD=...               # from the environment, like every credential
extensions/llm-helper/dev/postgres-up.sh
mercury-serve extensions/llm-helper/llm_helper.py -Dllm.transcript.store=redis \
  -Dllm.transcript.audit=postgres -Dllm.transcript.audit.postgres.ssl=false
```

`resources/application.yml` reads the database user and password from `POSTGRES_USER` (default `mercury`) and
`POSTGRES_PASSWORD`, so no credential travels on a command line; since a `-D` value resolves the same `${ENV_VAR}`
references, `-Dllm.transcript.audit.postgres.password='${POSTGRES_PASSWORD}'` works as well. The other connection
keys default to the development container.

The app listens on port 8086 (`rest.server.port` in `resources/application.yml`). Map its routes
from the engine's `event-over-http.yaml`, as the Java and Rust MiniGraph Playgrounds do:

```yaml
event:
  http:
    - route: 'llm.chat'
      target: 'http://127.0.0.1:8086/api/event'
    - route: 'llm.stream'
      target: 'http://127.0.0.1:8086/api/event'
```

## The contract

Both routes take the same request. `llm.chat` answers once; `llm.stream` answers with the model's
token batches as they are produced.

| Field | Meaning |
|---|---|
| `prompt` or `messages` | A single user turn, or conversation turns `[{role: user\|assistant, content}]`. `messages` wins when both are given. Content is plain text. |
| `system` | Optional system prompt (text). |
| `schema` | `llm.chat` only. A JSON schema; the reply is then structured output, returned parsed as `data`. `additionalProperties` defaults to `false`. A schema on `llm.stream` is a 400. |
| `attachments` | `llm.chat` only, and never a loop turn. Files for the model to look at, named by reference: a non-empty list whose entries are a path inside `llm.attachments.root`, or `{path, pages}` for pages of a PDF, such as `13` or `1-3,7`. See Attachments. |
| `params.model` | The model. Default `claude-opus-5-5` (`llm.model`). On Bedrock's default surface a bare id is sent with the `anthropic.` prefix and an id already spelled for Bedrock as written; on the classic runtime surface every id is sent as written, an inference profile id as the account lists it. |
| `params.max_tokens` | Output cap. Default 16000 (`llm.max.tokens`). |
| `params.timeout_ms` | `llm.chat`: a deadline for the whole call, SDK retries included. `llm.stream`: the idle allowance between events. Default 60000 (`llm.timeout.ms`). |
| `params.effort` | `low`, `medium`, `high`, `xhigh` or `max`. Sent only when set; otherwise the model's own default applies. A model that takes no effort parameter answers 400. |
| `params.stop_sequences` | A list of strings. |
| `params.provider` | Accepted for compatibility: only `anthropic`. |

Any other `params` key is a 400 that names the supported ones. The current Claude models take no
sampling parameters (`temperature`, `top_p`, `top_k`), so none is forwarded.

**`llm.chat` reply** (a map): `text`, or `data` for a `schema` request; `model` (the model that
answered); `stop_reason`; `usage {input_tokens, output_tokens}`; `request_id` when the API sent one;
`stop_details {category, explanation}` when the model refused.

**`llm.stream` reply**: one segment per token batch, in order, then a terminal event whose
trailing metadata carries `model`, `stop_reason`, `usage`, `request_id`, `language`, `trace_id`
and `my_correlation_id`.

### Progressive rendering

The point of `llm.stream` is to deliver batches continuously, so each batch the model produces
leaves this app as its own segment at that moment. Nothing is gathered and sent once. The stream's
head (status 200, `text/event-stream`) rides the first batch. A test pins this: the fake model
refuses to produce batch *k* until the caller already holds batches 0 to *k*-1.

**What a viewer sees is bounded by how the API delivers.** The helper forwards every batch the moment it
arrives, and the engine edge adds nothing: in the certification drive, every batch the helper forwarded
reached the edge as its own frame, within a few milliseconds and without drift. The cadence itself is the
API's, and it differs by model. Measured on the raw API with no SDK in the path, Haiku 4.5 sends about 2
tokens per batch every 25 ms and renders continuously; Sonnet 5.5 sends about 4 tokens in bursts every
350 ms or so; Opus 5.5 sends about 5 tokens, a dozen batches at a time, every 600 ms or so. A burst is
several batches arriving together, so the edge shows the same cadence. Choose `llm.model` (or
`params.model`) for the rendering you want. To attribute a slow or bursty stream, switch on
`llm.log.batches`: each batch then logs its number, size and arrival time (never its text), so the hop
that holds batches back can be told apart from the one that forwards them.

### Errors

Every failure is an `AppException(status, message)`, the portable error contract: the status rides
the envelope into a graph's error context or an HTTP edge.

| Status | When | Message starts |
|---|---|---|
| 400 | A malformed request, or a 400 from the API | `missing 'prompt' or 'messages'`, `unsupported params: …`, `LLM provider error - 400 …` |
| 401 403 404 413 5xx 529 | The API's status, passed through | `LLM provider error - {status} {type}: {message} (request_id …)` |
| 408 | The deadline or the SDK's timeout | `LLM request timed out after {n} ms` |
| 422 | The reply carries nothing usable | `LLM refused the request …`, `LLM reply is empty …`, `LLM reply is not valid JSON for the requested schema …` |
| 429 | Rate limited | `LLM provider rate limit - 429 rate_limit_error: …` |
| 503 | No credential, no region, or the API is unreachable | `LLM provider credential missing - set ANTHROPIC_API_KEY in the environment`, `AWS credential missing - …`, `AWS region missing - …`, `LLM provider unreachable - …` |

**A reply that carries nothing usable is an error, never an empty success.** A refusal with no
text, an empty reply, or a structured reply cut off before it is valid JSON is a 422 that says why
(`stop_reason`, the tokens spent, and what to raise). A reply that stopped early after real text
(`max_tokens`, or a refusal part-way) is returned with its `stop_reason`, and `stop_details` when
the model refused. A stream that ends before its first token fails with a 422 instead of an empty
200. Opus 5.5 thinks before it answers, and thinking tokens count against `max_tokens`, so a budget that
is too small for the question can end with no text at all (seen live with a 120-token budget): that is
the 422 above, not an empty stream. Raise `params.max_tokens` or lower `params.effort`.

## Attachments

A bounded AI node sometimes has to read a page, not a text: a scanned form, a table printed as an image, a chart, a
screenshot, chosen pages of a long PDF. A single `llm.chat` call may name such files in `attachments` (RFC-0016 in
mercury-composable). **The request names a file and never carries one,** so the event stays small and a file's bytes
reach no log, trace or stored record.

```text
graph node ── a path ──> llm-helper ── reads the file in its folder ── the file's bytes ──> the model
```

**Who reads the file.** The helper does. The model is a remote service and reads nothing on this host: the node
passes a path to the helper, the helper reads the file and sends its bytes to the model inside the request, as an
image or a document block before the text of the last turn. The path never reaches the model, and the file's content
leaves for the provider, as a prompt's text does.

**The shared folder.** `llm.attachments.root` names the one folder a request may read in; unset, every attachment is
refused. Whatever puts the files there does so before the graph runs: an upload, a copy, a mounted volume, or a
script that downloads them from an object store (the helper downloading from one itself is a later iteration). A
node names a file by its path inside the folder, sub-folders included:

```
create node read-page
with type Module
with properties
skill=graph.task
task=llm.chat
input[]=text(Return the blocks of the attached page.) -> prompt
input[]=model.page_schema -> schema
input[]=model.page_ref -> attachments[0].path
input[]=model.page_number -> attachments[0].pages
output[]=result.data -> model.page_reading
```

A mapping whose source is null supplies no parameter, so a node with no `model.page_ref` sends a text-only call that
still answers: guard the node with a decision on the reference when the page is required.

**What the helper does.**

- **Stays inside the folder.** A path is relative; the folder is resolved once at start, and a file is read only when
  its resolved path lies inside it, so a path that climbs out and a link that leads out are refused. Hidden entries
  (a name that starts with a dot) are refused, and only a regular file is read. These are the rules of the agent
  tools' workspace (`tools.file.root`).
- **Decides the kind by the first bytes, never by the name.** PNG, JPEG, GIF or WebP becomes an `image` block; a PDF
  a `document` block, from which the model reads both the text and the page image.
- **Cuts a PDF to the pages asked for.** With `pages`, the helper writes a new PDF that holds only those pages, in the
  order named, each named once, so a long document costs the pages asked for. This needs the `documents` extra
  (`pip install 'mercury-composable[llm,documents]'`, pypdf). With the extra installed a whole document is also
  checked; without it a whole document goes as it is and the provider judges it.
- **Refuses before any model call** what the provider would refuse, with a status that names the rule, so a caller
  learns its mistake without spending a token.
- **Reads off the event loop and inside the call's deadline:** `params.timeout_ms` covers the files and the model.

| Case | Status |
|---|---|
| `attachments` on `llm.stream`, or on a loop turn; a last turn that is not the user's | 400 |
| Not a non-empty list; an entry that is neither a path nor `{path, pages}`; `pages` that are malformed, run backward or name a page twice | 400 |
| More files than `llm.attachments.max.count`, or more bytes of files than `llm.attachments.max.bytes` (taken from each file's size before it is read) | 400 |
| An absolute path, a path or a link that leads out of the folder, a hidden entry, a missing file, a folder | 400 |
| A file that is not PNG, JPEG, GIF, WebP or PDF; `pages` on an image; a page the document does not have; an encrypted or unreadable PDF | 400 |
| An image larger than the backend takes, base64-encoded: 10 MB on the Claude API, 5 MB on Amazon Bedrock | 400 |
| No `llm.attachments.root` configured | 503 |
| `pages` without the `documents` extra | 503, naming the extra |
| A configured `llm.attachments.root` that is not a folder | the application stops at start (a request that meets it later is a 500) |

**Limits that are the provider's.** The helper sends no more than 20 files by default because a request with more
than 20 image and document blocks is held to a tighter limit on each image's dimensions. A request takes at most 600
PDF pages, 100 on a model whose context window is under 1M tokens, and each page costs its text and its image in
tokens: name `pages`. The helper sets no page limit of its own; a document over the provider's is the provider's 400,
passed through. Both Bedrock surfaces take these blocks.

**What it costs.** The file's content travels in every call that names it - nothing is kept at the provider between
calls - and it counts as input tokens, by what the model sees and not by the file's size in bytes. An image costs one
token per 28 by 28 pixel patch, capped by the model's resolution tier: at most 1,568 tokens on the standard tier and
4,784 on the high-resolution tier of the newer models, so a full-page scan is a few thousand tokens at most whatever
its megabytes. A PDF page costs its text, typically 1,500 to 3,000 tokens, plus its image, at most 1,568. `pages` is
therefore the cost control: ten pages named cost ten pages, however long the document.

**The folder is readable through the model.** Every caller that can reach this app can have a file of the folder
sent to the provider and read back in the reply. Choose the folder as deployment configuration, give it only what a
graph may show a model, and keep it out of reach of anything that is not trusted to write there.

**In the audit store.** With the audit of a single call switched on, the turn's `request` keeps each attachment as a
reference in place of its bytes - `{type: attachment, path, pages, media_type, bytes, sha256}`, the size and the
SHA-256 of the source file as the helper read it - followed by the turn's text. It is the one block of the audit
that is not the provider's format: the exchange is reconstructed from the record and the files, by hash. The hash
tells whether a file is still the one the model saw; it cannot bring back a file that was changed or removed. Where
an exchange must be reconstructable, keep the files unchanged for as long as the audit's retention
(`llm.transcript.audit.retention`): write-once names in the folder, or versioned objects once an object store is the
source. The prompt, the system prompt and the model's reply are in the audit in full, so what the model wrote about a
file is kept even when the file is not.

Not on `llm.stream`, where a reply carries no schema, and not on a loop turn: a transcript keeps every turn, so it
would have to keep the files, or references whose files may change between turns.

### Uploading documents

The helper fills its own folder: `POST /api/llm/upload` (a REST automation entry with `upload: true` in
`resources/rest.yaml`, served because `application.yml` sets `rest.automation: true`) takes a
`multipart/form-data` upload - PDFs, images, word-processor files, spreadsheets, any file - saves each file
under `uploads/` inside `llm.attachments.root` under a new name, and returns the references a later
`llm.chat` call names in `attachments`:

```shell
curl -s -F 'doc=@contract.pdf' -F 'image=@table.png' http://127.0.0.1:8086/api/llm/upload
```

```json
{
  "files": [
    {"path": "uploads/3f2a...-contract.pdf", "filename": "contract.pdf", "media_type": "application/pdf",
     "bytes": 48213, "sha256": "9c1e...", "attachable": true},
    {"path": "uploads/77b0...-table.png", "filename": "table.png", "media_type": "image/png",
     "bytes": 2048, "sha256": "0a4d...", "attachable": true}
  ]
}
```

`path` is the reference: a graph node that passes it to `llm.chat` as `attachments[0].path` has the model read
the document - for the context, the rules and the formulas it holds. `media_type` is the kind by the first
bytes, else the type the client declared; `attachable` says whether the model takes the file as it is (a PDF
or an image) or a conversion comes first (a word-processor file or a spreadsheet, which `file.extract` of
the agent tools turns into text; a PDF rendering of it is the other way). `sha256` is the hash the audit
keeps, so an exchange can be checked against the file. The saved name is new on every upload (`<id>-<the
original name made safe>`), so nothing is overwritten and a reference stays valid for the audit's retention.
The files of one upload are bounded together by `rest.upload.max.bytes` (16 MiB; 413 beyond it), and the
upload is a 503 by name until `llm.attachments.root` is set. **The folder is readable through the model, and
this entry writes into it:** in production put an authentication function in front of the entry
(`authentication: 'my.auth'`) or the gateway, and give the folder a retention of its own.

## The loop contract

`llm.chat` also serves one model turn of an agent loop. The loop is `llm.run`, a function of the Java and Rust
engines (RFC-0011 in mercury-composable): it calls `llm.chat` once per turn, runs each tool the model asks for as a
composable-function route, and sends the results back. A request that carries `tools` is such a turn. The
transcript never leaves this app: it lives in the transcript store, and the loop holds only a reference to it.

```text
loop     → llm.chat:  system, tools, prompt                               (the first turn)
llm.chat → loop:      status=tool_use, calls=[{id, name, input}], conversation, turn=1
loop     → route:     each call's input, as the event body
loop     → llm.chat:  system, tools, conversation, turn=1, results=[{id, output}]
llm.chat → loop:      status=done, text, conversation, turn=2
```

| Field | Meaning |
|---|---|
| `tools` | `[{name, description, input_schema, strict}]`, as the model sees them. `name` matches `^[a-zA-Z0-9_-]{1,128}$` and is unique; `strict: true` asks for grammar-constrained arguments. Any other key is a 400: a route or an approval mark stays in the loop's catalog, and the model never sees it. |
| `prompt` or `messages` | The first turn only. A later turn carries neither: the transcript holds the conversation. |
| `conversation` | The reference a previous turn returned. Absent on the first turn. |
| `turn` | The turn count the loop expects the record to hold: `0` or absent on the first turn, required with `conversation`. |
| `results` | The answers to the previous turn's calls, one per call: `[{id, output}]` or `[{id, error}]`. |
| `ttl` | How long the record lives while the run goes on: seconds, or `30m`, `12h`, `2d`. Default `7d` (`llm.transcript.ttl`). |
| `retention` | How long the record is kept once the model ends the run. Default `0` (`llm.transcript.retention`), which deletes it. |
| `system`, `schema`, `params` | As on any call, and sent on every turn. |

**Reply:** `status` (`done`, `tool_use`, `refusal` or `cap`); `text`, or `data` for a `schema` request, when done;
`calls [{id, name, input}]` for `tool_use`; `refusal {category}`; `cap` (`max_tokens` or `context`); then
`conversation`, `turn` (the record's count after this turn), `model`, `stop_reason`, `usage {input_tokens,
output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` and `request_id` when the API sent one.

- **A model outcome is a status, not an error.** A refusal, and a turn cut off at `max_tokens` or at the context
  window, come back with their status, so the loop can name them in its envelope. A cut-off turn returns no calls,
  so a truncated call never runs. A run that ends with nothing usable (no text, or a verdict that is not JSON) is
  still the 422 above, and `pause_turn` (server tools, which a loop never sends) is a 502.
- **The append check.** A request whose `turn` differs from the record's count is a 409, and so is a turn on a run
  that has ended. Each write is a compare-and-set on that count, so two copies of one turn (a duplicated resume)
  can never fork the conversation: the second is a 409 even when both passed the check. A conversation that ended
  or expired is a 404.
- **Append-only, exactly as returned.** Each assistant turn is stored as the API returned it, thinking blocks and
  their signatures included, and sent back unchanged. The helper builds the one user message of `tool_result`
  blocks, in call order, with `is_error` on an `error` result; a result that is not text is written as compact JSON.
  A missing `results`, an unknown or duplicate id, or a call left unanswered is a 400 that names the ids.
- **The model is pinned at the first turn.** Every later turn uses it, and a `params.model` that names another is a
  400. Server-side refusal fallbacks are never sent on a loop turn: a fallback moves the conversation to another
  model, whose replay rules edit the history. The graph routes `status: refusal` instead.
- **Caching is the helper's job.** Tools and system come first and stay the same across turns, so the system prompt
  carries one cache breakpoint and the request a top-level one that follows the conversation as it grows. No stored
  block is ever marked. `usage.cache_read_input_tokens` shows the hits.
- **No tool runs here.** The helper carries the model's calls out and the loop's results in; the loop dispatches.

### The transcript store

`llm.transcript.store` names it, and a loop turn without one is a `503 transcript store not configured`. A request
without `tools` never touches it, unless single calls are audited (see The audit of a single call).

| `llm.transcript.store` | For |
|---|---|
| `memory` | One process: development and tests. A turn that reaches another replica answers 404. |
| `redis` | Any number of replicas. The `redis` extra adds the client. |

Redis keeps one hash per conversation under `transcript:{conversation}`, holding the turn count and the record. A
Lua script makes the compare-and-set atomic on that one key. A deployment may share one Redis with the graph's
suspend store (`minigraph-state-redis`, keys `graph:{graph_id}:{cid}`): give this app a Redis ACL user confined to
`~transcript:*`, so the two share a server but neither a namespace nor a credential. The client connects on the
first turn, to a standalone server (a cluster's configuration endpoint needs the cluster client, not built yet); a
store that cannot be reached is a `503 transcript store unreachable - …`.

Every write sets the record's time to live to `ttl`, so a run that waits for a person keeps its transcript: make
`ttl` at least as long as the longest approval wait. When the model ends the run (done, a refusal, the context
window), the record takes `retention` instead: `0` deletes it, and anything longer keeps it closed for audit, under
the store's access control, because a transcript may hold personal data. A run the loop stops itself (a turn cut off
at `max_tokens`, or the loop's own limits) keeps its `ttl` and expires with it.

### The audit store

The transcript store is a **working store**: its time to live is the transaction's, and when the model ends the run
the record takes `retention`, which deletes it by default. An audit needs the opposite shape - durable, indexed for
people, kept on a policy the installation sets - so `llm.transcript.audit` adds one beside it (RFC-0012 in
mercury-composable). `postgres` is the reference store (the `postgres` extra adds psycopg 3); the schema is
published (`docs/guides/transcript-audit-schema.md` in mercury-composable, shipped here as `schema/postgres.sql`),
so an installation that runs another database implements the same two tables behind the same contract.

**Every turn is recorded, asynchronously, off the turn's path.** After the compare-and-set accepted a turn, `llm.chat`
sends one local event to `transcript.audit`, a private composable function of this process with one instance: its
mailbox orders the writes and the end-of-run check of a conversation without a lock. The function reads the working
record - since record version 2 it carries each turn's facts, saved inside the same compare-and-set: the trace id and
path, the correlation id, the UTC bounds of the model call, the outcome, the usage - and writes the rows the store
lacks. One **conversation** row: the reference, the correlation id, the graph and the node from the loop's
`x-graph-id` and `x-node` headers, the model, the status (`open`, `done`, `refusal`, `cap`; `expired` is the purge's),
the turn count, the times, the token totals, the system prompt and the catalog as advertised at the first turn. One
**turn** row per model turn: the trace id and path, the bounds, the stop reason, the status, the provider's request id,
the usage, the messages the turn sent as `request` (the prompt, or the one message of tool results), the assistant
content blocks it got back as `response`, and the normalized `calls` of a tool_use turn. The content is the provider's
format, as sent and as returned. A failed write is logged by type - the conversation reference, the turn, a SQLSTATE,
never a prompt, a completion, an argument or a result - and the turn is not refused.

**The end of a run is an integrity check.** A closing turn sends a second event behind its write: the check counts the
turns the record holds against the rows the store holds, writes the missing ones, and only then **releases** the working
record - its time to live becomes the `retention` the turn carried (`0` deletes it at once). Until then the closed record
waits with `llm.transcript.audit.grace` (default `1d`), not with `retention`; an open record (a run waiting for a tool
result, a person or the loop's next turn) is filled by a check and never released. A sweep every
`llm.transcript.audit.sweep.ms` (default 60 s; on Redis one replica per interval, by a short lock under
`transcript:audit:sweep`) sends a check for every record whose audited mark lags its turn count - the runs the loop
stopped and the writes a dying process lost. The mark is a third hash field, `audited`, beside `turn` and `record`;
the compare-and-set never touches it. Without an audit store nothing of this runs and `retention` applies at the
close as before.

**The audit of a single call.** A call without `tools` - the bounded AI node of a graph, on `llm.chat` or
`llm.stream` - keeps no working record, so by default the audit holds an agent loop's turns only. With
`llm.transcript.audit.single: true` it is recorded as a conversation of **one turn**, carried the way a loop's turns
are (RFC-0015 in mercury-composable): once the model has answered, the helper saves a closed record under a new
reference - the messages as sent, the assistant's content blocks as returned, the system prompt, no tools, the turn's
facts - with the audit's grace as its time to live, and sends the one event a closed record needs, the check.
`transcript.audit` is the function above, unchanged: the check writes the conversation row and the turn row and
releases the record, which deletes it, and a check that fails leaves the record for the sweep. The reply never waits
for the save; a working store that fails is logged by type, and that call is not audited.

- **Telling the two apart.** A single call's row has `turns` 1 and `tools` null, which a loop's row never has:
  `WHERE tools IS NULL` selects the single calls, `IS NOT NULL` the loops. The reference is not returned to the
  caller; the trace id and the correlation id find the row.
- **What is recorded is what the model did.** `status` is the model's outcome - `done`, `refusal`, or `cap` for a
  reply cut off at `max_tokens` or at the context window; any other stop reason is `done`, with the provider's word in
  `stop_reason` - and `model` is the model that answered, which a server-side fallback may have changed. A call the
  model answered is recorded whatever the caller then received: a refusal, a cut-off reply and a reply the helper
  judged unusable (its 422) are in the store with their cost. A call that failed before the model answered leaves no
  row, and neither does a stream the provider broke.
- **It needs the working store.** `llm.transcript.store` holds the record until its check (`memory` suits a single
  call on any number of replicas, since the save and the check run in the process that took the call); the switch
  without a store stops the application at start, by name.
- **It is off by default,** because switching it on multiplies what the audit store holds: every bounded call's
  prompt and reply, kept for `llm.transcript.audit.retention`.

**Retention and purge are the installation's.** `expires_at` is written as the first turn's start plus
`llm.transcript.audit.retention` (default `365d`); the helper runs no purge. `schema/purge.sql` holds the two
statements a purge is, `dev/audit-purge.sh` runs them against the development container by hand (`--dry-run` counts,
`--before <date>` also removes the conversations started before it), and operations automate the same statements on
the enterprise's own schedule and policy.

**Development.** `dev/postgres-up.sh` runs PostgreSQL 17 with the schema applied at first start and pgweb as the SQL
browser at `http://127.0.0.1:8081`, on Colima or Docker, `POSTGRES_PASSWORD` from the environment;
`dev/postgres-down.sh` removes both. The browser answers the natural questions - by correlation id (every turn of a
transaction, suspend and resume included), by trace id (the turns one request ran), by time (what ran between two
instants, with the graph and the node) - and shows the whole transcript of a conversation as the model exchanged it:

```sql
SELECT c.system, c.tools, t.turn, t.request, t.response, t.calls
FROM transcript c JOIN transcript_turn t USING (conversation) WHERE c.conversation = $1 ORDER BY t.turn;
```

The audit store holds everything telemetry refuses to, so it is personal data until proven otherwise: give the
function a role that may `INSERT`, `SELECT` and `UPDATE` the two tables and nothing else, people a reader role, and
keep TLS on (`llm.transcript.audit.postgres.ssl`, the default). The audit does not redact; a redaction policy is the
installation's, applied on read.

## Configuration

`resources/application.yml`, or `-Dkey=value` at run time. A call's `params` win over these keys.

| Key | Default | Meaning |
|---|---|---|
| `llm.backend` | `anthropic` | The route to the models: `anthropic` (the Claude API) or `bedrock` (AWS Bedrock); see Backends. |
| `llm.aws.region` | unset | The AWS region a Bedrock call is signed for. Unset, the SDK reads `AWS_REGION` / `AWS_DEFAULT_REGION`. |
| `llm.aws.api` | `mantle` | The Bedrock surface: `mantle`, the Anthropic-shaped endpoint, or `runtime`, the classic one; see Backends. |
| `llm.model` | `claude-opus-5-5` | The default model. |
| `llm.max.tokens` | `16000` | The default output cap. |
| `llm.timeout.ms` | `60000` | The default deadline (chat) or idle allowance (stream). |
| `llm.max.retries` | `2` | SDK retries for connection errors, 408, 409, 429 and 5xx. |
| `llm.fallbacks` | `default` | `default` or `off`. Server-side refusal fallbacks (`fallbacks: "default"`) on the models that support them (the Opus 5, Fable 5 and Sonnet 5.5 families). The reply's `model` names the model that answered. The Claude API only: Bedrock offers none, so the switch has no effect there. |
| `llm.effort` | unset | The default effort. |
| `llm.log.batches` | `false` | Log each streamed batch's number, size and arrival time (never its text). |
| `llm.provider` | `anthropic` | Only `anthropic`; any other value is a 400. |
| `llm.attachments.root` | unset | The one folder whose files a request may name in `attachments` (see Attachments). Unset, every attachment is a 503. |
| `llm.attachments.max.count` | `20` | Files per call. |
| `llm.attachments.max.bytes` | `20971520` | Bytes of files per call (20 MB), counted from the files' sizes before they are read. |
| `llm.transcript.store` | unset | `memory` or `redis`; a loop turn needs one (see The transcript store). |
| `llm.transcript.ttl` | `7d` | A loop turn's `ttl` when the request carries none. |
| `llm.transcript.retention` | `0` | A loop turn's `retention` when the request carries none. |
| `llm.transcript.redis.host`, `.port`, `.username`, `.password`, `.ssl`, `.database`, `.timeout.ms` | `127.0.0.1`, `6379`, none, none, `false`, `0`, `5000` | The Redis connection: the engines' `redis.*` set under this store's own prefix. The password comes from the environment (`${REDIS_PASSWORD}`). |
| `llm.transcript.audit` | unset | `postgres`: record every model turn in the audit store (see The audit store). Unset, no audit. |
| `llm.transcript.audit.retention` | `365d` | Written as a conversation's `expires_at`; the purge is the installation's. |
| `llm.transcript.audit.grace` | `1d` | How long a closed working record waits for its integrity check before it expires unreleased. |
| `llm.transcript.audit.sweep.ms` | `60000` | The sweep that repairs lost events and stopped runs; `0` disables it. |
| `llm.transcript.audit.single` | `false` | `true`: record a call without `tools` as a conversation of one turn (see The audit of a single call). Needs `llm.transcript.store`. |
| `llm.transcript.audit.postgres.host`, `.port`, `.database`, `.username`, `.password`, `.ssl`, `.timeout.ms` | `127.0.0.1`, `5432`, `mercury`, none, none, `true`, `5000` | The PostgreSQL connection, the engines' convention under this store's prefix. The shipped `application.yml` sets the user and the password from the environment (`${POSTGRES_USER:mercury}`, `${POSTGRES_PASSWORD}`); `ssl` `true` is `sslmode=require`, `false` `sslmode=disable`; `timeout.ms` bounds a connection attempt, a wait for the pool and, on the server, a statement (`statement_timeout`). |

The credential is never configured here. `ANTHROPIC_API_KEY` (or whatever the SDK resolves: an
auth token, or a profile) comes from the environment for the Claude API; the AWS credential chain,
or a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK`, for Bedrock. `llm.health` reports whether a call
could be sent (a credential is present) without reaching the model, so a health probe never spends a
token. `/health` includes it through `mandatory.health.dependencies`.

### The default model and the token budget

The default stays `claude-opus-5-5`, on purpose: it is the most capable model, and a graph's AI node is where
capability counts. It thinks before it answers, and its thinking tokens count against `max_tokens`, so give a
call a generous budget. The engines' demos ask for 2000 and the helper's own default is 16000; a budget of a few
hundred can end with no text at all, which the helper reports as `422 LLM reply is empty`, never as an empty
success.

For the lowest latency and a continuous token trickle, choose Haiku 4.5 (`llm.model: claude-haiku-4-5`, or
`params.model` on a call). The helper sends no thinking parameter, so Haiku does not think, and it takes no
`effort` parameter (a call that sets one answers 400). Sonnet 5.5 sits between the two. The progressive rendering
section above gives each model's cadence.

## Logs and traces

A call logs its model, `stop_reason`, token usage, `request_id` and elapsed time. A prompt, a
system prompt or a completion never reaches a log. Usage also rides the call's trace record as
annotations (`llm_model`, `llm_stop_reason`, `llm_input_tokens`, `llm_output_tokens`,
`llm_request_id`), so telemetry shows what a call cost. A call with attachments also logs and annotates their count
and bytes (`llm_attachments`, `llm_attachment_bytes`), never a path or a file's content. With an audit store, the `transcript.audit` executions a turn
triggers show in the same trace as the function's own; their log lines carry the conversation reference, the turn
numbers written and the failure's type, never the content.

## Backends

`get_backend()` builds the one client the app uses, and a `Backend` says what that route to the
models supports: how it spells a model id, whether server-side fallbacks exist, and what a missing
credential looks like. Nothing else in the module knows which route answered, so the routes, the
contract and the vector file are the same on every backend.

| `llm.backend` | Client | Credential | Model id | Server-side fallbacks |
|---|---|---|---|---|
| `anthropic` (default) | the Claude API, `AsyncAnthropic` | `ANTHROPIC_API_KEY`, or whatever the SDK resolves | as written | on the models that support them |
| `bedrock` | AWS Bedrock, the SDK's `AsyncAnthropicBedrockMantle` (`llm.aws.api: mantle`) or `AsyncAnthropicBedrock` (`runtime`) | the AWS credential chain through IAM (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, a profile, a role), or a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` | `mantle`: a bare id gets the `anthropic.` prefix, an id already spelled for Bedrock is sent as written; `runtime`: every id as written | none - the request never carries them |

**AWS Bedrock.** Install the extra (`pip install 'mercury-composable[llm,bedrock]'` - it adds boto3,
which signs each call), set `llm.backend: 'bedrock'` and a region (`llm.aws.region`, or `AWS_REGION`
in the environment; without one the helper answers `503 AWS region missing` instead of starting a
call), and give the process AWS credentials the way any AWS client gets them. The model must be
enabled for the account in that region. `llm.health` reports `region` beside `backend` and `model`,
and its health check resolves the credential chain the way a call would - on an EC2 or ECS host that
includes the instance metadata service - so a probe tells a missing credential from a present one
without spending a token; a call whose chain turns out empty is a `503 AWS credential missing`.
Effort, structured output and streaming work as on the Claude API. The SDK's client-side refusal
fallback middleware is not wired in; a refusal on Bedrock is the 422 or the `stop_details` of the
contract, like a refusal on the Claude API with `llm.fallbacks: off`.

**Two surfaces.** Bedrock serves Claude on two endpoints, and an account may be enabled for one and not the
other. `llm.aws.api: 'mantle'` (the default) is the Anthropic-shaped endpoint the SDK prefers, which takes the
vendor-prefixed ids above. `llm.aws.api: 'runtime'` is the classic Bedrock runtime, which takes an inference profile
id exactly as the account lists it (`us.anthropic.claude-haiku-4-5-20251001-v1:0` - they carry dates and versions no
rule can derive, so the helper sends every id as written there). The sign that an account lacks the default surface
is a `403 ... is not available for this account` on a model the console's playground runs: switch the surface and
name the model as a profile id. The same message on both surfaces, with the model agreement in place, is an account
enablement for that model generation - a conversation with the AWS account team, not a setting. `llm.health` reports
`api` beside `backend`, `model` and `region`. Bedrock sends no `request_id` the helper reads, so a reply carries none.
A loop turn's top-level cache breakpoint works on both surfaces with the current models; Bedrock's legacy integration
for Opus 4.6 and earlier rejects it with a 400.

## What it does not do

No tool ever runs here: the engines' agent loop dispatches each call to a route. No file's bytes in
a request (a single call names images and PDF pages by reference, see Attachments; other kinds of
file, a loop turn and a stream take none), no `system` turns inside `messages`, no sampling
parameters, no prompt caching controls for a caller (a loop turn is cached by the helper), and no
provider other than Claude. Those are deliberate: the helper is the bounded AI node of a graph and
the model turn of an agent loop, not an agent runtime. A bounded node may look at files it is given
by reference; it still decides nothing about control flow.

## Tests

`tests/test_llm_helper.py` runs the shared `tests/vectors/llm-helper-vectors.json` against a fake of
the SDK (68 cases: request validation, the exact SDK call, replies, the error contract, streaming,
the shape of a Bedrock call on either surface) and the shared `tests/vectors/llm-loop-vectors.json` (66 cases of
one or more turns: the loop fields, the exact SDK call of each turn with the transcript sent back, the reply, and the
stored record after each step), the loop cases once on the memory store and again on a Redis fake that runs the
store's Lua. Then come the tests a vector cannot express (token batches are never held back, no prompt or tool text
in a log, the deadline cancels the call, trace annotations, health, the backend seam, the Bedrock clients with their
region and credential checks, the stored blocks as the SDK sends them back, the Redis store's keys,
compare-and-set and connection settings). No token is spent and no credential is needed.
`tests/test_transcript_audit.py` pins the audit store: record version 2 and the facts a turn saves, the two events
behind a turn and the grace, the function driven through its mailbox (a write that fails, the check that repairs it
before the release, an open record only filled), the sweep, the audited mark on Redis, and the PostgreSQL store on a
recording fake of the pool that pins every statement and its parameters; the loop vector file gains no case, because
the audit is a side effect of a turn, not part of the loop contract. The audit of a single call has a shared file of
its own, `tests/vectors/llm-audit-vectors.json` (25 cases: the two rows a call without `tools` leaves on either route,
and when it leaves none), each run through the save, the check event and the function behind its mailbox; the tests
beside it pin that the reply does not wait for the save, that a failed write is repaired by the sweep, and that a
loop turn is untouched by the switch.

`tests/test_llm_attachments.py` pins the attachments with a third shared file,
`tests/vectors/llm-attachments-vectors.json` (48 cases: the shape of the field, where it does not belong, the folder
a request stays inside, the files refused before any model call, the blocks the model gets, the reference the audit
keeps), over the fixture files of `tests/fixtures/attachments`, which both packs hold byte for byte. A cut PDF is
checked page by page and never byte for byte, since two PDF libraries do not write the same bytes. The tests beside it
pin an image's limit by backend, the helper without the PDF library, the deadline over the read, the worker thread,
the log and the start with a folder that is not one.

```bash
pip install -e '.[dev]'
pytest tests/test_llm_helper.py tests/test_transcript_audit.py tests/test_llm_attachments.py
LLM_TRANSCRIPT_REDIS_URL=redis://127.0.0.1:6379/15 pytest tests/test_llm_helper.py -k live_redis   # on a real Redis
LLM_TRANSCRIPT_AUDIT_TEST_URL='postgres://mercury:...@127.0.0.1:5432/mercury?sslmode=disable' \
  pytest tests/test_transcript_audit.py -k live_postgres                                          # on a real PostgreSQL
```

The live runs touch only the keys and rows of the tests' own conversations; mercury-composable's
`helpers/redis-standalone` is a local Redis without Docker, and `dev/postgres-up.sh` the PostgreSQL on Colima.
