"""Event streaming tests: the multi-shot reply contract and the envelope-mode
SSE dialect over real HTTP - the wrapper half of the engines' Phase 2/3 matrix
(Java EventOverHttpStreamTest / Rust event_over_http_stream twins)."""

import asyncio
import base64
import re
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from mercury_composable import (
    AppException,
    Body,
    EventEnvelope,
    EventStreamWriter,
    FunctionRegistry,
    PostOffice,
    event_stream,
    trace_context,
)
from mercury_composable.server import EventApiServer

OCTET = "application/octet-stream"
SSE = "text/event-stream"
REFUSAL = "Streaming function requires a caller that accepts text/event-stream"

# the relay fixture learns its own host URL after the server binds
_relay_target: dict[str, str] = {}


def build_registry() -> FunctionRegistry:
    registry = FunctionRegistry()

    async def tokens(headers: dict[str, str], event: EventEnvelope):
        out = EventStreamWriter.from_request(event, registry=registry)
        mode = headers.get("mode", "tokens")
        if mode == "tokens":
            out.first(200, SSE)
            out.write("alpha")
            await asyncio.sleep(0.25)
            out.write("beta")
            await asyncio.sleep(0.25)
            out.close({"segments": 2})
        elif mode == "typed":
            # every escape-hatch trigger: a dict body, text with a carriage
            # return, a user event name colliding with the reserved word, a
            # binary body - plus one plain token that rides a raw frame
            out.first(200, SSE)
            out.write({"n": 1})
            out.write_named("crlf", "line1\r\nline2")
            out.write_named("envelope", "reserved-name")
            out.write(b"\x01\x02\x03\x04")
            out.write("plain token")
            out.close({"done": True})
        elif mode == "error-mid":
            out.first(200, SSE)
            out.write("partial")
            out.fail(AppException(503, "backend on fire"))
        elif mode == "error-first":
            out.fail(AppException(503, "no backend"))
        elif mode == "stall":
            # one-second declared idle allowance, then silence - the host must
            # fail the stream in-band
            out.first(200, SSE, ttl_seconds=1)
            out.write("one")
        elif mode == "crash-before":
            raise RuntimeError("kaboom before head")
        elif mode == "crash-mid":
            out.first(200, SSE)
            out.write("early")
            raise RuntimeError("kaboom mid-stream")
        elif mode == "manual":
            # a single-shot manual answer from an interceptor
            reply = EventEnvelope(to=event.reply_to, body={"manual": True})
            if event.cid:
                reply.set_correlation_id(event.cid)
            registry.send_event(reply)
        elif mode == "biz":
            # echo the injected business correlation-id view and the span
            # lineage of this execution (continuity proof)
            from mercury_composable import get_trace
            info = get_trace()
            out.first(200, SSE)
            out.close({"my_correlation_id": headers.get("my_correlation_id"),
                       "span_id": info.span_id if info else None,
                       "parent_span_id": info.parent_span_id if info else None})

    async def relay(_headers: dict[str, str], event: EventEnvelope):
        # the composition: forward MY caller's reply address into a call
        # against a remote streaming function - segments flow through verbatim
        po = PostOffice(registry=registry)
        try:
            await po.stream_to("unit.tokens", None, reply_to=event.reply_to or "",
                               endpoint=_relay_target.get("url"),
                               timeout_ms=10000, cid=event.cid)
        finally:
            await po.close()

    async def echo(_headers: dict[str, str], body: Body):
        return {"echo": body}

    async def biz(headers: dict[str, str], _body: Body):
        return {"my_correlation_id": headers.get("my_correlation_id")}

    registry.register("unit.tokens", tokens, interceptor=True)
    registry.register("unit.relay", relay, interceptor=True)
    registry.register("unit.echo", echo)
    registry.register("unit.biz", biz)
    return registry


@pytest_asyncio.fixture
async def stream_host() -> AsyncIterator[tuple[str, FunctionRegistry]]:
    registry = build_registry()
    server = EventApiServer(registry)
    runner = web.AppRunner(server.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    url = f"http://127.0.0.1:{port}/api/event"
    _relay_target["url"] = url
    yield url, registry
    await runner.cleanup()
    await registry.bus.close()


async def collect(po: PostOffice, route: str, url: str | None, *,
                  mode: str | None = None, timeout_ms: int = 10000,
                  cid: str = "cid-100") -> list[EventEnvelope]:
    headers = {"mode": mode} if mode else None
    events = []
    async for event in po.stream(route, None, headers=headers, timeout_ms=timeout_ms,
                                 endpoint=url, cid=cid):
        events.append(event)
    return events


def marker(event: EventEnvelope) -> str | None:
    return event_stream.stream_signal(event)


# ---- the host produces the envelope-mode dialect ----


async def test_streaming_target_relays_progressively(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url)
    assert len(events) == 3, "2 data envelopes + eof"
    head = events[0]
    assert marker(head) == "data"
    assert head.get_status() == 200
    assert head.headers.get("content-type") == SSE
    assert head.body == "alpha"
    assert head.cid == "cid-100", "original correlation id restored"
    assert events[1].body == "beta"
    eof = events[2]
    assert marker(eof) == "eof"
    assert eof.body == {"segments": 2}


async def test_wire_is_the_hybrid_dialect(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    # raw wire pin: the head and the terminal ride envelope frames; the plain
    # text token rides a raw frame
    event = EventEnvelope(to="unit.tokens").set_header("mode", "tokens")
    headers = {"content-type": OCTET, "x-ttl": "10000", "accept": SSE}
    async with (
        aiohttp.ClientSession() as session,
        session.post(url, data=event.to_bytes(), headers=headers) as response,
    ):
        assert response.status == 200
        assert response.headers["content-type"].startswith(SSE)
        assert response.headers["cache-control"] == "no-cache"
        text = (await response.read()).decode("utf-8")
    frames = [f for f in text.split("\n\n") if f.strip()]
    assert frames[0].startswith("event: envelope\n"), "head control rides an envelope frame"
    assert "data: beta" in frames, "a plain token rides a raw frame"
    assert frames[-1].startswith("event: envelope\n"), "the terminal is an envelope frame"
    # the terminal decodes to the eof envelope with its exact metadata
    encoded = frames[-1].split("data: ", 1)[1]
    terminal = EventEnvelope.from_bytes(base64.b64decode(encoded))
    assert marker(terminal) == "eof"
    assert terminal.body == {"segments": 2}
    # host-internal addressing never leaks to the wire
    assert terminal.to is None
    assert terminal.reply_to is None


async def test_typed_segments_round_trip_exactly(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url, mode="typed")
    assert len(events) == 6, "5 data envelopes + eof"
    assert events[0].body == {"n": 1}
    crlf = events[1]
    assert event_stream.stream_event_name(crlf) == "crlf"
    assert crlf.body == "line1\r\nline2", "carriage return preserved"
    reserved = events[2]
    assert event_stream.stream_event_name(reserved) == "envelope", \
        "a user event name colliding with the reserved word survives"
    assert reserved.body == "reserved-name"
    assert events[3].body == b"\x01\x02\x03\x04", "binary body preserved"
    assert events[4].body == "plain token"
    assert marker(events[5]) == "eof"
    assert events[5].body == {"done": True}


async def test_single_shot_over_capable_path_is_classic(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        # an interceptor's manual single-shot answer
        events = await collect(po, "unit.tokens", url, mode="manual")
        assert len(events) == 1
        assert marker(events[0]) is None
        assert events[0].body == {"manual": True}
        assert events[0].cid == "cid-100"
        # a plain (non-interceptor) function - opting in is always safe
        events = await collect(po, "unit.echo", url)
        assert len(events) == 1
        assert events[0].body == {"echo": None}


async def test_business_cid_rides_the_streaming_hop(stream_host: tuple[str, FunctionRegistry]):
    # the caller's business correlation-id (the my_cid tag) crosses the HTTP
    # hop and is injected as the my_correlation_id header view at delivery
    url, _ = stream_host
    async with PostOffice() as po:
        with trace_context("biz-trace-1", "TEST /stream", my_correlation_id="biz-42"):
            events = await collect(po, "unit.tokens", url, mode="biz")
    assert marker(events[-1]) == "eof"
    assert events[-1].body["my_correlation_id"] == "biz-42"


async def test_span_lineage_continues_across_the_hop(stream_host: tuple[str, FunctionRegistry]):
    # the engines' span model: the caller's span rides the outbound envelope;
    # the receiving execution mints its own span with the caller's as parent
    url, _ = stream_host
    caller_span = "ab" * 8
    async with PostOffice() as po:
        with trace_context("4bf92f3577b34da6a3ce929d0e0e4746", "TEST /lineage",
                           span_id=caller_span):
            events = await collect(po, "unit.tokens", url, mode="biz")
    body = events[-1].body
    assert body["parent_span_id"] == caller_span
    assert body["span_id"] != caller_span
    assert re.fullmatch(r"[0-9a-f]{16}", body["span_id"]), "16-hex W3C-shaped span"


async def test_trace_dataset_emitted_with_engine_shape(
        stream_host: tuple[str, FunctionRegistry],
        caplog: pytest.LogCaptureFixture):
    # non-RPC executions emit the engines' distributed-trace dataset record;
    # RPC round-trips are suppressed (their metrics fold into the caller)
    url, registry = stream_host
    caller_span = "cd" * 8
    with caplog.at_level("INFO", logger="distributed.tracing"):
        async with PostOffice() as po:
            with trace_context("4bf92f3577b34da6a3ce929d0e0e4747", "TEST /telemetry",
                               span_id=caller_span):
                await collect(po, "unit.tokens", url, mode="biz")
                rpc = await PostOffice(registry=registry).request(
                    "unit.biz", None, timeout_ms=5000)
                assert rpc.get_status() == 200
    def trace_of(message: object) -> dict[str, Any] | None:
        # a dataset record's message is {"trace": {...}[, "annotations": ...]}
        section = message.get("trace") if isinstance(message, dict) else None
        return section if isinstance(section, dict) else None

    sections = (trace_of(r.msg) for r in caplog.records
                if r.name == "distributed.tracing")
    traces = [t for t in sections if t is not None]
    services = [t["service"] for t in traces]
    assert "unit.biz" not in services, "RPC legs emit no dataset (engine parity)"
    tokens = [t for t in traces if t["service"] == "unit.tokens"]
    assert len(tokens) == 1
    trace = tokens[0]
    assert trace["id"] == "4bf92f3577b34da6a3ce929d0e0e4747"
    assert trace["path"] == "TEST /telemetry"
    assert trace["parent_span_id"] == caller_span
    assert trace["success"] is True
    assert trace["status"] == 200
    # an anonymous /api/event caller: the host fills the sender with its own
    # identity, the engines' EventApiService parity
    assert trace["from"] == "event.api.service"
    for key in ("origin", "start", "exec_time", "span_id"):
        assert key in trace, f"engine dataset key {key}"


async def test_business_cid_injected_on_local_delivery(stream_host: tuple[str, FunctionRegistry]):
    # engine WorkerHandler parity: local bus deliveries inject the read-only
    # view too, and no context means no injection
    _, registry = stream_host
    po = PostOffice(registry=registry)
    with trace_context("biz-trace-2", "TEST /local", my_correlation_id="biz-7"):
        reply = await po.request("unit.biz", None, timeout_ms=5000)
    assert reply.body == {"my_correlation_id": "biz-7"}
    plain = await po.request("unit.biz", None, timeout_ms=5000)
    assert plain.body == {"my_correlation_id": None}


async def test_streaming_target_without_accept_is_refused_406(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        reply = await po.request("unit.tokens", None, headers={"mode": "tokens"},
                                 endpoint=url, timeout_ms=5000)
    assert reply.get_status() == 406
    assert reply.body == REFUSAL


async def test_local_rpc_to_streaming_target_is_refused_406(stream_host: tuple[str, FunctionRegistry]):
    _, registry = stream_host
    po = PostOffice(registry=registry)
    reply = await po.request("unit.tokens", None, headers={"mode": "tokens"},
                             timeout_ms=5000)
    assert reply.get_status() == 406
    assert reply.body == REFUSAL


async def test_mid_stream_failure_propagates_exact_status(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url, mode="error-mid")
    assert events[0].body == "partial"
    error = events[-1]
    assert marker(error) == "exception"
    assert error.get_status() == 503
    # the standard error key-values: '{"type": "error", "status": n, "message": text}'
    assert error.body == {"type": "error", "status": 503, "message": "backend on fire"}


async def test_failure_before_first_segment_arrives_as_exception(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url, mode="error-first")
    assert len(events) == 1
    error = events[0]
    assert marker(error) == "exception"
    assert error.get_status() == 503
    assert error.body["message"] == "no backend"


async def test_interceptor_crash_before_head_is_the_classic_error(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url, mode="crash-before")
    assert len(events) == 1
    assert marker(events[0]) is None, "an unstarted stream fails single-shot"
    assert events[0].get_status() == 500
    assert events[0].body == "kaboom before head"


async def test_interceptor_crash_mid_stream_fails_in_band(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url, mode="crash-mid")
    assert events[0].body == "early"
    error = events[-1]
    assert marker(error) == "exception"
    assert error.get_status() == 500
    assert error.body["message"] == "kaboom mid-stream"


async def test_idle_stall_fails_in_band_408(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    started = asyncio.get_running_loop().time()
    async with PostOffice() as po:
        events = await collect(po, "unit.tokens", url, mode="stall", timeout_ms=10000)
    elapsed = asyncio.get_running_loop().time() - started
    assert events[0].body == "one"
    error = events[-1]
    assert marker(error) == "exception"
    assert error.get_status() == 408
    assert error.body["message"] == "Timeout for 1 seconds"
    assert elapsed < 8, f"the producer's 1s idle allowance governs, took {elapsed:.1f}s"


async def test_relay_composition_streams_through(stream_host: tuple[str, FunctionRegistry]):
    url, _ = stream_host
    # the flagship: unit.relay forwards its caller's reply address into a call
    # against the remote streaming function - engine-parity composition
    async with PostOffice() as po:
        events = await collect(po, "unit.relay", url, cid="cid-relay")
    assert [e.body for e in events] == ["alpha", "beta", {"segments": 2}]
    assert marker(events[-1]) == "eof"
    assert events[0].cid == "cid-relay", "the original correlation id rides the chain"


async def test_local_stream_uses_the_same_contract(stream_host: tuple[str, FunctionRegistry]):
    _, registry = stream_host
    po = PostOffice(registry=registry)
    events = []
    async for event in po.stream("unit.tokens", None, timeout_ms=10000, cid="cid-local"):
        events.append(event)
    assert [e.body for e in events] == ["alpha", "beta", {"segments": 2}]
    assert marker(events[0]) == "data"
    assert marker(events[-1]) == "eof"


# ---- the client guards the dialect against a misbehaving peer ----


@pytest_asyncio.fixture
async def misbehaving_peer() -> AsyncIterator[str]:
    def envelope_frame_text(event: EventEnvelope) -> str:
        encoded = base64.b64encode(event.to_bytes()).decode("ascii")
        return f"event: envelope\ndata: {encoded}\n\n"

    head = envelope_frame_text(
        EventEnvelope(body="mock-head")
        .set_header("x-event-stream", "data").set_header("content-type", SSE)
        .set_status(200))
    eof = envelope_frame_text(
        EventEnvelope(body={"done": True}).set_header("x-event-stream", "eof"))

    async def handle(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        response.headers["content-type"] = SSE
        await response.prepare(request)
        if request.path == "/mock/raw-first":
            await response.write(b"data: hello\n\n")
        elif request.path == "/mock/no-terminal":
            await response.write(head.encode())
        elif request.path == "/mock/foreign-dialect":
            payload = head + "data: mock-token\n\n" + eof + "data: trailing-noise\n\n"
            await response.write(payload.encode())
        elif request.path == "/mock/silent":
            await response.write(head.encode())
            await asyncio.sleep(5)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/mock/{tail:.*}", handle)
    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    await runner.cleanup()


async def test_raw_first_frame_is_rejected(misbehaving_peer: str):
    async with PostOffice() as po:
        events = await collect(po, "any.route", f"{misbehaving_peer}/mock/raw-first")
    assert len(events) == 1
    assert marker(events[0]) == "exception"
    assert events[0].get_status() == 500
    assert events[0].body["message"] == "Invalid event stream - missing envelope head"


async def test_transport_end_without_terminal_is_truncation(misbehaving_peer: str):
    async with PostOffice() as po:
        events = await collect(po, "any.route", f"{misbehaving_peer}/mock/no-terminal")
    assert events[0].body == "mock-head"
    error = events[-1]
    assert marker(error) == "exception"
    assert error.get_status() == 500
    assert error.body["message"] == "Event stream ended without eof"


async def test_foreign_dialect_works_and_trailing_frames_drop(misbehaving_peer: str):
    async with PostOffice() as po:
        events = await collect(po, "any.route", f"{misbehaving_peer}/mock/foreign-dialect")
    assert [e.body for e in events] == ["mock-head", "mock-token", {"done": True}]
    assert marker(events[1]) == "data", "a raw token after the head is a data segment"
    assert marker(events[2]) == "eof"


async def test_client_idle_guard_fails_in_band(misbehaving_peer: str):
    async with PostOffice() as po:
        events = await collect(po, "any.route", f"{misbehaving_peer}/mock/silent",
                                timeout_ms=2000)
    assert events[0].body == "mock-head"
    error = events[-1]
    assert marker(error) == "exception"
    assert error.get_status() == 408
    assert error.body["message"] == "Timeout for 2 seconds"
