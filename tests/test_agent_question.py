"""The agent-question app (extensions/agent-question/agent_question.py).

The shared vectors first (tests/vectors/agent-question-vectors.json): the event the intake accepts or refuses, the
request the automated click sends, the pages the replay and the history read. Then the app in-process as the reader of
the question record the main flow keeps: the intake waking the feed, the replay and the history pages and the stream
over the host, the transcripts browsed, a decision forwarded to a fake reply endpoint under the app's bearer and the
row re-read as the recorder left it, the two auth modes with the UI's configuration, the PostgreSQL reader's statements
on a recording fake of the pool, the boot's refusals by name, and - gated on AGENT_QUESTION_TEST_URL - the schemas and
the rows on a live PostgreSQL.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import jwt
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from mercury_composable import (
    FunctionRegistry,
    Platform,
    PostOffice,
    app_config,
    default_registry,
    iso_utc,
)
from mercury_composable.server import EventApiServer

_TESTS = Path(__file__).resolve().parent
_MODULE = _TESTS.parent / "extensions" / "agent-question" / "agent_question.py"
_REST_YAML = _MODULE.parent / "resources" / "rest.yaml"
_RECORD_SCHEMA = _TESTS.parent / "extensions" / "ask-person" / "schema" / "postgres.sql"
_TRANSCRIPT_SCHEMA = _TESTS.parent / "extensions" / "llm-helper" / "schema" / "postgres.sql"

_spec = importlib.util.spec_from_file_location("agent_question_under_test", _MODULE)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes are
# unknowable statically - Any tells every analyzer to trust the runtime
aq: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("agent_question_under_test", aq)
_spec.loader.exec_module(aq)

VECTORS: dict[str, Any] = json.loads((_TESTS / "vectors" / "agent-question-vectors.json").read_text("utf-8"))

ISSUER = "https://issuer.example.test"
AUDIENCE = "agent-question"
CALLS = [
    {"id": "toolu_01A", "name": "issue_refund", "route": "demo.refund.issue", "wait": True,
     "input": {"order_id": "ORD-1002", "amount": 174.0, "reason": "Shipment lost in transit"}},
]
CONVERSATION_A = "a" * 32
CONVERSATION_B = "b" * 32
CONVERSATION_C = "c" * 32


def wait_event(ticket: str = "t-answer", base: str = "https://ask.example.test", **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "approval", "graph_id": "order-assistant",
        "description": "Customer-support assistant: a refund waits for a supervisor",
        "correlation_id": "order-1002-case", "turn": 2, "node": "assist",
        "summary": "A refund waits for the supervisor's decision", "calls": CALLS, "ttl": "2d",
        "requested_at": iso_utc(), "expires_at": iso_utc(datetime.now(UTC) + timedelta(days=2)),
        "ticket": ticket, "link": f"{base}/ask/{ticket}",
    }  # fmt: skip
    event.update(overrides)
    return event


def advice_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "advice", "graph_id": "order-assistant", "description": "Customer-support assistant",
        "correlation_id": "order-1002-case", "turn": 2, "node": "decide",
        "summary": "The supervisor's decision was received; the assistant continues",
        "details": "The assistant runs the decided call and answers the customer",
    }  # fmt: skip
    event.update(overrides)
    return event


def make_row(event: dict[str, Any], row_id: int, seq: int, **overrides: Any) -> Any:
    """A row as the main flow's recorder writes it, for the reader under test."""
    wait = event["kind"] in aq.WAIT_KINDS
    now = iso_utc()
    fields: dict[str, Any] = {
        "id": row_id, "seq": seq, "kind": event["kind"], "graph_id": event["graph_id"],
        "correlation_id": event["correlation_id"], "turn": event.get("turn"),
        "ticket": event.get("ticket") if wait else None, "link": event.get("link") if wait else None,
        "description": event["description"], "summary": event["summary"], "details": event.get("details"),
        "message": dict(event), "state": "open" if wait else None, "requested_at": event.get("requested_at") or now,
        "expires_at": event.get("expires_at") if wait else None, "updated_at": now,
    }  # fmt: skip
    fields.update(overrides)
    return aq.Row(**fields)


def put(event: dict[str, Any], row_id: int, seq: int, **overrides: Any) -> Any:
    return aq.reader().put(make_row(event, row_id, seq, **overrides))


def conversation(key: str, started_at: str, correlation_id: str = "order-1002-case", graph_id: str = "order-assistant",
                 status: str = "done") -> dict[str, Any]:
    return {
        "conversation": key, "correlation_id": correlation_id, "graph_id": graph_id, "node": "assist",
        "model": "claude-haiku-4-5", "status": status, "turns": 2, "started_at": started_at, "updated_at": started_at,
        "ended_at": started_at if status == "done" else None, "expires_at": "2027-11-19T09:00:00.000Z",
        "input_tokens": 120, "output_tokens": 40, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "system": "You are a careful assistant.", "tools": [{"name": "issue_refund"}],
    }  # fmt: skip


def turn(number: int, status: str = "done") -> dict[str, Any]:
    return {
        "turn": number, "trace_id": f"trace-{number}", "trace_path": "POST /api/graph/order-assistant",
        "correlation_id": "order-1002-case", "started_at": "2026-11-19T09:00:00.000Z", "ended_at": "2026-11-19T09:00:01.000Z",
        "stop_reason": "end_turn" if status == "done" else "tool_use", "status": status, "request_id": f"req-{number}",
        "input_tokens": 60, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "request": [{"role": "user", "content": "refund?"}], "response": [{"type": "text", "text": "Done."}],
        "calls": None if status == "done" else [{"id": "toolu_01A", "name": "issue_refund", "input": {}}],
    }  # fmt: skip


def _names(section: str) -> list[str]:
    return [case["name"] for case in VECTORS[section]]


def _case(section: str, name: str) -> dict[str, Any]:
    return next(case for case in VECTORS[section] if case["name"] == name)


# --- the shared vectors ---------------------------------------------------------------------------


def test_the_vector_file_is_the_shared_contract() -> None:
    assert VECTORS["contract"] == "agent-question" and VECTORS["version"] == 3
    for section in ("intake_cases", "click_cases", "feed_cases"):
        assert VECTORS[section], section


@pytest.mark.parametrize("name", _names("intake_cases"))
def test_intake_cases(name: str) -> None:
    case = _case("intake_cases", name)
    if "accepted" in case:
        event = aq.validate_event(case["event"])
        assert {"accepted": True, "kind": event["kind"], "graph_id": event["graph_id"], "turn": event.get("turn")} == case["accepted"]
    else:
        with pytest.raises(aq.AppException) as refused:
            aq.validate_event(case["event"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


@pytest.mark.parametrize("name", _names("click_cases"))
def test_click_cases(name: str) -> None:
    case = _case("click_cases", name)
    if "url" in case:
        assert aq.reply_request(case["link"], case["ticket"], case["decisions"], case["who"]) == (case["url"], case["body"])
    else:
        with pytest.raises(aq.ReplyError) as refused:
            aq.reply_request(case["link"], case["ticket"], case["decisions"], case["who"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


def _feed_rows(specs: list[dict[str, Any]]) -> list[Any]:
    return [
        aq.Row(id=s["id"], seq=s["seq"], kind=s["kind"], graph_id="g", correlation_id=f"c-{s['id']}",
               turn=2 if s["kind"] != "advice" else None, ticket=f"t-{s['id']}" if s["kind"] != "advice" else None,
               link=None, description="d", summary="s", details=None, message={}, state=s["state"],
               requested_at="2026-11-20T00:00:00.000Z", expires_at=None, updated_at="2026-11-20T00:00:00.000Z")
        for s in specs
    ]


async def test_feed_cases_page_the_rows_in_both_directions() -> None:
    for case in VECTORS["feed_cases"]:
        rows = _feed_rows(case["rows"])
        reader = aq.MemoryReader()
        for row in rows:
            reader.put(row)
        for query in case["queries"]:
            if "before" in query:
                page = aq.page_before(rows, query["before"], query["limit"])
                read = await reader.before(query["before"], query["limit"])
            else:
                page = aq.page_since(rows, query["since"], query["limit"])
                read = await reader.since(query["since"], query["limit"])
            shown = {"ids": [r["id"] for r in page["rows"]], "seqs": [r["seq"] for r in page["rows"]],
                     "next": page["next"], "more": page["more"]}
            assert shown == {k: query[k] for k in ("ids", "seqs", "next", "more")}, (case["name"], query)
            assert [r.id for r in read] == query["ids"], (case["name"], query)
        assert await reader.latest() == max(s["seq"] for s in case["rows"])


# --- configuration and the fresh reader ------------------------------------------------------------


@pytest.fixture
def configure() -> Iterator[Callable[[dict[str, Any]], Any]]:
    """Set keys on the shared configuration, reload the settings and give the module a fresh memory reader, fresh
    wake-ups and a fresh feed; clear everything afterwards."""
    touched: list[str] = []
    aq._reader = aq.MemoryReader()
    aq._wakeups = aq.Wakeups()
    aq._feed = aq.Feed()

    def apply(values: dict[str, Any]) -> Any:
        for key, value in values.items():
            app_config().set(key, value)
            touched.append(key)
        return aq.configure()

    yield apply
    for key in touched:
        app_config().set(key, None)
    aq._settings = None
    aq._reader = None
    aq._wakeups = aq.Wakeups()
    aq._feed = aq.Feed()


BASE: dict[str, Any] = {aq.STORE: "memory", aq.AUTH: "none", aq.REPLY_TOKEN: "", "event.stream.keep.alive": "1s"}


@pytest.mark.parametrize(
    ("values", "names"),
    [
        ({aq.STORE: "redis"}, "question.store must be memory or postgres"),
        ({aq.AUTH: "basic"}, "question.auth must be one of none, oauth2"),
        ({aq.AUTH: "proxy"}, "question.auth must be one of none, oauth2"),
        ({aq.AUTH: "oauth2"}, "set question.auth.oauth2.issuer or question.auth.oauth2.jwks-url"),
        ({aq.POSTGRES_PREFIX + "port": "db"}, "question.store.postgres.port must be a port number"),
        ({aq.POLL: "0"}, "question.poll must be a duration"),
    ],
)
def test_the_boot_fails_by_name(configure: Callable[[dict[str, Any]], Any], values: dict[str, Any], names: str) -> None:
    with pytest.raises(aq.QuestionError) as refused:
        configure({**BASE, **values})
    assert names in str(refused.value)


def test_the_postgres_settings_follow_the_audit_store(configure: Callable[[dict[str, Any]], Any]) -> None:
    settings = configure({
        **BASE, aq.STORE: "postgres", aq.POSTGRES_PREFIX + "host": "db.internal", aq.POSTGRES_PREFIX + "port": "6543",
        aq.POSTGRES_PREFIX + "database": "agents", aq.POSTGRES_PREFIX + "username": "reader",
        aq.POSTGRES_PREFIX + "password": "s3", aq.POSTGRES_PREFIX + "ssl": "false", aq.POSTGRES_PREFIX + "timeout.ms": 2500,
        aq.POLL: "250ms",
    })  # fmt: skip
    assert settings.postgres == {
        "host": "db.internal", "port": 6543, "dbname": "agents", "user": "reader", "password": "s3",
        "sslmode": "disable", "connect_timeout": 2, "options": "-c statement_timeout=2500",
    }  # fmt: skip
    assert settings.postgres_timeout_s == 2.5 and settings.poll_ms == 250 and settings.reply_timeout_ms == 10_000
    assert settings.oauth2_scopes == "openid profile email"


# --- the intake and the feed ----------------------------------------------------------------------


async def test_the_intake_accepts_the_event_and_wakes_the_feed(configure: Callable[[dict[str, Any]], Any]) -> None:
    configure(BASE)
    queue = aq._wakeups.subscribe()
    office = PostOffice(registry=default_registry)
    aq._feed.start(60_000)  # a poll an hour away: only the wake-up can make the feed look
    try:
        put(wait_event(), 1, 1)  # the recorder wrote the row before the channel was told
        reply = await office.request(aq.INTAKE_ROUTE, wait_event(), timeout_ms=5000)
        assert reply.get_status() == 200, reply.body
        assert reply.body == {"accepted": True, "kind": "approval", "graph_id": "order-assistant", "turn": 2}
        woken = await asyncio.wait_for(queue.get(), 2)
        assert woken.id == 1 and aq._feed.cursor == 1, "the intake's wake-up made the feed look at once"
        bad = await office.request(aq.INTAKE_ROUTE, {"kind": "notice"}, timeout_ms=5000)
        assert bad.get_status() == 400 and "kind must be one of" in str(bad.body)
        advice = await office.request(aq.INTAKE_ROUTE, advice_event(), timeout_ms=5000)
        assert advice.body == {"accepted": True, "kind": "advice", "graph_id": "order-assistant", "turn": 2}
        assert queue.empty(), "nothing new in the record: the feed has nothing to tell"
        put(advice_event(), 2, 2)
        assert [r.id for r in await aq._feed.poll_once()] == [2]
        assert (await asyncio.wait_for(queue.get(), 2)).kind == "advice"
    finally:
        await aq._feed.stop()
        aq._wakeups.unsubscribe(queue)
        await default_registry.bus.close()


# --- the person's side over the host -----------------------------------------------------------------


class FakeReply:
    """The main flow's reply endpoint as the app sees it: scripted by ticket, recording every click, and letting a
    test play the recorder behind it (on_post) before the outcome goes back."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.on_post: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.jwks: dict[str, Any] = {"keys": []}

    async def reply(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.posts.append({"body": body, "authorization": request.headers.get("authorization")})
        if self.on_post is not None:
            await self.on_post(body)
        ticket = body.get("ticket")
        if ticket == "t-expired":
            return web.json_response({"type": "nothing-waiting", "run": "fresh"}, status=410)
        if ticket == "t-refused":
            return web.json_response({"type": "approval", "turn": 2, "error": "not a call waiting for approval",
                                      "calls": CALLS})
        return web.json_response({"type": "answer", "turn": 3, "run": "resume", "message": "Your refund has been issued."})

    async def keys(self, _request: web.Request) -> web.Response:
        return web.json_response(self.jwks)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/ask/reply", self.reply)
        app.router.add_get("/jwks", self.keys)
        return app


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


@pytest_asyncio.fixture
async def reply_endpoint() -> AsyncIterator[tuple[FakeReply, str]]:
    fake = FakeReply()
    runner, url = await _serve(fake.app())
    try:
        yield fake, url
    finally:
        await runner.cleanup()


@contextmanager
def rest_enabled() -> Iterator[None]:
    """The app's rest.yaml on the shared configuration, as its application.yml turns it on."""
    config = app_config()
    config.set("rest.automation", True)
    config.set("yaml.rest.automation", f"file:{_REST_YAML}")
    try:
        yield
    finally:
        config.set("rest.automation", None)
        config.set("yaml.rest.automation", None)


def rest_app() -> web.Application:
    """The pack's host serving the app's rest.yaml entries in front of this module's functions, as mercury-serve
    does with rest.automation: true."""
    with rest_enabled():
        return EventApiServer(default_registry).create_app()


@pytest_asyncio.fixture
async def host() -> AsyncIterator[str]:
    runner, url = await _serve(rest_app())
    try:
        yield url
    finally:
        await runner.cleanup()
        await aq.shutdown()
        # the functions run on the default registry's bus, whose workers bind to this test's loop
        await default_registry.bus.close()


async def get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    async with aiohttp.ClientSession() as http, http.get(url, headers=headers) as response:
        return response.status, await response.json(content_type=None)


def seven_rows() -> None:
    specs = [(1, 3, "approval", "answered"), (2, 2, "advice", None), (3, 5, "question", "open"), (4, 4, "approval", "expired"),
             (5, 7, "approval", "open"), (6, 6, "advice", None), (7, 1, "approval", "answered")]
    for row_id, seq, kind, state in specs:
        event = advice_event(correlation_id=f"case-{row_id}") if kind == "advice" else wait_event(
            ticket=f"t-{row_id}", correlation_id=f"case-{row_id}", kind=kind)
        put(event, row_id, seq, state=state)


async def test_the_replay_and_the_history_page_the_record(configure: Callable[[dict[str, Any]], Any], host: str) -> None:
    configure(BASE)
    seven_rows()
    status, page = await get_json(f"{host}/questions?since=0")
    assert status == 200
    assert [r["id"] for r in page["rows"]] == [7, 2, 1, 4, 3, 6, 5] and page["next"] == 7 and page["more"] is False
    assert page["rows"][2]["calls"] == CALLS and page["rows"][2]["ticket"] == "t-1" and page["rows"][1]["state"] is None
    status, tail = await get_json(f"{host}/questions?since=5&limit=1")
    assert [r["id"] for r in tail["rows"]] == [6] and tail["next"] == 6 and tail["more"] is True
    status, newest = await get_json(f"{host}/questions?before=&limit=3")
    assert status == 200
    assert [r["id"] for r in newest["rows"]] == [5, 6, 3] and newest["next"] == 5 and newest["more"] is True
    assert newest["before"] is None
    status, older = await get_json(f"{host}/questions?before=5&limit=3")
    assert [r["id"] for r in older["rows"]] == [4, 1, 2] and older["next"] == 2 and older["more"] is True
    status, oldest = await get_json(f"{host}/questions?before=2&limit=3")
    assert [r["id"] for r in oldest["rows"]] == [7] and oldest["next"] == 1 and oldest["more"] is False
    assert (await get_json(f"{host}/questions?since=x"))[0] == 400
    assert (await get_json(f"{host}/questions?before=x"))[0] == 400


async def test_the_stream_replays_then_follows_the_feed_with_keep_alives(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure(BASE)
    put(wait_event(), 1, 1)
    async with aiohttp.ClientSession() as http, http.get(f"{host}/questions/events",
                                                          headers={"accept": "text/event-stream"}) as response:
        assert response.status == 200 and response.content_type == "text/event-stream"
        reader = response.content

        async def frame() -> list[str]:
            lines: list[str] = []
            while True:
                line = (await asyncio.wait_for(reader.readline(), 5)).decode().rstrip("\n")
                if not line:
                    if lines:
                        return lines
                    continue
                lines.append(line)

        assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
        replayed = await frame()
        assert replayed[0] == "event: question", "the engines' SSE dialect: an event name and the data, no id line"
        first_row = json.loads(replayed[1][len("data: "):])
        assert first_row["ticket"] == "t-answer" and first_row["seq"] == 1, "the row's seq is the cursor"
        ping = await frame()
        assert ping == [": ping"], "a keep-alive while nothing changes"
        put(advice_event(), 2, 2)
        await aq._feed.poll_once()
        live = await frame()
        assert live[0] == "event: question"
        advice = json.loads(live[1][len("data: "):])
        assert advice["kind"] == "advice" and advice["seq"] == 2
        assert "state" in advice and advice["state"] is None, "a null stays on the wire - the UI reads it"
    async with aiohttp.ClientSession() as http, http.get(f"{host}/questions/events",
                                                          headers={"last-event-id": "1"}) as response:
        first = (await asyncio.wait_for(response.content.readline(), 5)).decode()
        second = (await asyncio.wait_for(response.content.readline(), 5)).decode()
        assert first == "event: question\n" and json.loads(second[len("data: "):])["seq"] == 2, \
            "the reconnect resumes after the cursor the browser kept"
    async with aiohttp.ClientSession() as http, http.get(f"{host}/questions/events",
                                                          headers={"last-event-id": "x"}) as response:
        assert response.status == 400 and "whole number" in (await response.json())["message"]


async def test_the_ui_configuration_names_the_sign_in(configure: Callable[[dict[str, Any]], Any], host: str) -> None:
    configure(BASE)
    assert await get_json(f"{host}/questions/config") == (200, {"auth": "none", "title": "Agent questions"})
    configure({aq.AUTH: "oauth2", aq.OAUTH2_ISSUER: ISSUER, aq.OAUTH2_AUDIENCE: AUDIENCE, aq.OAUTH2_CLIENT_ID: "agent-question-ui",
               aq.PAGE_TITLE: "Decisions"})
    assert await get_json(f"{host}/questions/config") == (200, {
        "auth": "oauth2", "title": "Decisions",
        "oauth2": {"issuer": ISSUER, "client_id": "agent-question-ui", "scopes": "openid profile email", "audience": AUDIENCE},
    })


async def test_a_decision_is_forwarded_and_the_row_is_re_read_as_the_recorder_left_it(
    configure: Callable[[dict[str, Any]], Any], reply_endpoint: tuple[FakeReply, str], host: str
) -> None:
    fake, base = reply_endpoint
    configure({**BASE, aq.REPLY_TOKEN: "channel-app-bearer"})
    row = put(wait_event(ticket="t-answer", base=base), 1, 1)
    queue = aq._wakeups.subscribe()
    decisions = [{"id": "toolu_01A", "approved": True, "note": "Carrier confirmed the loss"}]

    async def recorder(body: dict[str, Any]) -> None:
        # the recorder behind the reply endpoint writes the answer before the outcome goes back
        put(wait_event(ticket=body["ticket"], base=base), 1, 2, state="answered", answered_by=body["decided_by"],
            answered_at=iso_utc(), decisions=body["decisions"], outcome={"status": 200, "body": {"type": "answer"}})

    fake.on_post = recorder
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{host}/questions/{row.id}/decide",
                             json={"decisions": decisions, "decided_by": "operator-17"}) as response:
            result = await response.json()
            assert response.status == 200, result
        assert result["row"]["state"] == "answered" and result["row"]["answered_by"] == "operator-17"
        assert result["outcome"]["status"] == 200 and result["outcome"]["body"]["type"] == "answer"
        assert fake.posts[0]["body"] == {"ticket": "t-answer", "decisions": decisions, "decided_by": "operator-17"}
        assert fake.posts[0]["authorization"] == "Bearer channel-app-bearer"
        # the feed was told to look: the change reaches every device
        assert [r.id for r in await aq._feed.poll_once()] == [1]
        woken = queue.get_nowait()
        assert woken.id == row.id and woken.state == "answered"
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": decisions}) as response:
            assert response.status == 409 and "operator-17" in (await response.json())["message"]
        # a recorder that did not write (or a wait decided elsewhere): the row is reported as it stands
        fake.on_post = None
        expired = put(wait_event(ticket="t-expired", base=base, correlation_id="case-2"), 2, 3)
        async with http.post(f"{host}/questions/{expired.id}/decide", json={"decisions": decisions}) as response:
            body = await response.json()
        assert body["row"]["state"] == "open" and body["outcome"]["status"] == 410
        refused = put(wait_event(ticket="t-refused", base=base, correlation_id="case-3"), 3, 4)
        async with http.post(f"{host}/questions/{refused.id}/decide", json={"decisions": decisions}) as response:
            body = await response.json()
        assert body["row"]["state"] == "open" and body["outcome"]["body"]["error"].startswith("not a call")
        silent = put(wait_event(ticket="t-silent", base="http://127.0.0.1:9", correlation_id="case-4"), 4, 5)
        configure({aq.REPLY_TIMEOUT: "2s"})
        async with http.post(f"{host}/questions/{silent.id}/decide", json={"decisions": decisions}) as response:
            body = await response.json()
        assert body["row"]["state"] == "open" and body["outcome"]["status"] == 502
        # the refusals of the endpoint itself
        async with http.post(f"{host}/questions/{row.id}/decide", data=b"nope") as response:
            assert response.status == 400
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": []}) as response:
            assert response.status == 400 and "non-empty list" in (await response.json())["message"]
        async with http.post(f"{host}/questions/999/decide", json={"decisions": decisions}) as response:
            assert response.status == 404
        advice = put(advice_event(), 5, 6)
        async with http.post(f"{host}/questions/{advice.id}/decide", json={"decisions": decisions}) as response:
            assert response.status == 409 and "advice" in (await response.json())["message"]
        async with http.post(f"{host}/questions/{advice.id}/seen") as response:
            assert response.status == 404, "the seen endpoint is gone with the state"
    aq._wakeups.unsubscribe(queue)


async def test_the_transcripts_are_browsed_from_the_audit_store(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure(BASE)
    reader = aq.reader()
    reader.put_conversation(conversation(CONVERSATION_A, "2026-11-19T09:00:00.000Z"), [turn(1, "tool_use"), turn(2)])
    reader.put_conversation(conversation(CONVERSATION_B, "2026-11-19T10:00:00.000Z", correlation_id="release-42",
                                         graph_id="plan-assistant", status="open"), [turn(1, "tool_use")])
    reader.put_conversation(conversation(CONVERSATION_C, "2026-11-19T11:00:00.000Z"), [turn(1)])
    status, page = await get_json(f"{host}/transcripts")
    assert status == 200
    assert [c["conversation"] for c in page["conversations"]] == [CONVERSATION_C, CONVERSATION_B, CONVERSATION_A]
    assert page["next"] == "2026-11-19T09:00:00.000Z" and page["more"] is False
    assert "system" not in page["conversations"][0] and "tools" not in page["conversations"][0], "the list is light"
    status, older = await get_json(f"{host}/transcripts?before=2026-11-19T11:00:00Z&limit=1")
    assert [c["conversation"] for c in older["conversations"]] == [CONVERSATION_B]
    assert older["next"] == "2026-11-19T10:00:00.000Z" and older["more"] is True
    status, by_case = await get_json(f"{host}/transcripts?correlation_id=order-1002-case")
    assert [c["conversation"] for c in by_case["conversations"]] == [CONVERSATION_C, CONVERSATION_A]
    status, by_graph = await get_json(f"{host}/transcripts?graph_id=plan-assistant")
    assert [c["conversation"] for c in by_graph["conversations"]] == [CONVERSATION_B]
    status, one = await get_json(f"{host}/transcripts/{CONVERSATION_A}")
    assert status == 200 and one["conversation"]["system"] == "You are a careful assistant."
    assert [t["turn"] for t in one["turns"]] == [1, 2] and one["turns"][0]["calls"][0]["name"] == "issue_refund"
    assert (await get_json(f"{host}/transcripts/{'f' * 32}"))[0] == 404
    assert (await get_json(f"{host}/transcripts/not-a-conversation"))[0] == 400
    assert (await get_json(f"{host}/transcripts?before=yesterday"))[0] == 400
    assert (await get_json(f"{host}/transcripts?limit=x"))[0] == 400


async def test_the_oauth2_mode_guards_every_read_and_the_click(
    configure: Callable[[dict[str, Any]], Any], reply_endpoint: tuple[FakeReply, str], host: str
) -> None:
    fake, base = reply_endpoint
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    fake.jwks = {"keys": [{**public_jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    configure({**BASE, aq.AUTH: "oauth2", aq.OAUTH2_ISSUER: ISSUER, aq.OAUTH2_AUDIENCE: AUDIENCE,
               aq.OAUTH2_JWKS_URL: f"{base}/jwks", aq.OAUTH2_CLIENT_ID: "agent-question-ui"})
    aq._verifier = None
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "u-17", "preferred_username": "operator-17",
                        "iat": now, "exp": now + 300}, pem, algorithm="RS256", headers={"kid": "k1"})
    row = put(wait_event(base=base), 1, 1)
    decisions = [{"id": "toolu_01A", "approved": False}]
    async with aiohttp.ClientSession() as http:
        for path in ("/questions?since=0", "/questions/events", "/transcripts", f"/transcripts/{CONVERSATION_A}"):
            async with http.get(f"{host}{path}") as response:
                assert response.status == 401, path
        async with http.get(f"{host}/questions/config") as response:
            assert response.status == 200 and (await response.json())["oauth2"]["client_id"] == "agent-question-ui"
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": decisions}) as response:
            assert response.status == 401
        bearer = {"authorization": f"Bearer {token}"}
        async with http.get(f"{host}/questions?since=0", headers=bearer) as response:
            assert response.status == 200 and [r["id"] for r in (await response.json())["rows"]] == [1]
        async with http.get(f"{host}/transcripts", headers=bearer) as response:
            assert response.status == 200
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": decisions, "decided_by": "spoof"},
                             headers=bearer) as response:
            assert response.status == 200
        assert fake.posts[0]["body"]["decided_by"] == "operator-17", "the token's name, never the body's"


# --- the PostgreSQL reader on a recording fake of the pool ---------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _FakeCursor:
        self._pool.log.append((sql, params))
        rows = self._pool.queue.pop(0) if self._pool.queue else self._pool.rows
        return _FakeCursor(rows)


class _FakePool:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.log: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows = rows or []
        self.queue: list[list[tuple[Any, ...]]] = []
        self.opened: tuple[bool, float] | None = None
        self.closed = False

    async def open(self, wait: bool, timeout: float) -> None:
        self.opened = (wait, timeout)

    async def close(self) -> None:
        self.closed = True

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_FakeConnection]:
        yield _FakeConnection(self)


def _record(**overrides: Any) -> tuple[Any, ...]:
    now = datetime(2026, 11, 19, 9, 12, 5, 123000, tzinfo=UTC)
    values: dict[str, Any] = {
        "id": 7, "seq": 12, "ticket": "t-answer", "graph_id": "order-assistant", "correlation_id": "order-1002-case",
        "turn": 2, "kind": "approval", "description": "Customer-support assistant", "summary": "A refund waits",
        "details": None, "message": {"kind": "approval", "calls": CALLS}, "link": "https://ask.example.test/ask/t-answer",
        "state": "open", "requested_at": now, "expires_at": now + timedelta(days=2), "updated_at": now,
        "answered_at": None, "answered_by": None, "decisions": None, "outcome": None,
    }  # fmt: skip
    values.update(overrides)
    return tuple(values[name.strip()] for name in aq.COLUMNS.split(","))


def _conversation_record(light: bool = True) -> tuple[Any, ...]:
    started = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
    values = (CONVERSATION_A, "order-1002-case", "order-assistant", "assist", "claude-haiku-4-5", "done", 2, started,
              started, started, started + timedelta(days=365), 120, 40, 0, 0)
    return values if light else values + ("You are a careful assistant.", [{"name": "issue_refund"}])


def _turn_record() -> tuple[Any, ...]:
    started = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
    return (1, "trace-1", "POST /api/graph/order-assistant", "order-1002-case", started, started + timedelta(seconds=1),
            "tool_use", "tool_use", "req-1", 60, 20, 0, 0, [{"role": "user", "content": "refund?"}],
            [{"type": "tool_use", "id": "toolu_01A", "name": "issue_refund", "input": {}}],
            '[{"id":"toolu_01A","name":"issue_refund","input":{}}]')


async def test_the_postgres_reader_runs_the_statements_and_maps_the_records() -> None:
    pool = _FakePool([_record()])
    reader = aq.PostgresReader(pool, 2.5)
    await reader.open()
    assert pool.opened == (True, 2.5)
    row = await reader.get(7)
    assert pool.log[-1] == (aq.SQL_GET, (7,)) and row is not None
    assert row.id == 7 and row.seq == 12 and row.requested_at == "2026-11-19T09:12:05.123Z"
    assert row.expires_at == "2026-11-21T09:12:05.123Z" and row.public()["calls"] == CALLS
    await reader.since(3, 50)
    assert pool.log[-1] == (aq.SQL_SINCE, (3, 50))
    await reader.before(None, 20)
    assert pool.log[-1] == (aq.SQL_BEFORE, (None, None, 20))
    await reader.before(9, 20)
    assert pool.log[-1] == (aq.SQL_BEFORE, (9, 9, 20))
    pool.queue = [[(42,)]]
    assert await reader.latest() == 42 and pool.log[-1] == (aq.SQL_LATEST, ())
    pool.queue = [[]]
    assert await reader.latest() == 0
    pool.queue = [[("open", 3), ("advice", 1)]]
    assert await reader.counts() == {"open": 3, "advice": 1}
    pool.queue = [[_conversation_record()]]
    shown = await reader.conversations("2026-11-19T12:00:00.000Z", 20, "order-1002-case", None)
    assert pool.log[-1] == (aq.SQL_CONVERSATIONS, ("2026-11-19T12:00:00.000Z", "2026-11-19T12:00:00.000Z", "order-1002-case",
                                                  "order-1002-case", None, None, 20))
    assert shown[0]["conversation"] == CONVERSATION_A and shown[0]["started_at"] == "2026-11-19T09:00:00.000Z"
    assert shown[0]["turns"] == 2 and shown[0]["input_tokens"] == 120 and "system" not in shown[0]
    pool.queue = [[_conversation_record(light=False)], [_turn_record()]]
    one = await reader.conversation(CONVERSATION_A)
    assert [entry[0] for entry in pool.log[-2:]] == [aq.SQL_CONVERSATION, aq.SQL_TURNS] and one is not None
    assert one["conversation"]["system"] == "You are a careful assistant." and one["conversation"]["tools"] == [{"name": "issue_refund"}]
    assert one["turns"][0]["turn"] == 1 and one["turns"][0]["status"] == "tool_use"
    assert one["turns"][0]["calls"] == [{"id": "toolu_01A", "name": "issue_refund", "input": {}}], "json text is parsed"
    assert one["turns"][0]["ended_at"] == "2026-11-19T09:00:01.000Z"
    pool.queue = [[]]
    assert await reader.conversation(CONVERSATION_B) is None
    await reader.close()
    assert pool.closed


def test_a_record_s_json_may_arrive_as_text_and_an_advice_has_no_state() -> None:
    record = _record(message='{"kind":"advice"}', decisions="[]", outcome='{"status":410}', kind="advice", ticket=None,
                     link=None, turn=None, expires_at=None, state=None)
    row = aq.row_from_record(record)
    assert row.message == {"kind": "advice"} and row.decisions == [] and row.outcome == {"status": 410}
    assert row.turn is None and row.expires_at is None and row.state is None
    assert aq.failure(RuntimeError("quotes a row")) == "RuntimeError"


async def test_a_stopping_application_ends_its_streams(configure: Callable[[dict[str, Any]], Any], host: str) -> None:
    """A device's stream otherwise keeps the process alive past SIGTERM (found by the UI drive of 2026-11-20): the
    shutdown hook ends every stream, a stream opened while stopping ends at once, and the device reconnects."""
    configure(BASE)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/questions/events") as response:
            assert response.status == 200
            await asyncio.sleep(0.2)
            assert aq._wakeups.connected == 1
            await aq.close_streams()
            body = await asyncio.wait_for(response.read(), 5)  # the stream ended
            assert b"event: question" not in body
        async with http.get(f"{host}/questions/events") as response:
            assert response.status == 200
            await asyncio.wait_for(response.read(), 5)  # a stream opened while stopping ends at once
    await asyncio.sleep(0.1)
    assert aq._wakeups.connected == 0


# --- the React UI's bundle, served by the host ------------------------------------------------------------

PUBLIC = Path(__file__).resolve().parent.parent / "extensions" / "agent-question" / "resources" / "public"


async def test_the_host_serves_the_committed_ui_bundle_at_the_root(configure: Callable[[dict[str, Any]], Any]) -> None:
    """The React UI is built in the Node.js pack and its bundle committed here, in resources/public next to the
    application file; the host's static content service serves it at the root - the app generates no page."""
    configure({**BASE, "static.html.folder": f"file:{PUBLIC}"})
    runner, host = await _serve(rest_app())
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{host}/") as response:
                index = await response.text()
                assert response.status == 200 and response.content_type == "text/html"
                assert '<div id="root">' in index and 'href="/favicon.svg"' in index
            scripts = re.findall(r'src="(/assets/[^"]+\.js)"', index)
            styles = re.findall(r'href="(/assets/[^"]+\.css)"', index)
            assert len(scripts) == 1 and len(styles) == 1, "one script and one stylesheet, hashed"
            async with http.get(f"{host}{scripts[0]}") as response:
                assert response.status == 200 and response.content_type == "text/javascript"
            async with http.get(f"{host}{styles[0]}") as response:
                assert response.status == 200 and response.content_type == "text/css"
            async with http.get(f"{host}/favicon.svg") as response:
                assert response.status == 200 and response.content_type == "image/svg+xml"
            async with http.get(f"{host}/questions/app") as response:
                assert response.status == 404, "the generated page is retired"
            # the engines' actuator endpoints stay at their paths beside the bundle - Kubernetes probes and operations
            for path in ("/info", "/info/routes", "/health", "/env"):
                async with http.get(f"{host}{path}") as response:
                    assert response.status == 200 and response.content_type == "application/json", path
                    await response.json()
            async with http.get(f"{host}/livenessprobe") as response:
                assert response.status == 200 and await response.text() == "OK"
    finally:
        await runner.cleanup()
        await aq.shutdown()


# --- the hooks and the boot ---------------------------------------------------------------------------


async def test_the_platform_hooks_mount_the_routes_and_run_the_boot(
    configure: Callable[[dict[str, Any]], Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(BASE)
    fresh = Platform(FunctionRegistry())
    monkeypatch.setattr(aq, "platform", fresh)
    aq._attach()
    app = fresh.create_app()
    assert len(app.on_shutdown) == 1, "the streams end on shutdown, before the host waits for the requests in flight"
    paths = [route.resource.canonical for route in app.router.routes() if route.resource is not None]
    assert not any(path.startswith(("/questions", "/transcripts")) for path in paths), \
        "no route is mounted - the person's side is rest.yaml's (RFC-0018)"
    with rest_enabled():
        rest = EventApiServer(default_registry).rest
    assert rest is not None
    expected = {
        ("GET", "/questions/config"): "question.config", ("GET", "/questions"): "question.list",
        ("GET", "/questions/events"): "question.events", ("POST", "/questions/7/decide"): "question.decide",
        ("GET", "/transcripts"): "transcript.list", ("GET", f"/transcripts/{CONVERSATION_A}"): "transcript.show",
    }  # fmt: skip
    for (method, path), service in expected.items():
        assigned = rest.table.find(method, path)
        assert assigned is not None and assigned.info.service == service, path
        assert (assigned.info.authentication == "question.identity") is (service != "question.config"), path
        registered = default_registry.get(service)
        assert registered is not None and registered.private, service
    events = rest.table.find("GET", "/questions/events")
    assert events is not None and events.info.stream and events.info.headers is not None
    decide = rest.table.find("POST", "/questions/7/decide")
    assert decide is not None and decide.path_params == {"id": "7"}
    assert rest.table.find("POST", "/questions/7/seen") is None, "the seen endpoint is gone with the state"
    runner = web.AppRunner(app)
    with caplog.at_level("WARNING"):
        await runner.setup()
    try:
        assert "The store is in memory" in caplog.text
        assert aq.reader().name == "memory" and aq._feed.running and aq._feed.cursor == 0
        info = await aq.question_health({"type": "info"}, None)
        assert info["service"] == "agent.question.inbox" and info["store"] == "memory" and info["auth"] == "none"
        assert info["feed"] == 0 and info["rows"] == {}
        assert await aq.question_health({}, None) == "agent.question.inbox is running fine"
    finally:
        await runner.cleanup()
    assert not aq._feed.running, "the cleanup stops the feed"


# --- a live PostgreSQL --------------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AGENT_QUESTION_TEST_URL"),
    reason="a live PostgreSQL: set AGENT_QUESTION_TEST_URL, e.g. postgres://mercury:...@127.0.0.1:5432/mercury?sslmode=disable",
)
async def test_the_reader_finds_the_rows_on_a_live_postgres() -> None:
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(os.environ["AGENT_QUESTION_TEST_URL"], min_size=1, max_size=2, open=False)
    reader = aq.PostgresReader(pool, 10.0)
    await reader.open()
    try:
        async with pool.connection() as connection:
            await connection.execute(_RECORD_SCHEMA.read_bytes())
            await connection.execute(_TRANSCRIPT_SCHEMA.read_bytes())
            cid = f"live-{int(datetime.now(UTC).timestamp())}"
            event = wait_event(ticket=f"{cid}-1", correlation_id=cid)
            cursor = await connection.execute(
                "INSERT INTO question (seq, ticket, graph_id, correlation_id, turn, kind, description, summary, message, "
                "link, state, requested_at, expires_at, updated_at) VALUES (nextval('question_seq'), %s, %s, %s, %s, %s, %s, "
                "%s, %s::jsonb, %s, 'open', %s::timestamptz, %s::timestamptz, %s::timestamptz) RETURNING id, seq",
                (event["ticket"], event["graph_id"], cid, 2, "approval", event["description"], event["summary"],
                 json.dumps(event), event["link"], event["requested_at"], event["expires_at"], iso_utc()),
            )
            inserted = (await cursor.fetchall())[0]
            conv = f"{int(datetime.now(UTC).timestamp() * 1000):032x}"[-32:]
            await connection.execute(
                "INSERT INTO transcript (conversation, correlation_id, graph_id, node, model, status, turns, started_at, "
                "updated_at, ended_at, expires_at, system, tools) VALUES (%s, %s, %s, %s, %s, 'done', 1, %s::timestamptz, "
                "%s::timestamptz, %s::timestamptz, %s::timestamptz, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (conv, cid, "order-assistant", "assist", "claude-haiku-4-5", iso_utc(), iso_utc(), iso_utc(), iso_utc(),
                 "sys", "[]"),
            )
            await connection.execute(
                "INSERT INTO transcript_turn (conversation, turn, trace_id, trace_path, correlation_id, started_at, ended_at, "
                "stop_reason, status, request_id, input_tokens, output_tokens, cache_creation_input_tokens, "
                "cache_read_input_tokens, request, response, calls) VALUES (%s, 1, %s, %s, %s, %s::timestamptz, "
                "%s::timestamptz, 'end_turn', 'done', %s, 1, 1, 0, 0, %s::jsonb, %s::jsonb, NULL) ON CONFLICT DO NOTHING",
                (conv, "trace-live", "POST /api/graph/order-assistant", cid, iso_utc(), iso_utc(), "req-live",
                 '[{"role":"user","content":"hi"}]', '[{"type":"text","text":"hello"}]'),
            )
        row_id, seq = int(inserted[0]), int(inserted[1])
        found = await reader.get(row_id)
        assert found is not None and found.ticket == f"{cid}-1" and found.state == "open"
        assert any(r.id == row_id for r in await reader.since(seq - 1, 10))
        assert (await reader.before(None, 5))[0].seq >= seq and await reader.latest() >= seq
        assert (await reader.counts()).get("open", 0) >= 1
        conversations = await reader.conversations(None, 10, cid, None)
        assert [c["conversation"] for c in conversations] == [conv]
        one = await reader.conversation(conv)
        assert one is not None and one["conversation"]["system"] == "sys" and one["turns"][0]["turn"] == 1
    finally:
        await reader.close()
