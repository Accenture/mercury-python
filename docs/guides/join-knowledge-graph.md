---
title: Join a Knowledge Graph
summary: Wire a Python function into a MiniGraph model - graph.task to a declarative target,
  deadlines, and the error.* contract from the function's seat.
audience: [developer, architect]
keywords: [minigraph, knowledge graph, graph.task, declarative target, error contract, x-ttl]
---

# Join a Knowledge Graph

*Join the engines: your function as a graph task in an Active Knowledge Graph.*

> **At a glance**
>
> - **What** — a `graph.task` node names your route; the same declarative map from the
>   [flow chapter](join-event-script.md) points it at your host.
> - **Requires** — engine **v4.11.11 or later** (the release that taught the
>   deployed-graph guard about the declarative map).

## The graph names the route — nothing more

In MiniGraph, behavior lives on nodes as **skills**. The `graph.task` skill invokes a
composable function by route name — and since v4.11.11 that route may resolve through
`yaml.event.over.http` to a Python host. A minimal node:

```json
{
  "types": ["Task"],
  "alias": "python-step",
  "properties": {
    "skill": "graph.task",
    "task": "hello.python",
    "input": ["input.body -> *"],
    "output": ["result -> output.body"]
  }
}
```

The graph model carries no URL and no language — deployment stays in the engine's
configuration, exactly as the composable theme demands. Everything about authoring
graphs (nodes, skills, the compile gate, the Playground) is the engine's domain:
start at [Build your first graph](https://accenture.github.io/mercury-composable/guides/knowledge-graph/build-your-first-graph/)
and the [built-in skills reference](https://accenture.github.io/mercury-composable/guides/knowledge-graph/skills-reference/).

## Deadlines: two distinct clocks

- The graph's **ttl** bounds the *event call* to your function — on breach the engine
  sees the standard 408 envelope.
- If your function itself calls onward over HTTP, that client call carries its own
  timeout — the same decoupling the engines document for `async.http.request`
  (`headers.x-ttl` in milliseconds).

Design functions so their own outbound work respects the deadline they were given:
read the caller's intent from your ttl-bounded world and fail fast rather than
overstay ([Design](design.md)).

## Errors: the graph's error.* contract

When your function raises `AppException(code, message)` — or fails unexpectedly with
a 500 — the walker stages the graph's generic error context:
`error.source` (the failing node), `error.code`, `error.message`, and `error.stack`
when one traveled. A graph can therefore route around a Python failure with the same
IF/THEN patterns it uses for any other task — see the engine's
[knowledge-graph guides](https://accenture.github.io/mercury-composable/guides/knowledge-graph/)
for the retry and recovery idioms.

## Worked demo

The quickest end-to-end proof mirrors the engines' own conformance test: a graph with
one `graph.task` node whose `task` names a route served by the Python demo app
(`hello.python` in [Getting Started](getting-started.md)), with the declarative map on
the engine pointing at `http://127.0.0.1:8086/api/event`. Deploy the graph (or dry-run
it in the Playground), `POST /api/graph/{graph-id}`, and the reply body is your
function's output — trace id visible in the Python host's log line.

## Checklist

1. Engine at v4.11.11+ with `yaml.event.over.http` configured.
2. The map entry's route == the node's `task` value == your `@preload` route.
3. The graph passes the compile gate and is listed in the manifest (deployed lane) —
   "compiled or 404" is the engine's rule.
4. Your host is healthy; `/info/routes` lists the route as `public` (a private route
   answers 403 on the wire by design).
