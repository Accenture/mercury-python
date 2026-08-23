"""Primitive event bus pins: local RPC (public+private), FIFO, workers, deadlines."""

import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio

from mercury_composable import Body, EventEnvelope, FunctionRegistry, PostOffice, trace_context
from mercury_composable.trace import get_trace


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[FunctionRegistry]:
    fresh = FunctionRegistry()
    yield fresh
    await fresh.bus.close()


async def test_local_rpc_to_public_route(registry: FunctionRegistry):
    async def echo(headers: dict[str, str], body: Body):
        return {"headers": headers, "body": body}

    registry.register("bus.echo", echo)
    po = PostOffice(registry=registry)
    reply = await po.request("bus.echo", body={"a": 1}, headers={"h1": "v1"}, timeout_ms=5000)
    assert reply.get_status() == 200
    assert reply.body["body"] == {"a": 1}
    # local delivery passes headers verbatim (hygiene is a wire-ingress concern)
    assert reply.body["headers"] == {"h1": "v1"}
    assert reply.sender == "bus.echo"
    assert reply.exec_time is not None


async def test_local_rpc_reaches_private_route(registry: FunctionRegistry):
    # the engines' semantics: private = callable in-app only; the HTTP host
    # still answers 403 for private targets (pinned in test_server)
    async def secret(_headers: dict[str, str], _body: Body):
        return {"secret": "ok"}

    registry.register("bus.secret", secret, private=True)
    po = PostOffice(registry=registry)
    reply = await po.request("bus.secret", body={}, timeout_ms=5000)
    assert reply.get_status() == 200
    assert reply.body == {"secret": "ok"}


async def test_unregistered_local_route_404(registry: FunctionRegistry):
    po = PostOffice(registry=registry)
    reply = await po.request("bus.no.where", body={}, timeout_ms=5000)
    assert reply.get_status() == 404
    assert reply.body == "Route bus.no.where not found"


async def test_fifo_ordering_with_one_worker(registry: FunctionRegistry):
    processed: list[int] = []
    done = asyncio.Event()

    async def collector(_headers: dict[str, str], body: Body):
        assert isinstance(body, dict)
        processed.append(int(str(body["n"])))
        if len(processed) == 3:
            done.set()

    registry.register("bus.fifo", collector, instances=1)
    po = PostOffice(registry=registry)
    for n in (1, 2, 3):
        ack = await po.send("bus.fifo", body={"n": n})
        assert ack.get_status() == 202
        assert ack.body["delivered"] is True
    await asyncio.wait_for(done.wait(), timeout=5)
    assert processed == [1, 2, 3]


async def test_instances_bounds_concurrency(registry: FunctionRegistry):
    active = 0
    peak = 0

    async def slow(_headers: dict[str, str], _body: Body):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.15)
        active -= 1
        return {"ok": True}

    registry.register("bus.slow", slow, instances=2)
    po = PostOffice(registry=registry)
    replies = await asyncio.gather(*(
        po.request("bus.slow", body={}, timeout_ms=5000) for _ in range(4)))
    assert all(r.get_status() == 200 for r in replies)
    assert peak == 2  # instances = the number of concurrent workers, faithfully


async def test_local_timeout_408_and_dead_work_skip(registry: FunctionRegistry):
    executed: list[str] = []
    release = asyncio.Event()

    async def gate(_headers: dict[str, str], body: Body):
        assert isinstance(body, dict)
        executed.append(str(body["id"]))
        await release.wait()
        return {"ok": True}

    registry.register("bus.gate", gate, instances=1)
    po = PostOffice(registry=registry)
    first = asyncio.create_task(po.request("bus.gate", body={"id": "first"}, timeout_ms=5000))
    await asyncio.sleep(0.05)  # the single worker is now blocked inside 'first'
    # the second RPC waits in the mailbox and times out before a worker frees up
    reply = await po.request("bus.gate", body={"id": "second"}, timeout_ms=200)
    assert reply.get_status() == 408
    assert reply.body == "Timeout for 200 ms"
    release.set()
    assert (await first).get_status() == 200
    await asyncio.sleep(0.05)  # give the worker a chance to reach the dead delivery
    # dead-work check: the timed-out delivery was skipped, never executed
    assert executed == ["first"]


async def test_trace_chain_through_local_private_sibling(registry: FunctionRegistry):
    async def helper(_headers: dict[str, str], _body: Body):
        info = get_trace()
        assert info is not None
        return {"helper_trace": info.trace_id, "helper_cid": info.cid}

    async def entry(_headers: dict[str, str], _body: Body):
        info = get_trace()
        assert info is not None
        inner_po = PostOffice(registry=registry)
        inner = await inner_po.request("bus.helper", body={}, timeout_ms=5000)
        assert isinstance(inner.body, dict)
        return {"entry_trace": info.trace_id, **inner.body}

    registry.register("bus.helper", helper, private=True)
    registry.register("bus.entry", entry)
    po = PostOffice(registry=registry)
    with trace_context("trace-bus-1", "TEST /bus", cid="cid-bus-1"):
        reply = await po.request("bus.entry", body={}, timeout_ms=5000)
    assert reply.get_status() == 200
    # one trace id flows: caller context -> entry handler -> private helper
    assert reply.body == {"entry_trace": "trace-bus-1", "helper_trace": "trace-bus-1",
                          "helper_cid": "cid-bus-1"}


async def test_local_send_returns_ack_envelope(registry: FunctionRegistry):
    seen = asyncio.Event()

    async def sink(_headers: dict[str, str], _body: Body):
        seen.set()

    registry.register("bus.sink", sink)
    po = PostOffice(registry=registry)
    ack = await po.send("bus.sink", body={"fire": "forget"})
    assert isinstance(ack, EventEnvelope)
    assert ack.get_status() == 202
    assert ack.body["type"] == "async"
    await asyncio.wait_for(seen.wait(), timeout=5)
