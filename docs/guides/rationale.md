---
title: Rationale
summary: Why externalized functions exist - the composable theme extended across language
  boundaries, and the boundaries that keep it honest.
audience: [architect, developer]
keywords: [rationale, polyglot, event over http, orchestration, distributed monolith]
---

# Rationale — Externalized Functions

*Foundations: the thinking before the how.*

> **At a glance**
>
> - **What** — why Python functions join Mercury flows and graphs over Event-over-HTTP,
>   and why the wrapper refuses to become a second orchestrator.
> - **For** architects deciding where a piece of logic should live.

## The composable theme does not stop at the JVM boundary

Mercury's core promise is that **functions are self-contained and coupled only by
route names and event envelopes**. A flow that reads

```yaml
process: 'hello.python'
```

does not know — and must not care — whether that route resolves to a Java method on a
virtual thread, a Rust function, or a Python handler in another process. The route
name is the contract; everything else is deployment detail.

Externalized functions take that promise literally. The
[Event-over-HTTP seam](https://accenture.github.io/mercury-composable/guides/event-over-http/)
already existed for engine-to-engine calls: the same event envelope, serialized in a
language-neutral wire format, carried over HTTP. A Python function host is simply
another peer speaking that protocol — nothing new crosses the wire.

## Why you would externalize a function

**Ecosystem gravity.** Python's strength is its libraries: `requests`-style
integrations, NumPy/pandas, scikit-learn and the ML inference stacks, domain SDKs.
Porting that logic into Java rarely adds value; wrapping it as a route does. The
function stays where its ecosystem lives; the orchestration stays where the
architecture lives.

**Team reality.** Enterprise installations are polyglot for the long haul. A data
science team ships Python; the platform team runs engine applications. Route names are
the only interface the two must agree on — a one-line YAML entry, not a shared
codebase.

**Independent lifecycles.** A Python host deploys, scales and restarts on its own
cadence as an ordinary Kubernetes pod — with the engines' own actuator surface, so
operations sees one shape everywhere.

## Why the wrapper owns nothing but functions

The tempting mistake is to grow the wrapper into a mini-engine — local flows, retries,
queues, persistence. Mercury's architecture deliberately says no:

- **Orchestration lives in the engines.** Event Script and MiniGraph carry the retry
  logic, branching, exception routing and state. A leaf that re-implements recovery
  fights the tier that owns it.
- **Back-pressure belongs to the tier that owns recovery.** The host's internal event
  bus has *no spill tier and no queue cap*: a call that cannot be served by its
  deadline fails fast with the standard 408 envelope, and the engine's flow decides
  what happens next. A leaf that hoards work merely hides congestion from the layer
  designed to handle it.
- **Sync-over-async across services is an anti-pattern by default.** The engines' own
  documentation warns that superimposing synchronous coupling on distributed parts
  builds a *distributed monolith*. The wrapper inherits that judgment: local
  composition is for simple leaf-side helpers; workflow processing belongs in flows
  and graphs.

The result is a package small enough to audit in an afternoon — an envelope codec, a
host, a primitive bus, a thin client, and the engines' operational conventions — with
the entire orchestration brain kept where it already is.

## When *not* to externalize

- **Orchestration-shaped logic** — sequencing, branching, compensation: write a flow
  or a graph, not a Python function that calls other functions in a loop.
- **Latency-critical hot paths** — an in-engine function call is an in-memory event;
  an externalized one is an HTTP round trip. Microseconds versus milliseconds.
- **Logic that is really data mapping** — Event Script's data mapping and MiniGraph's
  built-in skills often eliminate the function entirely.

## Where to go next

[Design — The Wrapper Anatomy](design.md) shows how these principles become the five
small components of the package.
