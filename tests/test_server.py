"""Event API host tests: engine-mirrored semantics over real HTTP."""

import asyncio

import aiohttp
import msgpack
import pytest_asyncio
from aiohttp import web

from mercury_composable import (
    AppException,
    EventEnvelope,
    FunctionRegistry,
    annotate_trace,
    get_trace,
)
from mercury_composable.server import EventApiServer

OCTET = "application/octet-stream"


def build_registry() -> FunctionRegistry:
    registry = FunctionRegistry()

    async def echo(headers, body):
        return {"headers": headers, "body": body}

    def upper(headers, body):  # synchronous handler runs in the executor
        info = get_trace()
        return {"text": str(body.get("text", "")).upper(),
                "trace_id": info.trace_id if info else None,
                "cid": info.cid if info else None}

    async def annotated(headers, body):
        annotate_trace("checked", "yes")
        return {"ok": True}

    async def app_error(headers, body):
        raise AppException(400, "missing 'text'")

    async def boom(headers, body):
        raise RuntimeError("kaboom")

    async def slow(headers, body):
        await asyncio.sleep(5)
        return {"late": True}

    async def secret(headers, body):
        return {"secret": True}

    registry.register("unit.echo", echo)
    registry.register("unit.upper", upper)
    registry.register("unit.annotated", annotated)
    registry.register("unit.app.error", app_error)
    registry.register("unit.boom", boom)
    registry.register("unit.slow", slow)
    registry.register("unit.secret", secret, private=True)
    return registry


@pytest_asyncio.fixture
async def server_url(aiohttp_server=None):
    server = EventApiServer(build_registry())
    runner = web.AppRunner(server.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def post_event(url: str, event: EventEnvelope, *, ttl="10000", extra=None):
    headers = {"content-type": OCTET, "x-ttl": ttl}
    headers.update(extra or {})
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{url}/api/event", data=event.to_bytes(), headers=headers) as response,
    ):
        return response.status, EventEnvelope.from_bytes(await response.read())


async def test_rpc_success_with_exec_time(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="unit.echo", body={"a": 1}))
    assert status == 200
    assert reply.get_status() == 200
    assert reply.body["body"] == {"a": 1}
    assert reply.sender == "unit.echo"
    assert reply.exec_time is not None and reply.exec_time >= 0


async def test_sync_handler_sees_trace_context(server_url):
    event = EventEnvelope(to="unit.upper", body={"text": "hello"})
    event.set_trace("trace-100", "TEST /upper").set_correlation_id("cid-9")
    status, reply = await post_event(server_url, event)
    assert status == 200
    assert reply.body == {"text": "HELLO", "trace_id": "trace-100", "cid": "cid-9"}


async def test_reserved_header_hygiene_and_my_cid_injection(server_url):
    event = EventEnvelope(to="unit.echo", body={})
    event.set_header("x-event-api", "callback").set_header("my_secret", "x")
    event.set_header("content-type", "application/json")
    event.tags["my_cid"] = "biz-123"
    _, reply = await post_event(server_url, event)
    delivered = reply.body["headers"]
    assert "x-event-api" not in delivered
    assert "my_secret" not in delivered
    assert delivered["my_correlation_id"] == "biz-123"
    assert delivered["content-type"] == "application/json"


async def test_annotations_ride_the_reply(server_url):
    _, reply = await post_event(server_url, EventEnvelope(to="unit.annotated", body={}))
    assert reply.annotations == {"checked": "yes"}


async def test_app_exception_is_portable_error_on_http_200(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="unit.app.error", body={}))
    assert status == 200  # handler-level errors ride HTTP 200, engine-style
    assert reply.get_status() == 400
    assert reply.body == "missing 'text'"
    assert reply.stack is None


async def test_unexpected_exception_maps_to_500_with_stack(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="unit.boom", body={}))
    assert status == 200
    assert reply.get_status() == 500
    assert reply.body == "kaboom"
    assert reply.stack is not None
    assert "RuntimeError" in reply.stack


async def test_unknown_route_404(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="no.where", body={}))
    assert status == 404
    assert reply.get_status() == 404
    assert reply.body == "Route no.where not found"


async def test_private_route_403(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="unit.secret", body={}))
    assert status == 403
    assert reply.body == "unit.secret is private"


async def test_missing_routing_path_400(server_url):
    status, reply = await post_event(server_url, EventEnvelope(body={"x": 1}))
    assert status == 400
    assert reply.body == "Missing routing path"


async def test_compact_request_rejected_400(server_url):
    compact = msgpack.packb({"0": "e1", "T": "unit.echo"}, use_bin_type=True)
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{server_url}/api/event", data=compact,
                     headers={"content-type": OCTET, "x-ttl": "5000"}) as response,
    ):
        assert response.status == 400
        reply = EventEnvelope.from_bytes(await response.read())
    assert reply.get_status() == 400
    assert "standard" in str(reply.body)


async def test_timeout_408_mirrors_engine_message(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="unit.slow", body={}),
                                     ttl="1000")
    assert status == 408
    assert reply.get_status() == 408
    assert reply.body == "Timeout for 1000 ms"


async def test_async_drop_n_forget_202_ack(server_url):
    status, reply = await post_event(server_url, EventEnvelope(to="unit.echo", body={}),
                                     extra={"x-async": "true"})
    assert status == 202
    assert reply.get_status() == 202
    assert reply.body["type"] == "async"
    assert reply.body["delivered"] is True
    assert "time" in reply.body


async def test_health_endpoint(server_url):
    async with (
        aiohttp.ClientSession() as session,
        session.get(f"{server_url}/health") as response,
    ):
        assert response.status == 200
        assert await response.text() == "OK"
