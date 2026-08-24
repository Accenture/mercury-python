---
title: Getting Started
summary: A running Python function in five minutes - hosted, probed, and called from a
  Mercury engine flow.
audience: [developer]
keywords: [quick start, preload, mercury-serve, event over http]
---

# Getting Started

*Guide: from zero to a Python function an engine can orchestrate.*

> **At a glance**
>
> - **What** — install the package, write one function, serve it, call it — first with
>   `curl`, then from a real engine flow.
> - **Time** — about five minutes.

## 1. Install

```bash
git clone https://github.com/Accenture/mercury-python.git
cd mercury-python
pip install -e '.[dev]'
```

*(Pre-release: the package installs from source until the PyPI release.)*

## 2. Write a function

A function is a plain handler registered under a **route name** — the only address the
rest of the system will ever know it by.

```python
# app.py
from mercury_composable import AppException, Body, platform, preload

@preload(route="hello.python", instances=10)
def handle_event(headers: dict[str, str], body: Body):
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        raise AppException(400, "missing 'text'")
    return {"text": body["text"].upper(), "language": "python"}

if __name__ == "__main__":
    platform.run()
```

Plain `def` is fine — blocking code (a `requests` call, a NumPy computation) runs in a
thread pool and can never stall the host. `async def` works too. The
[Function Writing Patterns](function-patterns.md) guide covers when to use which.

## 3. Configure (the engines' convention)

```yaml
# resources/application.yml
application.name: 'hello-app'
rest.server.port: 8086
```

Configuration lives in a `resources` folder, exactly like the engines, and any key can
be overridden at run time with the engines' `-D` syntax.

## 4. Serve it

```bash
mercury-serve app.py
```

```text
2026-08-24 10:15:30.123 INFO  mercury.server:124 - Loaded PUBLIC hello.python, instances=10
2026-08-24 10:15:30.124 INFO  mercury.server:126 - hello-app - Event API service started on port 8086
```

Open <http://127.0.0.1:8086/> — the host serves the engines' familiar index page, and
the same actuator endpoints (`/info`, `/health`, `/livenessprobe`, …) your operations
team already monitors on engine apps.

## 5. Call it from an engine

One declarative entry in the engine application tells it where the route lives —
`application.properties`:

```properties
yaml.event.over.http=classpath:/event-over-http.yaml
```

`event-over-http.yaml`:

```yaml
event.http:
  - route: 'hello.python'
    target: 'http://127.0.0.1:8086/api/event'
```

Any Event Script task or MiniGraph `graph.task` node that names `hello.python` now
executes your Python function — trace context, correlation id and error contract
carried end to end. [Join an Event Script Flow](join-event-script.md) walks through a
complete flow; [Join a Knowledge Graph](join-knowledge-graph.md) does the same for a
graph.

## 6. Or just curl it

The host speaks the engines' Event API protocol (envelope bytes over
`POST /api/event`), so the natural ad-hoc client is the package itself:

```python
import asyncio
from mercury_composable import PostOffice

async def main():
    async with PostOffice(endpoint="http://127.0.0.1:8086/api/event") as po:
        reply = await po.request("hello.python", body={"text": "polyglot"}, timeout_ms=5000)
        print(reply.get_status(), reply.body)

asyncio.run(main())
```

```text
200 {'text': 'POLYGLOT', 'language': 'python'}
```

## Next

- The **why**: [Rationale — Externalized Functions](rationale.md)
- The **how, in depth**: [Function Writing Patterns](function-patterns.md)
- The **wiring**: [Join an Event Script Flow](join-event-script.md)
