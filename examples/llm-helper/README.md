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

The Node.js twin ([mercury-nodejs `examples/llm-helper`](https://github.com/Accenture/mercury-nodejs))
speaks the same contract. Both run one shared vector file, so they cannot drift apart.

## Run it

```bash
pip install 'mercury-composable[llm]'      # the SDK is this app's one dependency
export ANTHROPIC_API_KEY=...               # from the environment, never from a config file
mercury-serve examples/llm-helper/llm_helper.py
```

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
| `params.model` | The model. Default `claude-opus-5-5` (`llm.model`). |
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
| 503 | No credential, or the API is unreachable | `LLM provider credential missing - set ANTHROPIC_API_KEY in the environment`, `LLM provider unreachable - …` |

**A reply that carries nothing usable is an error, never an empty success.** A refusal with no
text, an empty reply, or a structured reply cut off before it is valid JSON is a 422 that says why
(`stop_reason`, the tokens spent, and what to raise). A reply that stopped early after real text
(`max_tokens`, or a refusal part-way) is returned with its `stop_reason`, and `stop_details` when
the model refused. A stream that ends before its first token fails with a 422 instead of an empty
200. Opus 5.5 thinks before it answers, and thinking tokens count against `max_tokens`, so a budget that
is too small for the question can end with no text at all (seen live with a 120-token budget): that is
the 422 above, not an empty stream. Raise `params.max_tokens` or lower `params.effort`.

## Configuration

`resources/application.yml`, or `-Dkey=value` at run time. A call's `params` win over these keys.

| Key | Default | Meaning |
|---|---|---|
| `llm.backend` | `anthropic` | The route to the models; see Backends. |
| `llm.model` | `claude-opus-5-5` | The default model. |
| `llm.max.tokens` | `16000` | The default output cap. |
| `llm.timeout.ms` | `60000` | The default deadline (chat) or idle allowance (stream). |
| `llm.max.retries` | `2` | SDK retries for connection errors, 408, 409, 429 and 5xx. |
| `llm.fallbacks` | `default` | `default` or `off`. Server-side refusal fallbacks (`fallbacks: "default"`) on the models that support them (the Opus 5, Fable 5 and Sonnet 5.5 families). The reply's `model` names the model that answered. |
| `llm.effort` | unset | The default effort. |
| `llm.log.batches` | `false` | Log each streamed batch's number, size and arrival time (never its text). |
| `llm.provider` | `anthropic` | Only `anthropic`; any other value is a 400. |

The credential is never configured here. `ANTHROPIC_API_KEY` (or whatever the SDK resolves: an
auth token, or a profile) comes from the environment. `llm.health` reports whether a call could
be sent (a credential is present) with no network traffic, so a health probe never spends a token.
`/health` includes it through `mandatory.health.dependencies`.

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
`llm_request_id`), so telemetry shows what a call cost.

## Backends

`get_backend()` builds the one client the app uses, and a `Backend` says what that route to the
models supports. Today there is one, `anthropic` (the Claude API).

**Planned: AWS Bedrock through IAM.** It is additive. Build the SDK's Bedrock client in
`get_backend()` (it authenticates with the AWS credentials of the default chain instead of an API
key, and takes a region), give the `Backend` a `provider_model` that prefixes the model id (`anthropic.claude-opus-5-5`),
report `supports_fallbacks=False` (server-side fallbacks are not offered on Bedrock), and describe a
missing AWS credential in `credential_problem()`. Select it with `llm.backend: 'bedrock'`. The routes,
the contract and the vector file stay as they are, because nothing outside `Backend` knows which
route answered.

## What it does not do

No tools or function calling, no images or documents, no `system` turns inside `messages`, no
sampling parameters, no prompt caching controls, and no provider other than Claude. Those are
deliberate: the helper is the bounded AI node of a graph, not an agent runtime.

## Tests

`tests/test_llm_helper.py` runs the shared `tests/vectors/llm-helper-vectors.json` against a fake of
the SDK (63 cases: request validation, the exact SDK call, replies, the error contract, streaming),
plus the tests a vector cannot express (token batches are never held back, no prompt text in a
log, the deadline cancels the call, trace annotations, health, the backend seam). No token is
spent and no credential is needed.

```bash
pip install -e '.[dev]'
pytest tests/test_llm_helper.py
```
