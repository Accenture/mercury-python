"""The agent-question app (extensions/agent-question/agent_question.py).

The shared vectors first (tests/vectors/agent-question-vectors.json): the row a message event becomes, the
state an outcome leaves, the request the automated click sends, and the reference inbox on a scripted
sequence. Then the app in-process: the intake on the bus (a wait upserted, an advice appended, every
connected device told), the replay and the stream over the host, a decision forwarded to a fake reply
endpoint under the app's bearer with the outcome recorded and broadcast, an advice marked seen, open waits
expiring, the proxy mode, the PostgreSQL inbox's statements on a recording fake of the pool, the boot's
refusals by name, and - gated on AGENT_QUESTION_TEST_URL - the schema and the rows on a live PostgreSQL.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

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
_SCHEMA = _TESTS.parent / "extensions" / "agent-question" / "schema"

_spec = importlib.util.spec_from_file_location("agent_question_under_test", _MODULE)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes are
# unknowable statically - Any tells every analyzer to trust the runtime
aq: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("agent_question_under_test", aq)
_spec.loader.exec_module(aq)

VECTORS: dict[str, Any] = json.loads((_TESTS / "vectors" / "agent-question-vectors.json").read_text("utf-8"))

CALLS = [
    {"id": "toolu_01A", "name": "issue_refund", "route": "demo.refund.issue", "wait": True,
     "input": {"order_id": "ORD-1002", "amount": 174.0, "reason": "Shipment lost in transit"}},
]


def wait_event(ticket: str = "t-answer", base: str = "https://ask.example.test", **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "approval", "graph_id": "order-assistant",
        "description": "Customer-support assistant: a refund waits for a supervisor",
        "correlation_id": "order-1002-case", "turn": 2, "node": "assist",
        "summary": "A refund waits for the supervisor's decision", "calls": CALLS, "ttl": "2d",
        "requested_at": iso_utc(), "expires_at": iso_utc(datetime.now(timezone.utc) + timedelta(days=2)),
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


def _names(section: str) -> list[str]:
    return [case["name"] for case in VECTORS[section]]


def _case(section: str, name: str) -> dict[str, Any]:
    return next(case for case in VECTORS[section] if case["name"] == name)


# --- the shared vectors ---------------------------------------------------------------------------


def test_the_vector_file_is_the_shared_contract() -> None:
    assert VECTORS["contract"] == "agent-question" and VECTORS["version"] == 1
    for section in ("row_cases", "outcome_cases", "click_cases", "transition_cases"):
        assert VECTORS[section], section


@pytest.mark.parametrize("name", _names("row_cases"))
def test_row_cases(name: str) -> None:
    case = _case("row_cases", name)
    if "row" in case:
        event = aq.validate_event(case["event"])
        key = aq.wait_key(event)
        assert (list(key) if key else None) == case["key"]
        assert aq.row_fields(event, case["now"]) == case["row"]
    else:
        with pytest.raises(aq.AppException) as refused:
            aq.validate_event(case["event"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


@pytest.mark.parametrize("name", _names("outcome_cases"))
def test_outcome_cases(name: str) -> None:
    case = _case("outcome_cases", name)
    assert aq.state_after(case["status"], case["body"]) == case["state"]


@pytest.mark.parametrize("name", _names("click_cases"))
def test_click_cases(name: str) -> None:
    case = _case("click_cases", name)
    if "url" in case:
        assert aq.reply_request(case["link"], case["ticket"], case["decisions"], case["who"]) == (case["url"], case["body"])
    else:
        with pytest.raises(aq.ReplyError) as refused:
            aq.reply_request(case["link"], case["ticket"], case["decisions"], case["who"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


async def test_transition_cases_replay_on_the_reference_inbox() -> None:
    for case in VECTORS["transition_cases"]:
        box = aq.MemoryInbox()
        results: list[dict[str, Any]] = []
        for step in case["steps"]:
            if step["op"] == "take":
                row = await box.take(aq.validate_event(step["event"]), step["now"])
                results.append({"id": row.id, "seq": row.seq, "state": row.state, "ticket": row.ticket})
            elif step["op"] == "decided":
                state = aq.state_after(step["status"], step["body"])
                row = await box.decided(step["id"], step["decisions"], step["who"], state,
                                        {"status": step["status"], "body": step["body"]}, step["now"])
                results.append({"id": row.id, "seq": row.seq, "state": row.state, "answered_by": row.answered_by})
            elif step["op"] == "seen":
                row = await box.seen(step["id"], step["now"])
                results.append({"id": row.id, "seq": row.seq, "state": row.state})
            elif step["op"] == "expire":
                changed = await box.expire(step["now"])
                results.append({"expired": [{"id": r.id, "seq": r.seq} for r in changed]})
            elif step["op"] == "since":
                rows = await box.since(step["seq"], step["limit"])
                results.append({"ids": [r.id for r in rows], "seqs": [r.seq for r in rows]})
        assert results == case["results"], case["name"]
        final = [
            {"id": r.id, "seq": r.seq, "kind": r.kind, "state": r.state, "ticket": r.ticket, "turn": r.turn,
             "requested_at": r.requested_at, "expires_at": r.expires_at, "updated_at": r.updated_at,
             "answered_by": r.answered_by, "outcome": r.outcome}
            for r in sorted(await box.since(0, 100), key=lambda r: r.id)
        ]  # fmt: skip
        assert final == case["final"], case["name"]


# --- configuration and the fresh inbox -------------------------------------------------------------


@pytest.fixture
def configure() -> Iterator[Callable[[dict[str, Any]], Any]]:
    """Set keys on the shared configuration, reload the settings and give the module a fresh memory inbox and
    wake-ups; clear everything afterwards."""
    touched: list[str] = []
    aq._inbox = aq.MemoryInbox()
    aq._wakeups = aq.Wakeups()

    def apply(values: dict[str, Any]) -> Any:
        for key, value in values.items():
            app_config().set(key, value)
            touched.append(key)
        return aq.configure()

    yield apply
    for key in touched:
        app_config().set(key, None)
    aq._settings = None
    aq._inbox = None
    aq._wakeups = aq.Wakeups()


BASE: dict[str, Any] = {aq.INBOX: "memory", aq.AUTH: "none", aq.REPLY_TOKEN: "", "event.stream.keep.alive": "1s"}


@pytest.mark.parametrize(
    ("values", "names"),
    [
        ({aq.INBOX: "redis"}, "question.inbox must be memory or postgres"),
        ({aq.AUTH: "basic"}, "question.auth must be one of none, proxy, oauth2"),
        ({aq.AUTH: "proxy", aq.AUTH_HEADER: ""}, "question.auth.header names the header"),
        ({aq.AUTH: "oauth2"}, "set question.auth.oauth2.issuer or question.auth.oauth2.jwks-url"),
        ({aq.POSTGRES_PREFIX + "port": "db"}, "question.inbox.postgres.port must be a port number"),
        ({aq.SWEEP: "0"}, "question.sweep must be a duration"),
    ],
)
def test_the_boot_fails_by_name(configure: Callable[[dict[str, Any]], Any], values: dict[str, Any], names: str) -> None:
    with pytest.raises(aq.QuestionError) as refused:
        configure({**BASE, **values})
    assert names in str(refused.value)


def test_the_postgres_settings_follow_the_audit_store(configure: Callable[[dict[str, Any]], Any]) -> None:
    settings = configure({
        **BASE, aq.INBOX: "postgres", aq.POSTGRES_PREFIX + "host": "db.internal", aq.POSTGRES_PREFIX + "port": "6543",
        aq.POSTGRES_PREFIX + "database": "agents", aq.POSTGRES_PREFIX + "username": "inbox",
        aq.POSTGRES_PREFIX + "password": "s3", aq.POSTGRES_PREFIX + "ssl": "false", aq.POSTGRES_PREFIX + "timeout.ms": 2500,
    })  # fmt: skip
    assert settings.postgres == {
        "host": "db.internal", "port": 6543, "dbname": "agents", "user": "inbox", "password": "s3",
        "sslmode": "disable", "connect_timeout": 2, "options": "-c statement_timeout=2500",
    }  # fmt: skip
    assert settings.postgres_timeout_s == 2.5 and settings.sweep_ms == 30_000 and settings.reply_timeout_ms == 10_000


# --- the intake on the bus --------------------------------------------------------------------------


async def test_the_intake_upserts_a_wait_appends_an_advice_and_wakes_the_devices(
    configure: Callable[[dict[str, Any]], Any],
) -> None:
    configure(BASE)
    queue = aq._wakeups.subscribe()
    office = PostOffice(registry=default_registry)
    try:
        first = await office.request(aq.INTAKE_ROUTE, wait_event(), timeout_ms=5000)
        assert first.get_status() == 200, first.body
        assert first.body == {"stored": True, "id": 1, "seq": 1, "state": "open"}
        again = await office.request(aq.INTAKE_ROUTE, wait_event(ticket="t-second"), timeout_ms=5000)
        assert again.body == {"stored": True, "id": 1, "seq": 2, "state": "open"}, "the same turn upserts the same row"
        advice = await office.request(aq.INTAKE_ROUTE, advice_event(), timeout_ms=5000)
        assert advice.body == {"stored": True, "id": 2, "seq": 3, "state": "open"}
        bad = await office.request(aq.INTAKE_ROUTE, {"kind": "notice"}, timeout_ms=5000)
        assert bad.get_status() == 400 and "kind must be one of" in str(bad.body)
        rows = await aq.inbox().since(0, 10)
        assert [(r.id, r.seq, r.kind, r.ticket) for r in rows] == [(1, 2, "approval", "t-second"), (2, 3, "advice", None)]
        woken = [queue.get_nowait().seq for _ in range(3)]
        assert woken == [1, 2, 3], "every change reaches a connected device"
    finally:
        aq._wakeups.unsubscribe(queue)
        await default_registry.bus.close()


# --- the person's side over the host -----------------------------------------------------------------


class FakeReply:
    """The main flow's reply endpoint as the app sees it: scripted by ticket, recording every click."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def reply(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.posts.append({"body": body, "authorization": request.headers.get("authorization")})
        ticket = body.get("ticket")
        if ticket == "t-expired":
            return web.json_response({"type": "expired", "message": "Ticket expired"}, status=410)
        if ticket == "t-refused":
            return web.json_response({"type": "approval", "turn": 2, "error": "not a call waiting for approval",
                                      "calls": CALLS})
        return web.json_response({"type": "answer", "turn": 3, "run": "resume", "message": "Your refund has been issued."})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/ask/reply", self.reply)
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


@pytest_asyncio.fixture
async def host() -> AsyncIterator[str]:
    runner, url = await _serve(EventApiServer(FunctionRegistry()).create_app(routes=[aq.mount]))
    try:
        yield url
    finally:
        await runner.cleanup()
        await aq.shutdown()


async def _take(event: dict[str, Any]) -> Any:
    row = await aq.inbox().take(aq.validate_event(event), iso_utc())
    aq._wakeups.publish([row])
    return row


async def test_the_replay_lists_every_change_since_the_cursor(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure(BASE)
    await _take(wait_event())
    await _take(advice_event())
    await _take(wait_event(ticket="t-plan", correlation_id="release-42", graph_id="plan-assistant", kind="question", turn=1))
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/questions?since=0") as response:
            page = await response.json()
            assert response.status == 200
        assert [r["id"] for r in page["rows"]] == [1, 2, 3] and page["next"] == 3 and page["more"] is False
        assert page["rows"][0]["calls"] == CALLS and page["rows"][0]["ticket"] == "t-answer"
        assert page["rows"][1]["kind"] == "advice" and page["rows"][1]["details"].startswith("The assistant runs")
        async with http.get(f"{host}/questions?since=2&limit=1") as response:
            tail = await response.json()
        assert [r["id"] for r in tail["rows"]] == [3] and tail["next"] == 3
        async with http.get(f"{host}/questions?since=x") as response:
            assert response.status == 400
        async with http.get(f"{host}/questions/app") as response:
            assert response.status == 200 and "Agent questions" in await response.text()


async def test_the_stream_replays_then_follows_the_changes_with_keep_alives(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure(BASE)
    await _take(wait_event())
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

        replayed = await frame()
        assert replayed[0] == "id: 1" and replayed[1] == "event: question"
        assert json.loads(replayed[2][len("data: "):])["ticket"] == "t-answer"
        ping = await frame()
        assert ping == [": ping"], "a keep-alive while nothing changes"
        await _take(advice_event())
        live = await frame()
        assert live[0] == "id: 2" and json.loads(live[2][len("data: "):])["kind"] == "advice"
    async with aiohttp.ClientSession() as http, http.get(f"{host}/questions/events",
                                                          headers={"last-event-id": "1"}) as response:
        first = (await asyncio.wait_for(response.content.readline(), 5)).decode()
        assert first == "id: 2\n", "the reconnect resumes after the cursor the browser kept"


async def test_a_decision_is_forwarded_recorded_and_broadcast(
    configure: Callable[[dict[str, Any]], Any], reply_endpoint: tuple[FakeReply, str], host: str
) -> None:
    fake, base = reply_endpoint
    configure({**BASE, aq.REPLY_TOKEN: "channel-app-bearer"})
    row = await _take(wait_event(ticket="t-answer", base=base))
    queue = aq._wakeups.subscribe()
    decisions = [{"id": "toolu_01A", "approved": True, "note": "Carrier confirmed the loss"}]
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{host}/questions/{row.id}/decide",
                             json={"decisions": decisions, "decided_by": "operator-17"}) as response:
            result = await response.json()
            assert response.status == 200, result
        assert result["row"]["state"] == "answered" and result["row"]["answered_by"] == "operator-17"
        assert result["outcome"]["status"] == 200 and result["outcome"]["body"]["type"] == "answer"
        assert fake.posts[0]["body"] == {"ticket": "t-answer", "decisions": decisions, "decided_by": "operator-17"}
        assert fake.posts[0]["authorization"] == "Bearer channel-app-bearer"
        woken = queue.get_nowait()
        assert woken.id == row.id and woken.state == "answered"
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": decisions}) as response:
            assert response.status == 409 and "answered" in (await response.json())["message"]
        # an expired ticket, a refused decision and an endpoint that does not answer leave their states
        expired = await _take(wait_event(ticket="t-expired", base=base, correlation_id="case-2"))
        async with http.post(f"{host}/questions/{expired.id}/decide", json={"decisions": decisions}) as response:
            assert (await response.json())["row"]["state"] == "expired"
        refused = await _take(wait_event(ticket="t-refused", base=base, correlation_id="case-3"))
        async with http.post(f"{host}/questions/{refused.id}/decide", json={"decisions": decisions}) as response:
            body = await response.json()
        assert body["row"]["state"] == "open" and body["outcome"]["body"]["error"].startswith("not a call")
        silent = await _take(wait_event(ticket="t-silent", base="http://127.0.0.1:9", correlation_id="case-4"))
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
        advice = await _take(advice_event())
        async with http.post(f"{host}/questions/{advice.id}/decide", json={"decisions": decisions}) as response:
            assert response.status == 409 and "advice" in (await response.json())["message"]
        async with http.post(f"{host}/questions/{advice.id}/seen") as response:
            assert (await response.json())["row"]["state"] == "seen"
        async with http.post(f"{host}/questions/{row.id}/seen") as response:
            assert response.status == 409
    aq._wakeups.unsubscribe(queue)


async def test_open_waits_past_their_expiry_are_marked_expired(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure(BASE)
    past = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=5))
    stale = await _take(wait_event(ticket="t-stale", expires_at=past))
    fresh = await _take(wait_event(ticket="t-fresh", correlation_id="case-2"))
    queue = aq._wakeups.subscribe()
    async with aiohttp.ClientSession() as http, http.get(f"{host}/questions?since=0") as response:
        rows = {r["id"]: r["state"] for r in (await response.json())["rows"]}
    assert rows == {stale.id: "expired", fresh.id: "open"}
    assert queue.get_nowait().id == stale.id, "the expiry is broadcast like any change"
    aq._wakeups.unsubscribe(queue)


async def test_the_proxy_mode_takes_the_gateway_s_header(
    configure: Callable[[dict[str, Any]], Any], reply_endpoint: tuple[FakeReply, str], host: str
) -> None:
    fake, base = reply_endpoint
    configure({**BASE, aq.AUTH: "proxy"})
    row = await _take(wait_event(base=base))
    decisions = [{"id": "toolu_01A", "approved": False}]
    async with aiohttp.ClientSession() as http:
        for path in ("/questions?since=0", "/questions/events", "/questions/app"):
            async with http.get(f"{host}{path}") as response:
                assert response.status == 401, path
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": decisions}) as response:
            assert response.status == 401
        async with http.post(f"{host}/questions/{row.id}/decide", json={"decisions": decisions, "decided_by": "spoof"},
                             headers={"x-forwarded-user": "operator-17"}) as response:
            assert response.status == 200
        assert fake.posts[0]["body"]["decided_by"] == "operator-17", "the gateway's name, never the body's"


# --- the PostgreSQL inbox on a recording fake of the pool ----------------------------------------------


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
        return _FakeCursor(self._pool.rows)


class _FakePool:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.log: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows = rows or []
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
    now = datetime(2026, 11, 19, 9, 12, 5, 123000, tzinfo=timezone.utc)
    values: dict[str, Any] = {
        "id": 7, "seq": 12, "ticket": "t-answer", "graph_id": "order-assistant", "correlation_id": "order-1002-case",
        "turn": 2, "kind": "approval", "description": "Customer-support assistant", "summary": "A refund waits",
        "details": None, "message": {"kind": "approval", "calls": CALLS}, "link": "https://ask.example.test/ask/t-answer",
        "state": "open", "requested_at": now, "expires_at": now + timedelta(days=2), "updated_at": now,
        "answered_at": None, "answered_by": None, "decisions": None, "outcome": None,
    }  # fmt: skip
    values.update(overrides)
    return tuple(values[name.strip()] for name in aq.COLUMNS.split(","))


async def test_the_postgres_inbox_runs_the_statements_and_maps_the_records() -> None:
    pool = _FakePool([_record()])
    box = aq.PostgresInbox(pool, 2.5)
    await box.open()
    assert pool.opened == (True, 2.5)
    now = "2026-11-19T09:12:05.123Z"
    row = await box.take(aq.validate_event(wait_event()), now)
    sql, params = pool.log[-1]
    assert sql == aq.SQL_UPSERT_WAIT and params is not None
    assert params[0] == "t-answer" and params[1:5] == ("order-assistant", "order-1002-case", 2, "approval")
    assert json.loads(params[7])["calls"] == CALLS and params[11] == now
    assert row.id == 7 and row.seq == 12 and row.requested_at == now and row.expires_at == "2026-11-21T09:12:05.123Z"
    assert row.message["calls"] == CALLS and row.public()["calls"] == CALLS
    await box.take(aq.validate_event(advice_event()), now)
    sql, params = pool.log[-1]
    assert sql == aq.SQL_INSERT_ADVICE and params is not None and params[3] == "Customer-support assistant"
    await box.decided(7, [{"id": "toolu_01A", "approved": True}], "operator-17", "answered", {"status": 200, "body": {}}, now)
    sql, params = pool.log[-1]
    assert sql == aq.SQL_DECIDED and params == ("answered", '[{"id":"toolu_01A","approved":true}]', '{"status":200,"body":{}}',
                                                 "operator-17", now, now, 7)
    await box.seen(8, now)
    assert pool.log[-1] == (aq.SQL_SEEN, (now, 8))
    await box.expire(now)
    assert pool.log[-1] == (aq.SQL_EXPIRE, (now, now))
    await box.since(3, 50)
    assert pool.log[-1] == (aq.SQL_SINCE, (3, 50))
    await box.get(7)
    assert pool.log[-1] == (aq.SQL_GET, (7,))
    pool.rows = [("open", 3), ("answered", 1)]
    assert await box.counts() == {"open": 3, "answered": 1}
    await box.close()
    assert pool.closed


def test_a_record_s_json_may_arrive_as_text() -> None:
    record = _record(message='{"kind":"advice"}', decisions="[]", outcome='{"status":410}', kind="advice", ticket=None,
                     link=None, turn=None, expires_at=None, state="seen")
    row = aq.row_from_record(record)
    assert row.message == {"kind": "advice"} and row.decisions == [] and row.outcome == {"status": 410}
    assert row.turn is None and row.expires_at is None and row.state == "seen"


# --- the hooks and the boot ---------------------------------------------------------------------------


async def test_the_platform_hooks_mount_the_routes_and_run_the_boot(
    configure: Callable[[dict[str, Any]], Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(BASE)
    fresh = Platform(FunctionRegistry())
    monkeypatch.setattr(aq, "platform", fresh)
    aq._attach()
    app = fresh.create_app()
    paths = [route.resource.canonical for route in app.router.routes() if route.resource is not None]
    assert "/questions/app" in paths and "/questions/events" in paths and "/questions/{id}/decide" in paths
    assert paths.index("/questions") < paths.index("/{unknown}")
    runner = web.AppRunner(app)
    with caplog.at_level("WARNING"):
        await runner.setup()
    try:
        assert "The inbox is in memory" in caplog.text
        assert aq.inbox().name == "memory" and aq._sweeper is not None
        info = await aq.question_health({"type": "info"}, None)
        assert info["service"] == "agent.question.inbox" and info["inbox"] == "memory" and info["auth"] == "none"
        assert await aq.question_health({}, None) == "agent.question.inbox is running fine"
    finally:
        await runner.cleanup()
    assert aq._sweeper is None, "the cleanup stops the sweeper"


# --- a live PostgreSQL --------------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AGENT_QUESTION_TEST_URL"),
    reason="a live PostgreSQL: set AGENT_QUESTION_TEST_URL, e.g. postgres://mercury:...@127.0.0.1:5432/mercury?sslmode=disable",
)
async def test_the_rows_land_on_a_live_postgres() -> None:
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(os.environ["AGENT_QUESTION_TEST_URL"], min_size=1, max_size=2, open=False)
    box = aq.PostgresInbox(pool, 10.0)
    await box.open()
    try:
        async with pool.connection() as connection:
            await connection.execute((_SCHEMA / "postgres.sql").read_bytes())
        cid = f"live-{int(datetime.now(timezone.utc).timestamp())}"
        first = await box.take(aq.validate_event(wait_event(ticket=f"{cid}-1", correlation_id=cid)), iso_utc())
        again = await box.take(aq.validate_event(wait_event(ticket=f"{cid}-2", correlation_id=cid)), iso_utc())
        assert again.id == first.id and again.seq > first.seq and again.ticket == f"{cid}-2", "the same turn upserts"
        advice = await box.take(aq.validate_event(advice_event(correlation_id=cid)), iso_utc())
        assert advice.id != first.id and advice.kind == "advice"
        decided = await box.decided(first.id, [{"id": "toolu_01A", "approved": True}], "operator-17", "answered",
                                    {"status": 200, "body": {"type": "answer"}}, iso_utc())
        assert decided is not None and decided.state == "answered" and decided.outcome == {"status": 200, "body": {"type": "answer"}}
        seen = await box.seen(advice.id, iso_utc())
        assert seen is not None and seen.state == "seen"
        stale = await box.take(aq.validate_event(wait_event(ticket=f"{cid}-3", correlation_id=f"{cid}-b",
                                                             expires_at=iso_utc(datetime.now(timezone.utc) - timedelta(seconds=1)))),
                               iso_utc())
        expired = await box.expire(iso_utc())
        assert any(r.id == stale.id and r.state == "expired" for r in expired)
        rows = await box.since(first.seq, 100)
        assert {r.id for r in rows} >= {first.id, advice.id, stale.id}
        assert (await box.counts()).get("answered", 0) >= 1
    finally:
        await box.close()
