"""REST automation pins: the shared vector file over the pure half, and the edge over real HTTP -
the engines' behaviour, the Rust port's test list as the checklist (RFC-0018)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from mercury_composable import (
    AppException,
    AsyncHttpRequest,
    Body,
    EventEnvelope,
    EventStreamWriter,
    FunctionRegistry,
    app_config,
    get_trace,
    load_config,
)
from mercury_composable.rest_routing import (
    CorsInfo,
    HeaderTransform,
    RoutingTable,
    accept_fallback_type,
    error_body,
    map_response,
    negotiate_stream_type,
    parse_body,
    parse_cookies,
    parse_query,
    parse_timeout,
    render_payload,
    stream_text,
)
from mercury_composable.server import EventApiServer

Serve = Callable[[FunctionRegistry], AbstractAsyncContextManager[str]]
VECTORS = Path(__file__).parent / "vectors" / "rest-automation-vectors.json"
MY_CID = "my_correlation_id"


@pytest.fixture(scope="module")
def vectors() -> dict[str, Any]:
    with open(VECTORS, encoding="utf-8") as f:
        return json.load(f)


# ---- the shared vectors over the pure half ----


def _route_view(route: Any) -> dict[str, Any]:
    return {"service": route.service, "methods": route.methods, "url": route.url,
            "timeout_ms": route.timeout_ms, "cors": route.cors.id if route.cors else None,
            "headers": route.headers.id if route.headers else None,
            "authentication": route.authentication, "tracing": route.tracing,
            "stream": route.stream, "upload": route.upload, "trace_id_header": route.trace_id_header,
            "correlation_id_header": route.correlation_id_header,
            "traceparent_header": route.traceparent_header}


def test_vectors_load_cases(vectors: dict[str, Any]):
    for case in vectors["load_cases"]:
        if "error" in case:
            with pytest.raises(AppException) as raised:
                RoutingTable.from_yaml_text(case["yaml"])
            assert raised.value.status == 400, case["name"]
            assert raised.value.message == case["error"], case["name"]
            continue
        table = RoutingTable.from_yaml_text(case["yaml"])
        assert [_route_view(r) for r in table.routes] == case["routes"], case["name"]
        static = table.static_content
        expect_filter = case["static"]["filter"]
        assert static.no_cache_pages == case["static"]["no_cache_pages"], case["name"]
        if expect_filter is None:
            assert static.filter is None, case["name"]
        else:
            assert static.filter is not None
            assert {"path": static.filter.path_list, "exclusion": static.filter.exclusion_list,
                    "service": static.filter.service} == expect_filter, case["name"]


def test_vectors_timeout_cases(vectors: dict[str, Any]):
    for case in vectors["timeout_cases"]:
        assert parse_timeout(case["value"]) == case["ms"], case


def test_vectors_match_cases(vectors: dict[str, Any]):
    tables = {name: RoutingTable.from_yaml_text(text) for name, text in vectors["tables"].items()}
    for case in vectors["match_cases"]:
        table = tables[case["table"]]
        found = table.find(case["method"], case["path"])
        if "status" in case:
            assert found is None, case["name"]
            expected = 405 if case["status"] == 405 else 404
            actual = 405 if table.path_matches_any_method(case["path"]) else 404
            assert actual == expected, case["name"]
        else:
            assert found is not None, case["name"]
            assert found.info.service == case["service"], case["name"]
            assert found.path_params == case["params"], case["name"]


def test_vectors_header_transform_cases(vectors: dict[str, Any]):
    for case in vectors["header_transform_cases"]:
        spec = case["transform"]
        transform = HeaderTransform(add=[tuple(pair) for pair in spec.get("add", [])],
                                    drop=list(spec.get("drop", [])), keep=list(spec.get("keep", [])))
        result = transform.apply(case["headers"])
        assert {k.lower(): v for k, v in result.items()} == \
            {k.lower(): v for k, v in case["expect"].items()}, case["name"]


def test_vectors_body_cases(vectors: dict[str, Any]):
    for case in vectors["body_cases"]:
        parsed = parse_body(case["content_type"], case["body"].encode("utf-8"))
        assert parsed.kind == case["kind"], case["name"]
        value = parsed.value.decode("utf-8") if parsed.kind == "bytes" else parsed.value
        assert value == case["value"], case["name"]


def test_vectors_query_and_cookie_cases(vectors: dict[str, Any]):
    for case in vectors["query_cases"]:
        assert parse_query(case["query"]) == case["expect"], case
    for case in vectors["cookie_cases"]:
        assert parse_cookies(case["header"]) == case["expect"], case


def test_vectors_negotiation_cases(vectors: dict[str, Any]):
    for case in vectors["negotiation_cases"]:
        assert accept_fallback_type(case["accept"]) == case["type"], case
    for case in vectors["stream_negotiation_cases"]:
        assert negotiate_stream_type(case["accept"]) == case["type"], case


def _build(spec: dict[str, Any]) -> AsyncHttpRequest:
    request = AsyncHttpRequest()
    if "method" in spec:
        request.set_method(spec["method"])
    if "url" in spec:
        request.set_url(spec["url"])
    if "host" in spec:
        request.set_target_host(spec["host"])
    if "ip" in spec:
        request.set_remote_ip(spec["ip"])
    if "https" in spec:
        request.set_secure(spec["https"])
    if "timeout" in spec:
        request.set_timeout_seconds(spec["timeout"])
    for name, value in spec.get("headers", {}).items():
        request.set_header(name, value)
    for name, value in spec.get("path", {}).items():
        request.set_path_parameter(name, value)
    for name, value in spec.get("query", {}).items():
        request.set_query_parameter(name, value)
    if "body" in spec:
        request.set_body(spec["body"])
    if "query_string" in spec:
        request.set_query_string(spec["query_string"])
    for name, value in spec.get("cookies", {}).items():
        request.set_cookie(name, value)
    for name, value in spec.get("session", {}).items():
        request.set_session_info(name, value)
    for uploaded in spec.get("files", []):
        request.add_file(uploaded["name"], uploaded["content_type"],
                         base64.b64decode(uploaded["content_base64"]), uploaded["tag"])
    return request


def _with_base64(data: dict[str, Any]) -> dict[str, Any]:
    """A map's file bytes as the vector file carries them (JSON holds no bytes)."""
    if "contents" in data:
        data = dict(data)
        data["contents_base64"] = [base64.b64encode(c).decode("ascii") for c in data.pop("contents")]
    return data


def _from_base64(data: dict[str, Any]) -> dict[str, Any]:
    if "contents_base64" in data:
        data = dict(data)
        data["contents"] = [base64.b64decode(c) for c in data.pop("contents_base64")]
    return data


def _read(request: AsyncHttpRequest, key: str) -> Any:
    name, _, argument = key.partition(".")
    attribute = getattr(request, name)
    return attribute(argument) if argument else attribute


def test_vectors_request_model_cases(vectors: dict[str, Any]):
    for case in vectors["request_model_cases"]:
        assert _with_base64(_build(case["build"]).to_map()) == case["map"], case["name"]
    for case in vectors["request_read_cases"]:
        request = AsyncHttpRequest(case["map"])
        for key, expected in case["expect"].items():
            assert _read(request, key) == expected, (case["name"], key)


def test_vectors_upload_read_cases(vectors: dict[str, Any]):
    for case in vectors["upload_read_cases"]:
        request = AsyncHttpRequest(_from_base64(case["map"]))
        expect = case["expect"]
        assert request.is_file == expect["is_file"], case["name"]
        assert request.content_length == expect["content_length"], case["name"]
        assert request.upload_tags == expect["upload_tags"], case["name"]
        files = [{"name": f.name, "content_type": f.content_type, "size": f.size, "tag": f.tag,
                  "content_base64": base64.b64encode(f.content).decode("ascii")} for f in request.files()]
        assert files == expect["files"], case["name"]


def test_vectors_response_cases(vectors: dict[str, Any]):
    for case in vectors["response_cases"]:
        spec = case["transform"]
        transform = None if spec is None else HeaderTransform(
            add=[tuple(pair) for pair in spec.get("add", [])],
            drop=list(spec.get("drop", [])), keep=list(spec.get("keep", [])))
        cors_spec = case["cors"]
        cors = None if cors_spec is None else CorsInfo(
            "c", headers=[tuple(pair) for pair in cors_spec.get("headers", [])])
        mapped = map_response(status=case["status"], reply_headers=case["reply_headers"],
                              body=case["body"], accept=case["accept"], is_head=case["head"],
                              transform=transform, cors=cors, cid=case["cid"],
                              cid_header=case["cid_header"])
        assert mapped.status == case["expect"]["status"], case["name"]
        assert sorted((n.lower(), v) for n, v in mapped.headers) == \
            sorted((n.lower(), v) for n, v in case["expect"]["headers"]), case["name"]
        assert mapped.body.decode("utf-8") == case["expect"]["body"], case["name"]


def test_vectors_render_cases(vectors: dict[str, Any]):
    for case in vectors["render_cases"]:
        assert render_payload(case["body"], case["type"]).decode("utf-8") == case["text"], case
    for case in vectors["stream_text_cases"]:
        assert stream_text(case["body"]) == case["text"], case
    for case in vectors["error_body_cases"]:
        assert error_body(case["status"], case["message"]) == case["expect"]


def test_bytes_body_rides_raw():
    mapped = map_response(status=200, reply_headers={"content-type": "image/png"}, body=b"\x89PNG",
                          accept="*/*", is_head=False, transform=None, cors=None, cid="c",
                          cid_header="x-correlation-id")
    assert mapped.body == b"\x89PNG"
    assert ("content-type", "image/png") in mapped.headers


# ---- the edge over HTTP ----

REST_YAML = """\
rest:
  - service: "http.echo"
    methods: ['GET', 'POST']
    url: "/api/echo/{user}"
    timeout: 5s
    cors: cors_1
    headers: header_1
  - service: "ghost.service"
    methods: ['GET']
    url: "/api/ghost"
  - service: "plain.text"
    methods: ['GET']
    url: "/index.html"
  - service: "header.probe"
    methods: ['GET', 'HEAD']
    url: "/api/headers"
    headers: header_1
  - service: "request.view"
    methods: ['GET']
    url: "/api/request/view"
  - service: "plain.text"
    methods: ['GET']
    url: "/api/files/*"
  - service: "plain.text"
    methods: ['GET']
    url: "/api/v*/ping"
  - service: "plain.text"
    methods: ['GET']
    url: "/api/mid/*/end"
  - service: "trace.probe"
    methods: ['GET']
    url: "/api/traced"
    tracing: true
  - service: "trace.probe"
    methods: ['GET']
    url: "/api/renamed/traceparent/probe"
    tracing: true
    traceparent.header: "X-Endpoint-Trace"
  - service: "trace.header.echo"
    methods: ['GET']
    url: "/api/echo/headers"
    timeout: 5s
  - service: "plain.text"
    methods: ['GET']
    url: "/api/text"
  - service: "body.probe"
    methods: ['POST']
    url: "/api/probe"
  - service: "slow.text"
    methods: ['GET']
    url: "/api/slow"
    timeout: 1s
  - service: "http.echo"
    methods: ['GET']
    url: "/api/secure"
    authentication: "api.key.check"
  - service: "failing.service"
    methods: ['GET']
    url: "/api/fail"
  - service: "json.error.service"
    methods: ['GET']
    url: "/api/fail/json"
  - service: "envelope.probe"
    methods: ['GET']
    url: "/api/envelope/{user}"
  - service: "token.stream"
    methods: ['POST']
    url: "/api/stream"
    stream: true
    headers: header_1
  - service: "stream.fail"
    methods: ['POST']
    url: "/api/stream/fail"
    stream: true
  - service: "lines.stream"
    methods: ['POST']
    url: "/api/stream/lines"
    stream: true
  - service: "token.stream"
    methods: ['POST']
    url: "/api/stream/single"
  - service: "info.actuator.service"
    methods: ['GET']
    url: "/ops/info"
    authentication: "api.key.check"
  - service: "upload.probe"
    methods: ['POST']
    url: "/api/upload"
    upload: true
cors:
  - id: cors_1
    options:
      - "Access-Control-Allow-Origin: *"
      - "Access-Control-Allow-Methods: GET, POST, OPTIONS"
    headers:
      - "Access-Control-Allow-Origin: ${api.origin:*}"
headers:
  - id: header_1
    response:
      add: ["x-served-by: mercury"]
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@contextmanager
def configured(tmp_path: Path, rest_yaml: str | None, settings: dict[str, Any] | None = None,
               public: dict[str, str] | None = None) -> Iterator[Path]:
    """An application folder whose resources hold the rest.yaml (and a web bundle), loaded as the
    shared configuration with the settings given; a fresh default configuration afterwards."""
    folder = tmp_path / "app"
    (folder / "resources").mkdir(parents=True, exist_ok=True)
    if rest_yaml is not None:
        write(folder / "resources" / "rest.yaml", rest_yaml)
    for name, text in (public or {}).items():
        write(folder / "resources" / "public" / name, text)
    load_config(app_dir=str(folder))
    config = app_config()
    config.set("rest.automation", True)
    for key, value in (settings or {}).items():
        config.set(key, value)
    try:
        yield folder
    finally:
        load_config()


class Probe:
    """What the probe functions observed."""

    def __init__(self) -> None:
        self.trace: tuple[str | None, str | None, str | None] | None = None
        self.datasets: list[dict[str, Any]] = []


def build_registry(probe: Probe) -> FunctionRegistry:
    registry = FunctionRegistry()

    async def echo(headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        return {"user": request.path_parameter("user"), "q": request.query_parameter("q"),
                "my_cid": headers.get(MY_CID), "echo_body": request.body, "method": request.method,
                "session": request.session, "headers": request.headers}

    async def plain(_headers: dict[str, str], _body: Body):
        return "plain response"

    async def slow(_headers: dict[str, str], _body: Body):
        await asyncio.sleep(2.5)
        return "late"

    async def body_probe(_headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        value = request.body
        kind = ("missing" if not (isinstance(body, dict) and "body" in body)
                else "null" if value is None
                else "text" if isinstance(value, str) else "bytes" if isinstance(value, bytes)
                else "map" if isinstance(value, dict) else "list" if isinstance(value, list)
                else "other")
        content = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        return {"kind": kind, "content": content, "query": request.query_parameters_map}

    async def request_view(_headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        return {"q": request.query_parameters_map.get("q"), "single": request.query_parameter("single"),
                "cookie_first": request.cookie("first"), "cookie_second": request.cookie("second"),
                "cookie_header": request.header("cookie"), "query": request.query_string,
                "https": request.is_secure, "ip": request.remote_ip, "timeout": request.timeout_seconds}

    async def api_key_check(_headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        allowed = request.header("x-api-key") == "open-sesame"
        return EventEnvelope().set_header("user", "demo").set_body(allowed)

    async def header_probe(_headers: dict[str, str], _body: Body):
        return (EventEnvelope().set_status(302).set_header("Location", "/somewhere/else")
                .set_header("Content-Type", "application/vnd.demo+json")
                .set_header("Set-Cookie", "first=1; Path=/|second=2; HttpOnly")
                .set_header("X-Custom", "custom-value").set_header("x-stream-id", "stream.100.in")
                .set_header("x-ttl", "10").set_body({"moved": True}))

    async def trace_probe(headers: dict[str, str], _body: Body):
        info = get_trace()
        probe.trace = (info.trace_id if info else None, info.trace_path if info else None,
                       headers.get(MY_CID))
        return "traced"

    async def trace_header_echo(_headers: dict[str, str], body: Body):
        return {"headers": AsyncHttpRequest(body).headers}

    async def failing(_headers: dict[str, str], _body: Body):
        raise AppException(404, "nothing here")

    async def json_error(_headers: dict[str, str], _body: Body):
        return EventEnvelope().set_status(409).set_body('{"custom":true}')

    async def envelope_probe(_headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        return (EventEnvelope().set_status(202).set_header("x-typed-reply", "envelope")
                .set_body({"user": request.path_parameter("user"), "accepted": True}))

    async def token_stream(_headers: dict[str, str], event: EventEnvelope):
        out = EventStreamWriter.from_request(event, registry=registry)
        out.first(200, "text/event-stream")
        out.write("hello")
        out.write_named("tokens", {"n": 2})
        out.close({"usage": 1})

    async def lines_stream(_headers: dict[str, str], event: EventEnvelope):
        out = EventStreamWriter.from_request(event, registry=registry)
        out.first(200, "application/json")
        out.write("hello")
        out.write({"n": 2})
        out.close()

    async def stream_fail(_headers: dict[str, str], event: EventEnvelope):
        out = EventStreamWriter.from_request(event, registry=registry)
        out.first(200, "text/event-stream")
        out.write("partial")
        out.fail(AppException(503, "upstream down"))

    async def upload_probe(_headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        return {"files": [{"name": f.name, "type": f.content_type, "size": f.size, "tag": f.tag,
                           "sha256": hashlib.sha256(f.content).hexdigest()} for f in request.files()],
                "query": request.query_parameters_map, "body_present": isinstance(body, dict) and "body" in body,
                "size": request.content_length, "is_file": request.is_file}

    async def forwarder(_headers: dict[str, str], body: Body):
        if isinstance(body, dict):
            probe.datasets.append(body)

    registry.register("http.echo", echo)
    registry.register("plain.text", plain)
    registry.register("slow.text", slow)
    registry.register("body.probe", body_probe)
    registry.register("request.view", request_view)
    registry.register("api.key.check", api_key_check)
    registry.register("header.probe", header_probe)
    registry.register("trace.probe", trace_probe)
    registry.register("trace.header.echo", trace_header_echo)
    registry.register("failing.service", failing)
    registry.register("json.error.service", json_error)
    registry.register("envelope.probe", envelope_probe)
    registry.register("token.stream", token_stream, interceptor=True)
    registry.register("lines.stream", lines_stream, interceptor=True)
    registry.register("stream.fail", stream_fail, interceptor=True)
    registry.register("upload.probe", upload_probe)
    registry.register("distributed.trace.forwarder", forwarder, private=True)
    return registry


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest_asyncio.fixture
async def rest_url(tmp_path: Path, probe: Probe, serve: Serve) -> AsyncIterator[str]:
    with configured(tmp_path, REST_YAML, {"http.traceparent.header": "X-Trace-Context"}):
        async with serve(build_registry(probe)) as base:
            yield base


async def http(url: str, method: str = "GET", headers: dict[str, str] | None = None,
               data: bytes | str | None = None) -> tuple[int, list[tuple[str, str]], bytes]:
    """One exchange; the headers as lowercased (name, value) lines so repeats stay visible."""
    async with (
        aiohttp.ClientSession(skip_auto_headers=["Accept", "Accept-Encoding", "User-Agent",
                                             "Content-Type"]) as session,
        session.request(method, url, headers=headers, data=data, allow_redirects=False) as response,
    ):
        return (response.status, [(k.lower(), v) for k, v in response.headers.items()],
                await response.read())


def header(lines: list[tuple[str, str]], name: str) -> str | None:
    return next((v for k, v in lines if k == name), None)


def headers_named(lines: list[tuple[str, str]], name: str) -> list[str]:
    return [v for k, v in lines if k == name]


async def test_function_response_headers_survive_the_rest_boundary(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/headers")
    assert status == 302
    assert header(lines, "location") == "/somewhere/else"
    assert header(lines, "content-type") == "application/vnd.demo+json"
    assert header(lines, "x-custom") == "custom-value"
    assert headers_named(lines, "set-cookie") == ["first=1; Path=/", "second=2; HttpOnly"]
    assert header(lines, "x-served-by") == "mercury"
    assert header(lines, "x-stream-id") is None and header(lines, "x-ttl") is None
    assert json.loads(body) == {"moved": True}


async def test_head_response_carries_headers_but_no_body(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/headers", "HEAD")
    assert status == 302
    assert header(lines, "location") == "/somewhere/else"
    assert header(lines, "content-type") != "application/vnd.demo+json"
    assert body == b""


async def test_get_with_path_query_and_generated_cid(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/echo/eric?q=hello%20world", headers={"Accept": "application/json"})
    assert status == 200
    assert header(lines, "content-type") == "application/json"
    assert header(lines, "x-served-by") == "mercury"
    assert header(lines, "access-control-allow-origin") == "*"
    data = json.loads(body)
    assert data["user"] == "eric" and data["q"] == "hello world" and data["method"] == "GET"
    assert data["my_cid"] and data["my_cid"] == header(lines, "x-correlation-id")


async def test_response_echoes_inbound_correlation_id(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/echo/eric", headers={"X-Correlation-Id": "cid-echo-0101"})
    assert status == 200
    assert header(lines, "x-correlation-id") == "cid-echo-0101"
    assert json.loads(body)["my_cid"] == "cid-echo-0101"


async def test_post_json_body(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/echo/poster", "POST",
                                 {"Content-Type": "application/json", "X-Correlation-Id": "order-777"},
                                 '{"item":"book","qty":2}')
    assert status == 200
    data = json.loads(body)
    assert data["echo_body"] == {"item": "book", "qty": 2} and data["my_cid"] == "order-777"


async def test_body_dispatch_mirrors_the_content_type_rules(rest_url: str):
    async def probe_body(headers: dict[str, str], data: str) -> dict[str, Any]:
        status, _, body = await http(f"{rest_url}/api/probe", "POST", headers, data)
        assert status == 200, body
        return json.loads(body)

    assert (await probe_body({"Content-Type": "application/json"}, '{"item":"book"}'))["kind"] == "map"
    mislabelled = await probe_body({"Content-Type": "application/json"}, "import graph from tutorial-1")
    assert mislabelled["kind"] == "text" and mislabelled["content"] == "import graph from tutorial-1"
    empty = await probe_body({"Content-Type": "application/json"}, "")
    assert empty["kind"] == "map" and empty["content"] == {}
    unsniffed = await probe_body({"Content-Type": "text/plain"}, '{"item":"book"}')
    assert unsniffed["kind"] == "text"
    form = await probe_body({"Content-Type": "application/x-www-form-urlencoded"}, "a=1&b=hello+world")
    assert form["kind"] == "null" and form["query"] == {"a": "1", "b": "hello world"}
    raw = await probe_body({}, '{"item":"book"}')
    assert raw["kind"] == "bytes" and raw["content"] == '{"item":"book"}'


async def test_unknown_path_is_the_engines_404(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/nowhere")
    assert status == 404
    assert header(lines, "content-type") == "application/json"
    assert json.loads(body) == {"status": 404, "message": "Resource not found", "type": "error"}


async def test_entry_without_a_registered_service_is_skipped_at_start(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/ghost")
    assert status == 404 and json.loads(body)["message"] == "Resource not found"
    status, _, _ = await http(f"{rest_url}/api/echo/alice")
    assert status == 200


async def test_root_falls_back_to_the_index_html_entry(rest_url: str):
    status, _, body = await http(f"{rest_url}/index.html", headers={"Accept": "text/plain"})
    assert status == 200 and body == b"plain response"
    status, _, root = await http(f"{rest_url}/", headers={"Accept": "text/plain"})
    assert status == 200 and root == body


async def test_envelope_sets_status_and_headers(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/envelope/carol", headers={"Accept": "application/json"})
    assert status == 202
    assert header(lines, "x-typed-reply") == "envelope"
    assert json.loads(body) == {"user": "carol", "accepted": True}


async def test_function_failure_is_the_standard_error_body(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/fail")
    assert status == 404 and header(lines, "content-type") == "application/json"
    assert json.loads(body) == {"status": 404, "message": "nothing here", "type": "error"}
    status, _, body = await http(f"{rest_url}/api/fail/json")
    assert status == 409 and body == b'{"custom":true}'


async def test_cors_preflight_and_405_without_cors(rest_url: str):
    status, lines, _ = await http(f"{rest_url}/api/echo/any", "OPTIONS")
    assert status == 204
    assert header(lines, "access-control-allow-origin") == "*"
    assert "POST" in (header(lines, "access-control-allow-methods") or "")
    status, _, body = await http(f"{rest_url}/api/text", "OPTIONS")
    assert status == 405 and json.loads(body)["message"] == "Method not allowed"


async def test_known_path_wrong_method_is_405(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/text", "PUT")
    assert status == 405 and json.loads(body)["message"] == "Method not allowed"


async def test_slow_function_times_out_as_408(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/slow")
    assert status == 408
    assert json.loads(body) == {"status": 408, "message": "Timeout for 1000 ms", "type": "error"}


async def test_authentication_gate_and_session(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/secure")
    assert status == 401 and json.loads(body)["message"] == "Unauthorized"
    status, _, body = await http(f"{rest_url}/api/secure", headers={"X-Api-Key": "open-sesame"})
    assert status == 200
    assert json.loads(body)["session"] == {"user": "demo"}
    # a built-in service behind the entry's authentication
    status, _, _ = await http(f"{rest_url}/ops/info")
    assert status == 401
    status, _, body = await http(f"{rest_url}/ops/info", headers={"X-Api-Key": "open-sesame"})
    assert status == 200 and "app" in json.loads(body)


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("telemetry not seen")


async def test_edge_starts_trace_and_adopts_the_traceparent_parent(rest_url: str, probe: Probe):
    upstream_trace, upstream_span = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    status, _, _ = await http(f"{rest_url}/api/traced?flag=on", headers={"traceparent": f"00-{upstream_trace}-{upstream_span}-01",
                               "X-Correlation-Id": "biz-9"})
    assert status == 200
    assert probe.trace == (upstream_trace, "GET /api/traced?flag=on", "biz-9")

    def seen() -> bool:
        services = {d["trace"]["service"] for d in probe.datasets}
        return {"http.request", "trace.probe"} <= services
    await _wait_for(seen)
    edge = next(d["trace"] for d in probe.datasets if d["trace"]["service"] == "http.request")
    function = next(d["trace"] for d in probe.datasets if d["trace"]["service"] == "trace.probe")
    assert edge["id"] == upstream_trace and edge["parent_span_id"] == upstream_span
    assert edge["path"] == "GET /api/traced?flag=on" and edge["status"] == 200 and edge["success"]
    assert "from" not in edge
    assert function["parent_span_id"] == edge["span_id"] and function["from"] == "http.request"
    assert function["path"] == "GET /api/traced?flag=on" and "round_trip" in function


async def test_content_negotiation(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/text", headers={"Accept": "text/plain"})
    assert status == 200 and header(lines, "content-type") == "text/plain" and body == b"plain response"
    _, lines, body = await http(f"{rest_url}/api/text", headers={"Accept": "*/*"})
    assert header(lines, "content-type") == "application/json" and body == b"plain response"
    _, lines, body = await http(f"{rest_url}/api/echo/html?q=1", headers={"Accept": "text/html"})
    assert header(lines, "content-type") == "text/html"
    assert body.startswith(b"<html><body><pre>\n") and body.endswith(b"\n</pre></body></html>")
    _, lines, body = await http(f"{rest_url}/api/text")
    assert header(lines, "content-type") is None and body == b"plain response"


async def test_request_model_matches_the_engines_keys(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/request/view?q=a&q=b&single=1", headers={"Accept": "application/json", "Cookie": "first=alpha; second=beta",
                                  "X-Forwarded-Proto": "https"})
    assert status == 200
    data = json.loads(body)
    assert data["q"] == ["a", "b"] and data["single"] == "1"
    assert data["cookie_first"] == "alpha" and data["cookie_second"] == "beta"
    assert "cookie_header" not in data  # a null entry is omitted from the JSON
    assert data["query"] == "q=a&q=b&single=1" and data["https"] is True
    assert data["ip"] == "127.0.0.1" and data["timeout"] == 30


async def test_wildcard_grammar(rest_url: str):
    accept = {"Accept": "*/*"}
    assert (await http(f"{rest_url}/api/files/a/b", headers=accept))[0] == 200
    assert (await http(f"{rest_url}/api/files", headers=accept))[0] == 404
    assert (await http(f"{rest_url}/api/v2/ping", headers=accept))[0] == 200
    assert (await http(f"{rest_url}/api/mid/x/end", headers=accept))[0] == 200
    assert (await http(f"{rest_url}/api/mid/end", headers=accept))[0] == 404


async def test_traceparent_header_names(rest_url: str, probe: Probe):
    # the custom global name (http.traceparent.header) delivers the context when the standard is absent
    custom = "1af92f3577b34da6a3ce929d0e0e4701"
    await http(f"{rest_url}/api/traced", headers={"X-Trace-Context": f"00-{custom}-00f067aa0ba902b7-01"})
    assert probe.trace is not None and probe.trace[0] == custom
    # the standard traceparent wins over the custom name
    standard = "3af92f3577b34da6a3ce929d0e0e4703"
    await http(f"{rest_url}/api/traced", headers={"X-Trace-Context": "00-2af92f3577b34da6a3ce929d0e0e4702-00f067aa0ba902b7-01",
                                          "traceparent": f"00-{standard}-00f067aa0ba902b8-01"})
    assert probe.trace is not None and probe.trace[0] == standard
    # the per-endpoint override beats the global name
    endpoint = "5af92f3577b34da6a3ce929d0e0e4705"
    await http(f"{rest_url}/api/renamed/traceparent/probe", headers={"X-Endpoint-Trace": f"00-{endpoint}-00f067aa0ba902b7-01",
                "X-Trace-Context": "00-6af92f3577b34da6a3ce929d0e0e4706-00f067aa0ba902b8-01"})
    assert probe.trace is not None and probe.trace[0] == endpoint
    # no header at all: a fresh trace id, 32 hex
    await http(f"{rest_url}/api/traced")
    assert probe.trace is not None and probe.trace[0] is not None and len(probe.trace[0]) == 32


async def test_endpoint_timeout_rides_the_dataset_as_x_ttl(rest_url: str):
    _, _, body = await http(f"{rest_url}/api/echo/headers", headers={"Accept": "application/json"})
    assert json.loads(body)["headers"]["x-ttl"] == "5000"
    _, _, body = await http(f"{rest_url}/api/echo/headers", headers={"Accept": "application/json", "x-ttl": "1500"})
    assert json.loads(body)["headers"]["x-ttl"] == "1500"


async def test_stream_renders_server_sent_events(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/stream", "POST", {"Accept": "text/event-stream"})
    assert status == 200
    assert header(lines, "content-type") == "text/event-stream"
    assert header(lines, "cache-control") == "no-cache"
    assert header(lines, "x-served-by") == "mercury"
    assert header(lines, "x-correlation-id")
    text = body.decode("utf-8")
    assert "data: hello\n\n" in text
    assert "event: tokens\ndata: {\"n\":2}\n\n" in text
    assert text.endswith("event: done\ndata: {\"usage\":1}\n\n")


async def test_stream_renders_chunked_text_without_sse(rest_url: str):
    # the head's content type wins over Accept: an SSE head stays SSE under */*
    status, lines, _ = await http(f"{rest_url}/api/stream", "POST", {"Accept": "*/*"})
    assert status == 200 and header(lines, "content-type") == "text/event-stream"
    # a JSON head renders chunked: text verbatim, a structure as one JSON line, no done frame
    status, lines, body = await http(f"{rest_url}/api/stream/lines", "POST", {"Accept": "*/*"})
    assert status == 200
    assert header(lines, "content-type") == "application/json"
    assert body == b'hello{"n":2}\n'


async def test_stream_failure_in_band(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/stream/fail", "POST", {"Accept": "text/event-stream"})
    assert status == 200
    text = body.decode("utf-8")
    assert "data: partial\n\n" in text
    assert text.endswith('event: error\ndata: {"status":503,"message":"upstream down","type":"error"}\n\n')


async def test_streaming_function_without_the_stream_flag_answers_single_shot(rest_url: str):
    status, lines, body = await http(f"{rest_url}/api/stream/single", "POST", {"Accept": "text/plain"})
    assert status == 200
    # the first event completes the exchange (the engines' rule): the head's content type and
    # the first segment's text, nothing more
    assert header(lines, "content-type") == "text/event-stream"
    assert body == b"hello"


async def test_default_entries_serve_the_host_paths(rest_url: str):
    event = EventEnvelope(to="plain.text")
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{rest_url}/api/event", data=event.to_bytes(),
                     headers={"content-type": "application/octet-stream", "x-ttl": "5000"}) as response,
    ):
        assert response.status == 200
        reply = EventEnvelope.from_bytes(await response.read())
    assert reply.body == "plain response"
    status, _, body = await http(f"{rest_url}/info")
    assert status == 200 and "app" in json.loads(body)
    status, _, _ = await http(f"{rest_url}/livenessprobe")
    assert status == 200


DEFAULT_OVERRIDE_YAML = """\
rest:
  - service: "event.api.service"
    methods: ['POST']
    url: "/api/event"
    timeout: 60s
    cors: cors_1
    tracing: true
cors:
  - id: cors_1
    options:
      - "Access-Control-Allow-Origin: *"
    headers:
      - "Access-Control-Allow-Origin: *"
"""


async def test_user_entry_overrides_a_default_and_adds_cors(tmp_path: Path, probe: Probe, serve: Serve):
    with configured(tmp_path, DEFAULT_OVERRIDE_YAML):
        async with serve(build_registry(probe)) as base:
            event = EventEnvelope(to="plain.text")
            async with (
                aiohttp.ClientSession() as session,
                session.post(f"{base}/api/event", data=event.to_bytes(),
                             headers={"content-type": "application/octet-stream"}) as response,
            ):
                assert response.status == 200
                assert response.headers.get("Access-Control-Allow-Origin") == "*"
            status, lines, _ = await http(f"{base}/api/event", "OPTIONS")
            assert status == 204 and header(lines, "access-control-allow-origin") == "*"
            # the other defaults are still merged
            status, _, _ = await http(f"{base}/health")
            assert status == 200


async def test_rest_automation_off_leaves_the_host_unchanged(tmp_path: Path, probe: Probe, serve: Serve):
    with configured(tmp_path, REST_YAML, {"rest.automation": False}):
        registry = build_registry(probe)
        assert EventApiServer(registry).rest is None
        async with serve(registry) as base:
            status, _, body = await http(f"{base}/api/echo/eric")
            assert status == 404 and json.loads(body)["message"] == "Resource not found"


async def test_missing_rest_yaml_serves_the_defaults_only(tmp_path: Path, probe: Probe, serve: Serve):
    with configured(tmp_path, None):
        registry = build_registry(probe)
        server = EventApiServer(registry)
        assert server.rest is not None
        assert sorted(r.url for r in server.rest.table.routes) == \
            sorted(["/api/event", "/info", "/info/routes", "/env", "/health", "/livenessprobe"])
        async with serve(registry) as base:
            status, _, _ = await http(f"{base}/info")
            assert status == 200


def test_invalid_rest_yaml_fails_the_start(tmp_path: Path, probe: Probe):
    with configured(tmp_path, "rest:\n  - service: http.flow.adapter\n    flow: demo\n    methods: ['GET']\n    url: /x\n"):
        with pytest.raises(AppException) as raised:
            EventApiServer(build_registry(probe))
        assert "flow binding is not available in a language pack" in raised.value.message


def test_file_location_forms(tmp_path: Path, probe: Probe):
    elsewhere = tmp_path / "elsewhere" / "routes.yaml"
    write(elsewhere, "rest:\n  - service: plain.text\n    methods: ['GET']\n    url: /elsewhere\n")
    with configured(tmp_path, None, {"yaml.rest.automation": f"file:{elsewhere}"}):
        server = EventApiServer(build_registry(probe))
        assert server.rest is not None and server.rest.table.has_url("/elsewhere")


EXTENSION_YAML = """\
rest:
  - service: "plain.text"
    methods: ['GET']
    url: "/ext/claimed"
"""


async def test_entries_win_over_extension_routes_and_files(tmp_path: Path, probe: Probe):
    public = {"index.html": "<h1>bundle</h1>", "about.html": "<h1>about</h1>",
              "style.css": "body {}", "ext/claimed": "a file at an entry's path"}
    with configured(tmp_path, EXTENSION_YAML, public=public):
        registry = build_registry(probe)

        async def claimed(_request: web.Request) -> web.Response:
            return web.Response(text="extension")

        async def free(_request: web.Request) -> web.Response:
            return web.Response(text="extension free")

        def mount(app: web.Application) -> None:
            app.router.add_get("/ext/claimed", claimed)
            app.router.add_get("/ext/free", free)

        runner = web.AppRunner(EventApiServer(registry).create_app([mount]))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            _, _, body = await http(f"{base}/ext/claimed", headers={"Accept": "text/plain"})
            assert body == b"plain response"
            _, _, body = await http(f"{base}/ext/free")
            assert body == b"extension free"
            status, _, body = await http(f"{base}/about.html")
            assert status == 200 and body == b"<h1>about</h1>"
        finally:
            await runner.cleanup()


STATIC_YAML = """\
rest:
  - service: "plain.text"
    methods: ['GET']
    url: "/api/text"
static-content:
  no-cache-pages: ["/", "/index.html"]
  filter:
    path: ["/", "/private/*", "*.html"]
    exclusion: ["*.css"]
    service: "http.request.filter"
"""


async def test_static_content_filter_and_no_cache_pages(tmp_path: Path, probe: Probe, serve: Serve):
    public = {"index.html": "<h1>bundle</h1>", "about.html": "<h1>about</h1>",
              "style.css": "body {}", "private/page.html": "<h1>private</h1>"}
    with configured(tmp_path, STATIC_YAML, public=public):
        registry = build_registry(probe)
        seen: list[dict[str, Any]] = []

        async def http_filter(_headers: dict[str, str], body: Body):
            request = AsyncHttpRequest(body)
            seen.append(request.to_map())
            if request.url.startswith("/private/"):
                return EventEnvelope().set_status(302).set_header("Location", "/login")
            return EventEnvelope().set_header("x-filter", "demo")

        registry.register("http.request.filter", http_filter)
        async with serve(registry) as base:
            status, lines, body = await http(f"{base}/about.html")
            assert status == 200 and body == b"<h1>about</h1>"
            assert header(lines, "x-filter") == "demo"
            assert header(lines, "etag") is not None and header(lines, "pragma") is None
            assert seen[-1]["method"] == "GET" and seen[-1]["url"] == "/about.html"
            assert seen[-1]["body"] is None and "headers" in seen[-1]
            status, lines, _ = await http(f"{base}/style.css")
            assert status == 200 and header(lines, "x-filter") is None
            status, lines, body = await http(f"{base}/private/page.html")
            assert status == 302 and header(lines, "location") == "/login" and body == b""
            status, lines, body = await http(f"{base}/")
            assert status == 200 and body == b"<h1>bundle</h1>"
            assert header(lines, "cache-control") == "no-cache, no-store"
            assert header(lines, "pragma") == "no-cache"
            assert header(lines, "expires") == "Thu, 01 Jan 1970 00:00:00 GMT"
            status, _, body = await http(f"{base}/api/text", headers={"Accept": "text/plain"})
            assert status == 200 and body == b"plain response"
            status, _, body = await http(f"{base}/missing.html")
            assert status == 404 and json.loads(body)["message"] == "Resource not found"


def test_cors_lines_resolve_configuration_references(tmp_path: Path, probe: Probe):
    os.environ["REST_TEST_ORIGIN"] = "https://app.example"
    try:
        yaml_text = REST_YAML.replace("${api.origin:*}", "${REST_TEST_ORIGIN:*}")
        with configured(tmp_path, yaml_text):
            server = EventApiServer(build_registry(probe))
            assert server.rest is not None
            entry = server.rest.table.find("GET", "/api/echo/x")
            assert entry is not None and entry.info.cors is not None
            assert entry.info.cors.headers == [("Access-Control-Allow-Origin", "https://app.example")]
    finally:
        del os.environ["REST_TEST_ORIGIN"]


# ---- upload: the bounded form of the engines' streaming upload ----

PDF_BYTES = b"%PDF-1.4 a small document"
PNG_BYTES = b"\x89PNG\r\n\x1a\n a small image"


def _form() -> aiohttp.FormData:
    data = aiohttp.FormData()
    data.add_field("doc", PDF_BYTES, filename="report.pdf", content_type="application/pdf")
    data.add_field("image", PNG_BYTES, filename="page.png", content_type="image/png")
    data.add_field("note", "hello world")
    return data


async def post_form(url: str, data: aiohttp.FormData) -> tuple[int, bytes]:
    async with aiohttp.ClientSession() as session, session.post(url, data=data) as response:
        return response.status, await response.read()


async def test_multipart_upload_delivers_the_files_and_the_fields(rest_url: str):
    status, body = await post_form(f"{rest_url}/api/upload", _form())
    assert status == 200, body
    data = json.loads(body)
    assert data["files"] == [
        {"name": "report.pdf", "type": "application/pdf", "size": len(PDF_BYTES), "tag": "doc",
         "sha256": hashlib.sha256(PDF_BYTES).hexdigest()},
        {"name": "page.png", "type": "image/png", "size": len(PNG_BYTES), "tag": "image",
         "sha256": hashlib.sha256(PNG_BYTES).hexdigest()},
    ]
    assert data["query"] == {"note": "hello world"}
    assert data["size"] == len(PDF_BYTES) + len(PNG_BYTES) and data["is_file"] is True
    assert data["body_present"] is False


async def test_a_json_post_to_an_upload_entry_dispatches_as_usual(rest_url: str):
    status, _, body = await http(f"{rest_url}/api/upload", "POST", {"Content-Type": "application/json"},
                                 '{"a": 1}')
    assert status == 200
    data = json.loads(body)
    assert data["files"] == [] and data["is_file"] is False and data["body_present"] is True


async def test_multipart_to_an_entry_without_upload_is_bytes(rest_url: str):
    status, body = await post_form(f"{rest_url}/api/probe", _form())
    assert status == 200
    data = json.loads(body)
    assert data["kind"] == "bytes" and "report.pdf" in data["content"]


async def test_an_upload_over_the_bound_is_413(tmp_path: Path, probe: Probe, serve: Serve):
    with configured(tmp_path, REST_YAML, {"rest.upload.max.bytes": 40}):
        async with serve(build_registry(probe)) as base:
            status, body = await post_form(f"{base}/api/upload", _form())
            assert status == 413
            assert json.loads(body) == {"status": 413, "message": "Upload exceeds 40 bytes", "type": "error"}
