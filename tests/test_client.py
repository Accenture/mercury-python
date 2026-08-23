"""PostOffice client tests against the in-process host (full wrapper loop)."""

from collections.abc import AsyncIterator

import pytest_asyncio
from aiohttp import web

from mercury_composable import Body, FunctionRegistry, PostOffice
from mercury_composable.server import EventApiServer
from mercury_composable.trace import trace_context


def build_registry() -> FunctionRegistry:
    registry = FunctionRegistry()

    async def echo(headers: dict[str, str], body: Body):
        return {"headers": headers, "body": body}

    async def whoami(_headers: dict[str, str], _body: Body):
        from mercury_composable import get_trace
        info = get_trace()
        assert info is not None
        return {"trace_id": info.trace_id, "trace_path": info.trace_path, "cid": info.cid}

    registry.register("client.echo", echo)
    registry.register("client.whoami", whoami)
    return registry


@pytest_asyncio.fixture
async def endpoint() -> AsyncIterator[str]:
    server = EventApiServer(build_registry())
    runner = web.AppRunner(server.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}/api/event"
    await runner.cleanup()


async def test_rpc_round_trip(endpoint: str):
    async with PostOffice(endpoint=endpoint) as po:
        reply = await po.request("client.echo", body={"hello": "world"},
                                 headers={"h1": "v1"}, timeout_ms=5000)
    assert reply.get_status() == 200
    assert reply.body["body"] == {"hello": "world"}
    assert reply.body["headers"]["h1"] == "v1"


async def test_trace_context_propagates_through_client(endpoint: str):
    with trace_context("trace-777", "TEST /client", cid="cid-42"):
        async with PostOffice(endpoint=endpoint) as po:
            reply = await po.request("client.whoami", body={}, timeout_ms=5000)
    assert reply.body == {"trace_id": "trace-777", "trace_path": "TEST /client", "cid": "cid-42"}


async def test_error_reply_is_returned_not_raised(endpoint: str):
    async with PostOffice(endpoint=endpoint) as po:
        reply = await po.request("no.such.route", body={}, timeout_ms=5000)
    assert reply.get_status() == 404
    assert reply.body == "Route no.such.route not found"


async def test_drop_n_forget_ack(endpoint: str):
    async with PostOffice(endpoint=endpoint) as po:
        ack = await po.send("client.echo", body={"fire": "forget"}, timeout_ms=5000)
    assert ack.get_status() == 202
    assert ack.body["delivered"] is True
