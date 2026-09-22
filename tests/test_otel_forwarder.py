"""The OpenTelemetry forwarder: the OTLP protobuf wire format, the dataset ->
span mapping, the exporter's retry and diagnostics against a mock collector,
and the host hook that hands every emitted dataset to the extension route -
the Python twin of the engines' forwarder tests."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

from mercury_composable import AppConfig, Body, FunctionRegistry, __version__
from mercury_composable.otel import (
    DISTRIBUTED_TRACE_FORWARDER,
    INSTRUMENTATION_SCOPE,
    Exporter,
    ExportFailure,
    ForwarderSettings,
    activate,
    describe_http_failure,
    otlp,
    parse_headers,
    span_from_dataset,
)
from mercury_composable.otel.span import (
    KIND_INTERNAL,
    KIND_SERVER,
    STATUS_ERROR,
    STATUS_OK,
    parse_iso8601_nanos,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
PARENT_SPAN_ID = "b7ad6b7169203331"
FAST_BACKOFF = (10, 10, 10, 10)


def sample_dataset(**overrides: Any) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "origin": "node-1", "id": TRACE_ID, "path": "/api/hello", "service": "hello.world",
        "start": "2026-06-24T10:00:00.000Z", "success": True, "from": "http.request",
        "exec_time": 12.5, "status": 200, "span_id": SPAN_ID, "parent_span_id": PARENT_SPAN_ID,
    }
    trace.update(overrides)
    return {"trace": trace, "annotations": {"user": "alice"}}


# ---------------------------------------------------------------------------
# a decoding test collector (the twin of the Rust tests' MockCollector)
# ---------------------------------------------------------------------------


@dataclass
class DecodedSpan:
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None
    name: str = ""
    kind: int = 0
    start_unix_nano: int = 0
    end_unix_nano: int = 0
    flags: int = 0
    status_code: int = 0
    status_message: str = ""
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class DecodedRequest:
    service_name: str | None = None
    scope_name: str | None = None
    scope_version: str | None = None
    spans: list[DecodedSpan] = field(default_factory=list)


def _read_key_value(body: bytes) -> tuple[str, str]:
    r = otlp.ProtoReader(body)
    key, value = "", ""
    while r.has_more():
        f, w = r.read_tag() or (0, 0)
        if f == 1:
            key = r.read_string() or ""
        elif f == 2:
            inner = otlp.ProtoReader(r.read_bytes() or b"")
            while inner.has_more():
                vf, vw = inner.read_tag() or (0, 0)
                if vf == 1:
                    value = inner.read_string() or ""
                elif vf == 2:
                    value = "true" if inner.read_varint() else "false"
                elif vf == 3:
                    value = str(inner.read_varint())
                elif vf == 4:
                    number = inner.read_double() or 0.0
                    value = str(int(number)) if number.is_integer() else str(number)
                else:
                    inner.skip(vw)
        else:
            r.skip(w)
    return key, value


def _read_span(body: bytes) -> DecodedSpan:
    r = otlp.ProtoReader(body)
    span = DecodedSpan()
    while r.has_more():
        f, w = r.read_tag() or (0, 0)
        if f == 1:
            span.trace_id = (r.read_bytes() or b"").hex()
        elif f == 2:
            span.span_id = (r.read_bytes() or b"").hex()
        elif f == 4:
            span.parent_span_id = (r.read_bytes() or b"").hex()
        elif f == 5:
            span.name = r.read_string() or ""
        elif f == 6:
            span.kind = r.read_varint() or 0
        elif f == 7:
            span.start_unix_nano = r.read_fixed64() or 0
        elif f == 8:
            span.end_unix_nano = r.read_fixed64() or 0
        elif f == 9:
            key, value = _read_key_value(r.read_bytes() or b"")
            span.attributes[key] = value
        elif f == 15:
            inner = otlp.ProtoReader(r.read_bytes() or b"")
            while inner.has_more():
                sf, sw = inner.read_tag() or (0, 0)
                if sf == 2:
                    span.status_message = inner.read_string() or ""
                elif sf == 3:
                    span.status_code = inner.read_varint() or 0
                else:
                    inner.skip(sw)
        elif f == 16:
            span.flags = r.read_fixed32() or 0
        else:
            r.skip(w)
    return span


def decode_request(body: bytes) -> DecodedRequest:
    """Walk ExportTraceServiceRequest -> ResourceSpans -> ScopeSpans -> Span."""
    out = DecodedRequest()
    r = otlp.ProtoReader(body)
    while r.has_more():
        f, w = r.read_tag() or (0, 0)
        if f != 1:
            r.skip(w)
            continue
        rs = otlp.ProtoReader(r.read_bytes() or b"")
        while rs.has_more():
            rf, rw = rs.read_tag() or (0, 0)
            if rf == 1:  # Resource
                res = otlp.ProtoReader(rs.read_bytes() or b"")
                while res.has_more():
                    af, aw = res.read_tag() or (0, 0)
                    if af == 1:
                        key, value = _read_key_value(res.read_bytes() or b"")
                        if key == "service.name":
                            out.service_name = value
                    else:
                        res.skip(aw)
            elif rf == 2:  # ScopeSpans
                ss = otlp.ProtoReader(rs.read_bytes() or b"")
                while ss.has_more():
                    sf, sw = ss.read_tag() or (0, 0)
                    if sf == 1:
                        scope = otlp.ProtoReader(ss.read_bytes() or b"")
                        while scope.has_more():
                            cf, cw = scope.read_tag() or (0, 0)
                            if cf == 1:
                                out.scope_name = scope.read_string()
                            elif cf == 2:
                                out.scope_version = scope.read_string()
                            else:
                                scope.skip(cw)
                    elif sf == 2:
                        out.spans.append(_read_span(ss.read_bytes() or b""))
                    else:
                        ss.skip(sw)
            else:
                rs.skip(rw)
    return out


@dataclass
class Captured:
    path: str
    headers: dict[str, str]
    request: DecodedRequest


class MockCollector:
    """An OTLP/HTTP collector double: decodes every request it receives and
    answers a scripted status list (consumed in order), then 200."""

    def __init__(self) -> None:
        self.captured: list[Captured] = []
        self.script: list[tuple[int, bytes]] = []
        self.requests = 0
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("POST", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests += 1
        body = await request.read()
        self.captured.append(Captured(path=request.path,
                                      headers={k.lower(): v for k, v in request.headers.items()},
                                      request=decode_request(body)))
        if self.script:
            status, payload = self.script.pop(0)
            return web.Response(status=status, body=payload)
        return web.Response(status=200, body=b"")


@pytest_asyncio.fixture
async def collector() -> AsyncIterator[MockCollector]:
    mock = MockCollector()
    await mock.start()
    yield mock
    await mock.stop()


def exporter_for(collector: MockCollector, path: str, headers: list[tuple[str, str]],
                 service_name: str = "mercury-otel-demo") -> Exporter:
    settings = ForwarderSettings.fixed(collector.url(path), timeout_ms=2000, headers=headers,
                                       service_name=service_name)
    return Exporter(settings, backoff_ms=FAST_BACKOFF)


# ---------------------------------------------------------------------------
# headers, mapping, encoding
# ---------------------------------------------------------------------------


def test_header_parsing_accepts_both_forms_and_keeps_tokens_whole() -> None:
    assert parse_headers(None) == []
    assert parse_headers("  ") == []
    assert parse_headers("null") == []
    assert parse_headers("Authorization=Api-Token abc=def, X-Tenant: t=1") == [
        ("Authorization", "Api-Token abc=def"), ("X-Tenant", "t=1")]
    assert parse_headers("Authorization: Api-Token x:y") == [("Authorization", "Api-Token x:y")]
    # a repeated name keeps the last value; a pair without a separator is dropped
    assert parse_headers("a=1,a=2,junk,=nokey") == [("a", "2")]


def test_span_mapping_preserves_the_ids_and_the_metrics() -> None:
    span = span_from_dataset(sample_dataset())
    assert span is not None
    assert span.trace_id_hex == TRACE_ID
    assert span.span_id_hex == SPAN_ID
    assert span.parent_span_id_hex == PARENT_SPAN_ID
    assert span.name == "hello.world"
    # a function execution is an INTERNAL hop, even the first one (from=http.request)
    assert span.kind == KIND_INTERNAL
    assert span.status_code == STATUS_OK
    assert span.start_unix_nano == 1_782_295_200_000_000_000
    assert span.end_unix_nano - span.start_unix_nano == 12_500_000
    assert span.attribute("route") == "hello.world"
    assert span.attribute("path") == "/api/hello"
    assert span.attribute("from") == "http.request"
    assert span.attribute("origin") == "node-1"
    assert span.attribute("status") == 200
    assert span.attribute("exec_time_ms") == 12.5
    assert span.attribute("annotation.user") == "alice"


def test_span_mapping_failure_and_root_span_and_internal_kind() -> None:
    dataset = sample_dataset(success=False, exception="boom", status=500, parent_span_id=None)
    dataset["trace"]["from"] = "hello.caller"
    span = span_from_dataset(dataset)
    assert span is not None
    assert span.parent_span_id is None
    assert span.kind == KIND_INTERNAL
    assert span.status_code == STATUS_ERROR
    assert span.status_message == "boom"
    assert span.attribute("exception") == "boom"
    # no exception text: the status number describes the error
    span2 = span_from_dataset(sample_dataset(success=False, status=503))
    assert span2 is not None and span2.status_message == "status=503"


def test_span_mapping_skips_non_w3c_ids_and_shapeless_datasets() -> None:
    assert span_from_dataset({"trace": {"id": "not-hex", "span_id": SPAN_ID}}) is None
    assert span_from_dataset(sample_dataset(id="0" * 32)) is None
    assert span_from_dataset(sample_dataset(span_id="ABCDEF0123456789")) is None  # upper case
    assert span_from_dataset({"annotations": {}}) is None
    assert span_from_dataset("text") is None


def test_iso8601_parsing() -> None:
    assert parse_iso8601_nanos("2026-06-24T10:00:00Z") == 1_782_295_200_000_000_000
    assert parse_iso8601_nanos("2026-06-24T10:00:00.5Z") == 1_782_295_200_500_000_000
    assert parse_iso8601_nanos("1970-01-01T00:00:00.000000001Z") == 1
    for bad in ("2026-06-24 10:00:00Z", "2026-13-01T00:00:00Z", "2026-06-24T10:00:00",
                "2026-06-24T10:00:00.Z", "2026-06-24T10:00:00.0123456789Z"):
        assert parse_iso8601_nanos(bad) is None, bad


def test_encoding_round_trips_through_the_reader() -> None:
    span = span_from_dataset(sample_dataset())
    assert span is not None
    body = otlp.encode_export_request("svc", INSTRUMENTATION_SCOPE, __version__, span)
    decoded = decode_request(body)
    assert decoded.service_name == "svc"
    assert decoded.scope_name == INSTRUMENTATION_SCOPE
    assert decoded.scope_version == __version__
    assert len(decoded.spans) == 1
    got = decoded.spans[0]
    assert (got.trace_id, got.span_id, got.parent_span_id) == (TRACE_ID, SPAN_ID, PARENT_SPAN_ID)
    # a function execution encodes as kind 1 (INTERNAL); only the edge's round-trip record is SERVER
    assert got.name == "hello.world" and got.kind == 1 and got.status_code == 1
    assert got.flags == otlp.SPAN_FLAGS_SAMPLED_LOCAL
    assert got.start_unix_nano == 1_782_295_200_000_000_000
    assert got.end_unix_nano - got.start_unix_nano == 12_500_000
    assert got.attributes["status"] == "200"
    assert got.attributes["exec_time_ms"] == "12.5"
    assert got.attributes["annotation.user"] == "alice"


def test_partial_success_reader() -> None:
    w = otlp.ProtoWriter()
    inner = otlp.ProtoWriter()
    inner.int64_always(1, 3)
    inner.string(2, "3 spans had no name")
    w.message(1, inner.into_bytes())
    partial = otlp.partial_success(w.into_bytes())
    assert partial is not None
    assert (partial.rejected_spans, partial.error_message) == (3, "3 spans had no name")
    assert otlp.partial_success(b"") is None
    empty = otlp.ProtoWriter()
    empty.message(1, b"")
    assert otlp.partial_success(empty.into_bytes()) is None


def test_failure_descriptions_carry_hints_and_bound_the_body() -> None:
    text = describe_http_failure(401, "Token   Authentication\nfailed")
    assert text.startswith("HTTP 401 - Token Authentication failed | the backend rejected")
    assert "signal path" in describe_http_failure(404, "")
    assert describe_http_failure(500, "x" * 300).endswith("...")
    assert describe_http_failure(418, "") == "HTTP 418"


def test_a_misconfigured_endpoint_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="otel.exporter.otlp.endpoint"):
        Exporter(ForwarderSettings.fixed("localhost:4318/v1/traces"))
    with pytest.raises(ValueError):
        Exporter(ForwarderSettings.fixed("ftp://collector/v1/traces"))


# ---------------------------------------------------------------------------
# the exporter against the collector
# ---------------------------------------------------------------------------


async def test_exporter_delivers_the_span_and_the_credential(collector: MockCollector) -> None:
    for path in ("/api/v2/otlp/v1/traces", "/v2/trace/otlp"):
        collector.captured.clear()
        exporter = exporter_for(collector, path, [("Authorization", "Api-Token test-secret")])
        span = span_from_dataset(sample_dataset())
        assert span is not None
        try:
            await exporter.export(span)
        finally:
            await exporter.close()
        assert len(collector.captured) == 1
        request = collector.captured[0]
        assert request.path == path
        assert request.headers["content-type"] == "application/x-protobuf"
        assert request.headers["authorization"] == "Api-Token test-secret"
        assert request.request.service_name == "mercury-otel-demo"
        assert request.request.scope_name == INSTRUMENTATION_SCOPE
        assert request.request.scope_version == __version__
        assert len(request.request.spans) == 1
        got = request.request.spans[0]
        assert (got.trace_id, got.span_id, got.parent_span_id) == (TRACE_ID, SPAN_ID,
                                                                    PARENT_SPAN_ID)


async def test_transient_statuses_are_retried_to_success(collector: MockCollector) -> None:
    collector.script = [(503, b"busy"), (429, b"slow down")]
    exporter = exporter_for(collector, "/v1/traces", [])
    span = span_from_dataset(sample_dataset())
    assert span is not None
    try:
        await exporter.export(span)
    finally:
        await exporter.close()
    assert collector.requests == 3, "two retryable answers, then the success"


async def test_final_rejections_are_not_retried_and_name_their_cause(
        collector: MockCollector) -> None:
    collector.script = [(401, b"Token Authentication failed")]
    exporter = exporter_for(collector, "/v1/traces", [("Authorization", "Api-Token wrong")])
    span = span_from_dataset(sample_dataset())
    assert span is not None
    try:
        with pytest.raises(ExportFailure) as failure:
            await exporter.export(span)
    finally:
        await exporter.close()
    assert failure.value.attempts == 1
    assert str(failure.value).startswith("HTTP 401 - Token Authentication failed | ")
    assert collector.requests == 1


async def test_a_refused_connection_is_retried_then_reported() -> None:
    # nothing listens on port 1: every attempt is a transport failure
    settings = ForwarderSettings.fixed("http://127.0.0.1:1/v1/traces", timeout_ms=1000)
    exporter = Exporter(settings, backoff_ms=(10, 10))
    span = span_from_dataset(sample_dataset())
    assert span is not None
    try:
        with pytest.raises(ExportFailure) as failure:
            await exporter.export(span)
    finally:
        await exporter.close()
    assert failure.value.attempts == 3
    assert "(after 3 attempts)" in str(failure.value)


async def test_the_credential_is_re_read_on_every_export(collector: MockCollector) -> None:
    config = AppConfig(argv=[])
    config.set("otel.forwarding", "true")
    config.set("otel.exporter.otlp.endpoint", collector.url("/v1/traces"))
    config.set("otel.service.name", "late-credential-app")
    exporter = Exporter(ForwarderSettings.from_config(config), backoff_ms=FAST_BACKOFF)
    span = span_from_dataset(sample_dataset())
    assert span is not None
    try:
        assert exporter.header_names() == []
        await exporter.export(span)
        assert "authorization" not in collector.captured[0].headers
        # the credential bootstrap publishes the header after start-up
        config.set("otel.exporter.otlp.headers", "Authorization=Api-Token published-later")
        await exporter.export(span)
    finally:
        await exporter.close()
    assert collector.captured[1].headers["authorization"] == "Api-Token published-later"
    assert collector.captured[1].request.service_name == "late-credential-app"


# ---------------------------------------------------------------------------
# activation and the host hook
# ---------------------------------------------------------------------------


def _config(**keys: str) -> AppConfig:
    config = AppConfig(argv=[])
    for key, value in keys.items():
        config.set(key.replace("_", "."), value)
    return config


def test_the_switch_is_the_only_thing_that_turns_it_on() -> None:
    registry = FunctionRegistry()
    assert activate(_config(), registry) is None
    assert activate(_config(otel_forwarding="false"), registry) is None
    assert not registry.exists(DISTRIBUTED_TRACE_FORWARDER)
    exporter = activate(_config(otel_forwarding="true",
                                otel_exporter_otlp_endpoint="http://127.0.0.1:4318/v1/traces"),
                        registry)
    assert exporter is not None
    service = registry.get(DISTRIBUTED_TRACE_FORWARDER)
    assert service is not None and service.private and service.instances == 2
    # an application's own forwarder on the route wins
    other = FunctionRegistry()

    async def mine(_headers: dict[str, str], _body: Body) -> None:
        return None

    other.register(DISTRIBUTED_TRACE_FORWARDER, mine)
    assert activate(_config(otel_forwarding="true"), other) is None


def test_a_bad_endpoint_fails_the_activation() -> None:
    with pytest.raises(ValueError):
        activate(_config(otel_forwarding="true", otel_exporter_otlp_endpoint="nowhere"),
                 FunctionRegistry())


async def test_the_host_hands_every_dataset_to_the_forwarder(collector: MockCollector) -> None:
    registry = FunctionRegistry()
    seen: list[str] = []

    async def hello(_headers: dict[str, str], body: Body) -> dict[str, Any]:
        seen.append(str(body))
        return {"ok": True}

    registry.register("unit.hello", hello)
    config = _config(otel_forwarding="true", otel_exporter_otlp_endpoint=collector.url("/v1/traces"),
                     otel_service_name="python-host")
    exporter = activate(config, registry)
    assert exporter is not None
    service = registry.get("unit.hello")
    assert service is not None
    try:
        # a traced drop-n-forget execution emits a dataset (an RPC would fold into the caller)
        registry.bus.publish(service, {}, {"n": 1}, trace_id=TRACE_ID, trace_path="PY /test")
        await _wait_for(lambda: len(collector.captured) >= 1)
    finally:
        await exporter.close()
        await registry.bus.close()
    assert seen == ["{'n': 1}"]
    got = collector.captured[0].request
    assert got.service_name == "python-host"
    assert len(got.spans) == 1
    span = got.spans[0]
    assert span.trace_id == TRACE_ID and len(span.span_id) == 16
    assert span.name == "unit.hello" and span.status_code == 1
    assert span.attributes["route"] == "unit.hello"
    # the forwarder's own execution is untraced: exactly one span reached the collector
    await asyncio.sleep(0.2)
    assert len(collector.captured) == 1


async def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_edge_round_trip_record_is_the_server_span() -> None:
    # an engine's REST automation emits one record per traced request with service
    # "http.request" - the round trip from receipt to the completed response; it is the
    # SERVER span and the first function's parent (the same rule as the engines' forwarders)
    dataset = sample_dataset(service="http.request", path="GET /api/hello", exec_time=2016.0)
    del dataset["trace"]["from"]
    span = span_from_dataset(dataset)
    assert span is not None
    assert span.name == "http.request"
    assert span.kind == KIND_SERVER
    assert span.end_unix_nano - span.start_unix_nano == 2_016_000_000
    assert span.attribute("path") == "GET /api/hello"
    assert span.attribute("from") is None
