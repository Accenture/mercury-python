---
title: Test Report — OpenTelemetry Certification, four runtimes
summary: Permanent record of the live four-runtime OpenTelemetry drive of 2026-09-22 - the
  Java and Rust engines rendering Gemini tokens progressively through this host's llm.stream
  and its Node.js twin, every application forwarding its spans to Dynatrace, one trace per request.
layer: reference
audience: [developer, architect]
keywords: [opentelemetry, otlp, dynatrace, distributed trace, llm.stream, test report]
---

# Test Report — OpenTelemetry Certification, four runtimes

*The live drive of 2026-09-22 (UTC) that certified this host's
[OpenTelemetry forwarder](../guides/config-logging-actuators.md#distributed-tracing-the-opentelemetry-forwarder-opt-in)
in company: the Java and Rust engines' Playground edges rendering real Gemini tokens progressively,
the tokens produced by this host's `llm.stream` AI node (and by the Node.js host's twin), every
application forwarding its spans to the same Dynatrace tenant under its own service name — one trace
per request across an engine and a polyglot function host. The twin record on the engines is
Scenario 8 of their `otel-dynatrace-certification` reports.*

## What was driven

The forwarder exists on all four runtimes: the Java `opentelemetry-forwarder` module, the Rust port's
`mercury-opentelemetry-forwarder`, and the two zero-dependency ports of the Rust OTLP encoder merged
that day — this host (PR #33) and the Node.js host (mercury-nodejs #101). The maintainer's scenario is
the agent-orchestration experiment E0 stretched across them: `POST /api/llm/stream` on an engine's
Playground relays its reply lane into the event-over-http mapped `llm.stream` on a host, and the
provider's token batches re-render progressively out the engine's edge; `POST
/api/graph/support-triage` runs the E0 graph whose `llm.chat` node is a `graph.task` on the host.

This host ran `mercury-serve examples/demo_app.py` on `:8086` with `-Dotel.forwarding=true`,
`-Dotel.service.name=mercury-otel-cert-python`, `-Dllm.provider=gemini -Dllm.model=gemini-3.6-flash`,
the OTLP endpoint and credential from the environment (`OTLP_API_ENDPOINT`, `OTLP_AUTH_HEADER`,
`OTLP_TOKEN` — the demo `application.yml` wiring) and `GEMINI_API_KEY`. The Java Playground (4.12.14,
`:8085`) and the Rust Playground (its E0 twin, `:8090`) forwarded as `mercury-otel-cert-java` and
`mercury-otel-cert-rust`; the Node.js host (`:8087`) as `mercury-otel-cert-node`. Every request
carried a caller-set `traceparent`.

## The traces through this host

| Edge → this host | Trace | `llm.stream` span start (UTC) | Token frames | Outcome |
|------------------|-------|-------------------------------|--------------|---------|
| **Java → Python** | `c90af9e36d8dbd3c2390db240b406d3a` | 17:36:37.175Z | 2 + `done` | `STOP`, 34 output tokens, 5.8 s |
| **Rust → Python** | `a9686f1f87327466e46cc451ff34b319` | 17:32:57.146Z | 2 + `done` | `STOP`, 21.2 s |

The graph verdict through this host: Java → Python `372b040e245495158330fdb09b412ada` (17:32:29Z,
label `bug`). The `done` frame of each stream carried the model, `stop_reason`, usage and the trace
and business correlation ids — the continuity is self-documenting in the edge's output. (The Node.js
twin carried the other two pairings: Java → Node `888a3f721907d31a9b0ec9836b2e580a`, Rust → Node
`1232ab83511f3402a519e65a80e1a144`.)

**The lineage, read from both sides' own datasets.** The engine's relay span is the parent of this
host's `llm.stream` span, and this host's span is the parent of the engine's reply-lane deliveries —
the same ids in two applications' logs:

```text
Rust → Python a9686f1f…   rust   llm.stream.relay             b05fccf48da0d67a
                          python llm.stream                   02a46cfc25802ae5  (parent b05f…, 21.2 s)
                          rust   async.http.response.stream.0 9f9206c70ec1e37e  (parent 02a4…)
                          rust   async.http.response.stream.0 ad15a089c5a72849  (parent 02a4…)
Java → Python c90af9e3…   java   llm.stream.relay             babc1831e6f0d231
                          python llm.stream                   8bd6892d99b278d8  (parent babc…, 5.8 s)
                          java   async.http.response.stream.0 813208e56d985f29  (parent 8bd6…)
                          java   async.http.response.stream.0 902070e822a557d6  (parent 8bd6…)
```

**Exports.** Zero export failures in this host (and in every other application) in every run — five
drives, 24 LLM calls. This host exported 1–2 spans per round: its `llm.stream` executions. The
graph's `llm.chat` is an RPC leg, which folds into the caller's span on every runtime, so no span is
exported for it here — the engines' own rule, and the same one the `distributed.tracing` log follows.

## Observations

- **The provider, not the pipeline, decided which calls succeeded.** Gemini answered `503 This model
  is currently experiencing high demand` on roughly half the calls across the drives, `429
  RESOURCE_EXHAUSTED` once, and `gemini-2.5-flash` proved retired (`no longer available to new users`,
  its 404 text recommending `gemini-3.6-flash`, which then answered); the stable alias
  `gemini-flash-latest` — now the demo's default — was the one under demand, so the drives pinned
  `gemini-3.6-flash` with `-Dllm.model`. Every failure was itself a trace: this host rendered the
  provider's status through the portable error contract (`LLM provider error - 503 ...`), the edge
  returned it, and the forwarders exported those spans too.
- **The current flash models think before they answer.** A 200-token budget was spent entirely on
  reasoning (`stop_reason: MAX_TOKENS`, `output_tokens: 0`, an empty stream); 1000 tokens rendered two
  token frames and a `STOP`. A streaming AI node's budget is a certification setting, not a default.
- **What remains:** the backend's view — the maintainer's Dynatrace lookup of the traces above, each
  expected to show two services with the parentage the datasets assert, this host's spans under the
  instrumentation scope `mercury-composable-python`.
