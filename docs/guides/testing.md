---
title: Testing Your Functions
summary: The test harness patterns this package uses on itself - registry fixtures, real-HTTP
  host tests, and the golden wire-format vectors.
audience: [developer]
keywords: [testing, pytest, pytest-asyncio, fixtures, golden vectors, wire format]
---

# Testing Your Functions

*Write functions: test them the way this package tests itself.*

> **At a glance**
>
> - **What** — three proven layers: direct handler tests, in-process bus tests, and
>   real-HTTP host tests; plus the golden vectors that pin wire compatibility.

## Layer 1 — the handler is just a function

The cheapest test needs no framework at all:

```python
def test_uppercase_contract():
    reply = handle_event({}, {"text": "polyglot"})
    assert reply == {"text": "POLYGLOT", "language": "python"}
```

## Layer 2 — through the bus, with a fresh registry

Register into a **fresh** `FunctionRegistry` per test (never the default one) and
close its bus on teardown — the pattern from this package's own `tests/test_bus.py`:

```python
import pytest_asyncio
from collections.abc import AsyncIterator
from mercury_composable import FunctionRegistry, PostOffice, trace_context

@pytest_asyncio.fixture
async def registry() -> AsyncIterator[FunctionRegistry]:
    fresh = FunctionRegistry()
    yield fresh
    await fresh.bus.close()

async def test_trace_rides_through(registry: FunctionRegistry):
    registry.register("my.function", handler)
    po = PostOffice(registry=registry)
    with trace_context("trace-1", "TEST /unit", cid="cid-1"):
        reply = await po.request("my.function", body={"text": "x"}, timeout_ms=5000)
    assert reply.get_status() == 200
```

This exercises `instances`, private routes, deadlines (assert the 408 envelope) and
trace propagation exactly as production will.

## Layer 3 — over real HTTP

Boot the host on an ephemeral port and speak the actual protocol
(`tests/test_server.py` pattern):

```python
from aiohttp import web
from mercury_composable.server import EventApiServer

server = EventApiServer(registry)
runner = web.AppRunner(server.create_app())
await runner.setup()
site = web.TCPSite(runner, "127.0.0.1", 0)
await site.start()
port = runner.addresses[0][1]
# ... aiohttp client posts envelope bytes to /api/event, or PostOffice(endpoint=...)
await runner.cleanup()
```

Use this layer to pin transport behavior: 403 for private routes, 404 messages, the
`x-async` 202 acknowledgement, reserved-header hygiene, actuator shapes.

## Wire compatibility — the golden vectors

The codec is verified against the **golden conformance vectors shared with the Java
and Rust engines** (`tests/vectors/vectors.json`). If you extend envelope handling,
run the vector suite — it is the cross-language contract:

```bash
pytest -q tests/test_envelope.py
```

## The project gates

```bash
pytest -q          # all tests
ruff check .       # lint
basedpyright       # types (tests included)
```

The CI workflow runs all three plus a strict documentation build on every push and
pull request.
