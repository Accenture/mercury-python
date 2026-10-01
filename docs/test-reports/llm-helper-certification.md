---
title: Test Report — The LLM helper in the Python host
summary: This pack's view of the live LLM helper certification of 2026-10-01 - the Python helper
  (llm.chat, llm.stream, llm.health on the Anthropic SDK) behind the Java and Rust engines with real
  Claude calls - the token-free proof, the live results, and what the Python SDK needed.
layer: reference
audience: [developer, architect]
keywords: [llm helper, llm.chat, llm.stream, claude, anthropic, streaming, certification, test report]
---

# Test Report — The LLM helper in the Python host

*This pack's view of the live certification of 2026-10-01 (UTC). The full report — all four engine and host
pairs, the three layers, the progressive-rendering evidence and the findings — is the
[engine report](https://accenture.github.io/mercury-composable/test-reports/llm-helper-certification/), which the
Rust engine's docs carry too. The helper is
[`examples/llm-helper`](https://github.com/Accenture/mercury-python/tree/main/examples/llm-helper); its README holds the contract.*

## Verified without a credential

- **86 helper tests, no token spent** (the whole suite: 187). `tests/test_llm_helper.py` runs a fake of the Python SDK
  that builds the SDK's own error objects, so the status codes and messages are the real ones.
- **One contract, shared with the Node.js twin.** `tests/vectors/llm-helper-vectors.json` is byte-identical in both packs (SHA-256 `1f4823d9…8fa0`, pinned by
  a test in each) and holds 63 cases: 22 request validations, 29 chat outcomes and 12 stream outcomes, each fixing the exact SDK
  call, the reply or the error, and the segments. A change that drifts one helper away from the other fails a case.
- **The tests can fail.** A helper mutated to gather the token batches and send them at the end fails the sentinel test (the
  fake model refuses to produce batch *k* until the caller holds batches 0 to *k*-1) and the vector for tokens delivered before a
  mid-stream error; a helper whose default model is changed fails 13 cases. The unmutated helper passes all of them.
- **Static checks:** `ruff check .` clean, `basedpyright` 0 errors on the whole repository, `pytest -q` 187 passed.

## Verified live

Behind the Java engine and behind the Rust engine, the Python helper answered every scenario: 40 results per pair and no
failed check, across a streaming service (Layer 1), an Event Script flow (Layer 2) and two graphs (Layer 3), on
`claude-opus-5-5` and, where a request named it, `claude-haiku-4-5`.

| Progressive stream | Helper batches | Edge frames | Offset, median / spread | Longest gap |
|---|---|---|---|---|
| Java → Python, Opus 5.5 | 67 | 67 | 12 / 10 ms | 1201 / 1206 ms |
| Java → Python, Haiku 4.5 | 101 | 101 | 7 / 4 ms | 191 / 191 ms |
| Rust → Python, Opus 5.5 | 50 | 50 | 4 / 9 ms | 922 / 922 ms |
| Rust → Python, Haiku 4.5 | 82 | 82 | 3 / 5 ms | 228 / 227 ms |

Every batch the helper forwarded reached the engine's HTTP edge as its own frame, within a few milliseconds and without drift:
nothing is gathered or sent once. The long gaps are the API's own pacing, the same at both ends; Haiku 4.5 streams continuously
(a batch about every 25 ms) while Opus 5.5 arrives in bursts about every 600 ms. Credential states, measured on the real SDK:
no credential gave 503 through Java on all three layers and through Rust on the graph, and an invalid key gave Anthropic's 401 with its request id when this pack's client called the helper directly. The same message came from both helpers, through every layer. All traces that touched this helper rebuild
as one connected tree.

## What the Python SDK needed

- **No credential is not an API error.** `anthropic` 1.x raises a bare `TypeError` ("Could not resolve authentication method…")
  before anything is sent, so the helper maps it by its text to a 503 `LLM provider credential missing - set ANTHROPIC_API_KEY in the
  environment`. A missing key is also what `llm.health` reports, from the client's own attributes and with no network traffic.
- **The SDK takes no sampling parameters.** `temperature`, `top_p` and `top_k` are gone from its signatures, and the current models
  reject them, so the contract refuses them (`params` outside `provider`, `model`, `max_tokens`, `timeout_ms`, `effort` and
  `stop_sequences` is a 400).
- **A stream's request id is in the response headers.** The final message of a stream carries none (`_request_id` is empty), so the helper
  reads `stream.response.headers["request-id"]`; every reply and terminal event carries it.
- **`fallbacks="default"` is a typed value** on the beta namespace, and the API accepted it on Opus 5.5. The helper sends it only for
  the models that support it and never on a route that cannot.
- **The deadline is `asyncio.wait_for`** around the whole call, SDK retries included; the abandoned call is cancelled, which a test
  asserts. A stream has no total deadline: `timeout_ms` is its idle allowance.

## Reproduce

```bash
pip install -e '.[dev]'
pytest tests/test_llm_helper.py                              # token-free
export ANTHROPIC_API_KEY=...                                  # live: the credential reaches the helper only
mercury-serve examples/llm-helper/llm_helper.py -Dlog.format=compact -Dllm.log.batches=true
```

The commands for the engines, the deploy folder and the `curl` calls for each layer are in the
[engine report](https://accenture.github.io/mercury-composable/test-reports/llm-helper-certification/#reproduce).
