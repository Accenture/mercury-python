"""Ask a person - the main flow (extensions/ask-person/ask_person.py).

The shared vectors first (tests/vectors/ask-person-vectors.json): the ticket issued, verified, tampered,
expired and rotated; the message validated in its fixed order and the event built; the re-advice and the
resume requests the reply page and endpoint send; the outcomes the invoker gets back; the row a message
becomes in the question record, the state an answer leaves and the record's transitions on the reference
store. Then the function on the bus with channels that take, refuse and miss the event - the record
written before they are told - and the reply page and endpoint against a fake engine served in-process:
the page's shell and the wait read without resuming, the decisions forwarded with who decided prefixed to
each note and the answer recorded, an expired ticket refused before the engine is called, the two auth
modes (none, a signed access token against a JWKS the fake serves) with the page's sign-in configuration
and a channel app's bearer, the PostgreSQL store's statements on a recording fake of the pool, the host
hook that mounts the routes before the catch-all, the boot that fails by name, the health route and the
inspect command.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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

from mercury_composable import FunctionRegistry, Platform, PostOffice, app_config, default_registry
from mercury_composable.server import EventApiServer

_TESTS = Path(__file__).resolve().parent
_MODULE = _TESTS.parent / "extensions" / "ask-person" / "ask_person.py"

_spec = importlib.util.spec_from_file_location("ask_person_under_test", _MODULE)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes are
# unknowable statically - Any tells every analyzer to trust the runtime
ask: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("ask_person_under_test", ask)
_spec.loader.exec_module(ask)

VECTORS: dict[str, Any] = json.loads((_TESTS / "vectors" / "ask-person-vectors.json").read_text("utf-8"))

KEY = "test-key-current"
PREVIOUS = "test-key-previous"
REPLY_URL = "https://ask.example.test"
APP_TOKEN = "channel-app-bearer"
ISSUER = "https://issuer.example.test"
AUDIENCE = "ask-person"

CALLS = [
    {"id": "toolu_01A", "name": "issue_refund", "route": "demo.refund.issue", "wait": True,
     "input": {"order_id": "ORD-1002", "amount": 174.0, "reason": "Shipment lost in transit"}},
    {"id": "toolu_02B", "name": "ask_owner", "ask": True, "wait": True,
     "input": {"question": "Refund in full or offer a replacement?", "options": ["refund", "replacement"]}},
    {"id": "toolu_03C", "name": "order_lookup", "route": "demo.order.lookup", "wait": False,
     "input": {"order_id": "ORD-1002"}},
]


def approval_message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "approval",
        "graph_id": "order-assistant",
        "description": "Customer-support assistant: a refund waits for a supervisor",
        "correlation_id": "order-1002-case",
        "turn": 2,
        "node": "assist",
        "summary": "issue_refund for ORD-1002, 174.00 USD",
        "calls": CALLS,
        "ttl": "2d",
    }
    message.update(overrides)
    return message


def _names(section: str) -> list[str]:
    return [case["name"] for case in VECTORS[section]]


def _case(section: str, name: str) -> dict[str, Any]:
    return next(case for case in VECTORS[section] if case["name"] == name)


# --- the shared vectors ---------------------------------------------------------------------------


def test_the_vector_file_is_the_shared_contract() -> None:
    assert VECTORS["contract"] == "ask-person" and VECTORS["version"] == 2
    for section in ("ticket_cases", "verify_cases", "message_cases", "readvice_cases", "reply_cases",
                    "outcome_cases", "record_cases", "answer_cases", "transition_cases"):
        assert VECTORS[section], section


@pytest.mark.parametrize("name", _names("ticket_cases"))
def test_ticket_cases(name: str) -> None:
    case = _case("ticket_cases", name)
    assert ask.issue_ticket(case["payload"], case["key"].encode()) == case["ticket"]


@pytest.mark.parametrize("name", _names("verify_cases"))
def test_verify_cases(name: str) -> None:
    case = _case("verify_cases", name)
    keys = [key.encode() for key in case["keys"]]
    if "payload" in case:
        assert ask.verify_ticket(case["ticket"], keys, case["now"]) == case["payload"]
    else:
        with pytest.raises(ask.TicketError) as refused:
            ask.verify_ticket(case["ticket"], keys, case["now"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["message"])


@pytest.mark.parametrize("name", _names("message_cases"))
def test_message_cases(name: str) -> None:
    case = _case("message_cases", name)
    if "event" in case:
        message = ask.validate_message(case["message"])
        assert ask.build_event(message, case["key"].encode(), case["reply_url"]) == case["event"]
    else:
        with pytest.raises(ask.AppException) as refused:
            ask.validate_message(case["message"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


@pytest.mark.parametrize("name", _names("readvice_cases"))
def test_readvice_cases(name: str) -> None:
    case = _case("readvice_cases", name)
    if "request" in case:
        request = ask.readvice_request(case["payload"], case["graphs"])
        assert {"url": request.url, "headers": request.headers, "body": request.body} == case["request"]
    else:
        with pytest.raises(ask.ReplyError) as refused:
            ask.readvice_request(case["payload"], case["graphs"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


@pytest.mark.parametrize("name", _names("reply_cases"))
def test_reply_cases(name: str) -> None:
    case = _case("reply_cases", name)
    if "request" in case:
        request = ask.build_resume(case["payload"], case["decisions"], case["who"], case["at"], case["graphs"])
        assert {"url": request.url, "headers": request.headers, "body": request.body} == case["request"]
    else:
        with pytest.raises(ask.ReplyError) as refused:
            ask.build_resume(case["payload"], case["decisions"], case["who"], case["at"], case["graphs"])
        assert (refused.value.status, refused.value.message) == (case["status"], case["error"])


@pytest.mark.parametrize("name", _names("outcome_cases"))
def test_outcome_cases(name: str) -> None:
    case = _case("outcome_cases", name)
    engine = case["engine"]
    assert ask.map_outcome(engine["status"], engine["body"], engine["error"]) == (case["status"], case["body"])


@pytest.mark.parametrize("name", _names("record_cases"))
def test_record_cases(name: str) -> None:
    case = _case("record_cases", name)
    key = ask.wait_key(case["event"])
    assert (list(key) if key else None) == case["key"]
    assert ask.row_fields(case["event"], case["now"]) == case["row"]


@pytest.mark.parametrize("name", _names("answer_cases"))
def test_answer_cases(name: str) -> None:
    case = _case("answer_cases", name)
    assert ask.answer_state(case["status"], case["body"]) == case["state"]


async def test_transition_cases_replay_on_the_reference_store() -> None:
    for case in VECTORS["transition_cases"]:
        store = ask.MemoryQuestionStore()
        results: list[Any] = []
        for step in case["steps"]:
            if step["op"] == "record":
                row = await store.record(step["event"], step["now"])
                results.append({"id": row.id, "seq": row.seq, "state": row.state, "ticket": row.ticket})
            elif step["op"] == "answer":
                state = ask.answer_state(step["status"], step["body"])
                row = await store.answer(tuple(step["key"]), step["decisions"], step["who"], state,
                                         {"status": step["status"], "body": step["body"]}, step["now"])
                results.append(None if row is None else
                               {"id": row.id, "seq": row.seq, "state": row.state, "answered_by": row.answered_by})
            elif step["op"] == "expire":
                changed = await store.expire(step["now"])
                results.append({"expired": [{"id": r.id, "seq": r.seq} for r in changed]})
            elif step["op"] == "since":
                rows = await store.since(step["seq"], step["limit"])
                results.append({"ids": [r.id for r in rows], "seqs": [r.seq for r in rows]})
        assert results == case["results"], case["name"]
        final = [
            {"id": r.id, "seq": r.seq, "kind": r.kind, "state": r.state, "ticket": r.ticket, "turn": r.turn,
             "requested_at": r.requested_at, "expires_at": r.expires_at, "updated_at": r.updated_at,
             "answered_by": r.answered_by, "outcome": r.outcome}
            for r in sorted((await store.since(0, 100)), key=lambda r: r.id)
        ]
        assert final == case["final"], case["name"]
        assert (await store.counts()) == {
            label: sum(1 for r in final if (r["state"] or r["kind"]) == label)
            for label in {r["state"] or r["kind"] for r in final}
        }


def test_the_duration_grammar() -> None:
    assert ask.duration_ms("500ms") == 500 and ask.duration_ms("30m") == 1_800_000
    assert ask.duration_ms("12h") == 43_200_000 and ask.duration_ms("2d") == 172_800_000
    assert ask.duration_ms(90) == 90_000 and ask.duration_ms("90") == 90_000
    assert ask.duration_ms(True) is None and ask.duration_ms("soon") is None and ask.duration_ms(-1) is None


def test_requested_at_defaults_to_now_and_the_expiry_follows_the_ttl() -> None:
    before = time.time()
    event = ask.build_event(ask.validate_message(approval_message(ttl="1h")), KEY.encode(), REPLY_URL)
    requested = ask.parse_iso(event["requested_at"]).timestamp()
    expires = ask.parse_iso(event["expires_at"]).timestamp()
    assert before - 1 <= requested <= time.time() + 1
    assert abs((expires - requested) - 3600) < 0.001
    payload = ask.verify_ticket(event["ticket"], [KEY.encode()], time.time())
    assert payload == {"g": "order-assistant", "c": "order-1002-case", "t": 2, "k": "approval", "e": int(expires)}
    assert event["link"] == f"{REPLY_URL}/ask/{event['ticket']}"


# --- configuration ----------------------------------------------------------------------------------


@pytest.fixture
def configure() -> Iterator[Callable[[dict[str, Any]], Any]]:
    """Set keys on the shared configuration, reload the settings, and clear them afterwards."""
    touched: list[str] = []

    def apply(values: dict[str, Any]) -> Any:
        for key, value in values.items():
            app_config().set(key, value)
            touched.append(key)
        return ask.configure()

    yield apply
    for key in touched:
        app_config().set(key, None)
    ask._settings = None


BASE: dict[str, Any] = {
    ask.TICKET_KEY: KEY,
    ask.PREVIOUS_KEY: PREVIOUS,
    ask.REPLY_URL: REPLY_URL,
    ask.CHANNELS: [],
    ask.CHANNEL_ENDPOINTS: [],
    ask.GRAPHS: [],
    ask.REPLY_AUTH: "none",
    ask.APP_TOKEN: "",
    ask.AUDIT: "memory",
}


def test_settings_read_lists_and_pairs_in_both_forms(configure: Callable[[dict[str, Any]], Any]) -> None:
    settings = configure({
        **BASE,
        ask.CHANNELS: ["agent.question.inbox", "chat.post"],
        ask.CHANNEL_ENDPOINTS: [{"route": "agent.question.inbox", "url": "http://127.0.0.1:8092/api/event"}],
        ask.GRAPHS: [{"id": "order-assistant", "entry": "http://127.0.0.1:8085/api/graph/order-assistant"}],
        ask.CHANNEL_TIMEOUT: "2s",
    })
    assert settings.channels == ("agent.question.inbox", "chat.post")
    assert settings.channel_endpoints == {"agent.question.inbox": "http://127.0.0.1:8092/api/event"}
    assert settings.graphs == {"order-assistant": "http://127.0.0.1:8085/api/graph/order-assistant"}
    assert settings.channel_timeout_ms == 2000 and settings.engine_timeout_ms == 60_000
    assert settings.keys == (KEY.encode(), PREVIOUS.encode())
    as_text = configure({
        ask.CHANNELS: "a.route, b.route",
        ask.CHANNEL_ENDPOINTS: "b.route=http://peer:8085/api/event",
        ask.GRAPHS: "g1=http://e/1, g2=http://e/2",
    })
    assert as_text.channels == ("a.route", "b.route")
    assert as_text.channel_endpoints == {"b.route": "http://peer:8085/api/event"}
    assert as_text.graphs == {"g1": "http://e/1", "g2": "http://e/2"}


@pytest.mark.parametrize(
    ("values", "names"),
    [
        ({ask.TICKET_KEY: ""}, "ask.ticket.key is not set"),
        ({ask.REPLY_URL: "ask.example.test"}, "ask.reply.url must be the public base"),
        ({ask.REPLY_AUTH: "basic"}, "ask.reply.auth must be one of none, oauth2"),
        ({ask.REPLY_AUTH: "proxy"}, "ask.reply.auth must be one of none, oauth2"),
        ({ask.REPLY_AUTH: "oauth2"}, "set ask.reply.auth.oauth2.issuer or ask.reply.auth.oauth2.jwks-url"),
        ({ask.AUDIT: "mongo"}, "ask.audit must be one of memory, postgres"),
        ({ask.AUDIT_INSTANCES: "0"}, "ask.audit.instances must be a whole number >= 1"),
        ({ask.AUDIT_SWEEP: "never"}, "ask.audit.sweep must be a duration"),
        ({ask.GRAPHS: [{"id": "g", "entry": "ftp://x"}]}, "the entry of g must be an http or https URL"),
        ({ask.GRAPHS: [{"id": "g"}]}, "ask.graphs entries need id and entry"),
        ({ask.CHANNEL_ENDPOINTS: "nonsense"}, "ask.channel.endpoints takes route=url pairs"),
        ({ask.CHANNEL_TIMEOUT: "0"}, "ask.channel.timeout must be a duration"),
    ],
)
def test_the_boot_fails_by_name(
    configure: Callable[[dict[str, Any]], Any], values: dict[str, Any], names: str
) -> None:
    with pytest.raises(ask.AskError) as refused:
        configure({**BASE, **values})
    assert names in str(refused.value)


def test_a_disabled_extension_loads_without_a_key(configure: Callable[[dict[str, Any]], Any]) -> None:
    settings = configure({**BASE, ask.ENABLED: False, ask.TICKET_KEY: "", ask.PREVIOUS_KEY: ""})
    assert settings.enabled is False and settings.keys == ()


# --- the function on the bus -----------------------------------------------------------------------


ROWS_AT_DELIVERY: list[int] = []  # how many rows the record held when a channel was told


@pytest_asyncio.fixture
async def recorder() -> AsyncIterator[Any]:
    """The recorder on the default registry with a fresh memory store, as the boot registers it; the
    store is handed to the test and dropped afterwards."""
    store = ask.MemoryQuestionStore()
    ask._store = store
    if not default_registry.exists(ask.AUDIT_ROUTE):
        default_registry.register(ask.AUDIT_ROUTE, ask.question_audit, instances=2, private=True)
    try:
        yield store
    finally:
        ask._store = None
        # the bus binds its workers to the test's loop; a later test has another, so it is closed here as the
        # channel fixture closes it
        await default_registry.bus.close()


@pytest_asyncio.fixture
async def channel() -> AsyncIterator[list[dict[str, Any]]]:
    """A channel route on the default registry that records every event it takes (and how many rows
    the question record held at that moment), and one that refuses with a 500; the bus is closed
    afterwards, as the other extension tests do."""
    taken: list[dict[str, Any]] = []
    ROWS_AT_DELIVERY.clear()

    async def take(_headers: dict[str, str], body: Any) -> Any:
        taken.append(body)
        ROWS_AT_DELIVERY.append(len(await ask.question_store().since(0, 100)))
        return {"stored": True}

    async def refuse(_headers: dict[str, str], _body: Any) -> Any:
        raise ask.AppException(500, "the channel is down")

    default_registry.register("ask.test.channel", take, instances=1)
    default_registry.register("ask.test.refusing", refuse, instances=1)
    try:
        yield taken
    finally:
        await default_registry.bus.close()


async def test_person_ask_records_then_delivers_the_event_to_every_channel_best_effort(
    configure: Callable[[dict[str, Any]], Any], channel: list[dict[str, Any]], recorder: Any
) -> None:
    configure({**BASE, ask.CHANNELS: ["ask.test.channel", "ask.test.refusing", "ask.test.missing"]})
    office = PostOffice(registry=default_registry)
    reply = await office.request(ask.ROUTE, approval_message(), timeout_ms=5000)
    assert reply.get_status() == 200, reply.body
    body = reply.body
    assert body["delivered"] == [
        {"route": "ask.test.channel", "ok": True, "status": 200},
        {"route": "ask.test.refusing", "ok": False, "status": 500},
        {"route": "ask.test.missing", "ok": False, "status": 404},
    ]
    assert body["recorded"] is True
    assert body["link"] == f"{REPLY_URL}/ask/{body['ticket']}"
    assert len(channel) == 1
    event = channel[0]
    assert event["ticket"] == body["ticket"] and event["link"] == body["link"]
    assert event["expires_at"] == body["expires_at"] and event["calls"] == CALLS
    assert event["kind"] == "approval" and event["graph_id"] == "order-assistant" and event["turn"] == 2
    payload = ask.verify_ticket(body["ticket"], [KEY.encode()], time.time())
    assert payload["g"] == "order-assistant" and payload["c"] == "order-1002-case" and payload["t"] == 2
    # the record: the row was there before the channel was told, and holds the event as the channel got it
    assert ROWS_AT_DELIVERY == [1]
    rows = await recorder.since(0, 10)
    assert len(rows) == 1
    row = rows[0]
    assert (row.kind, row.state, row.turn) == ("approval", "open", 2)
    assert row.ticket == body["ticket"] and row.link == body["link"] and row.expires_at == body["expires_at"]
    assert row.message == event and row.public()["calls"] == CALLS


async def test_a_write_the_recorder_does_not_take_is_logged_and_never_fails_the_caller(
    configure: Callable[[dict[str, Any]], Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = configure({**BASE, ask.AUDIT_TIMEOUT: "1s"})
    event = ask.build_event(ask.validate_message(approval_message()), KEY.encode(), REPLY_URL)
    # no recorder anywhere: the write is refused by the bus and logged by type
    monkeypatch.setattr(ask, "_post_office", PostOffice(registry=FunctionRegistry()))
    with caplog.at_level("WARNING"):
        assert await ask.record({"record": event}, settings) is False
    assert "question.audit did not record" in caplog.text
    # a recorder that reports a failure of its store
    registry = FunctionRegistry()

    async def failing(_headers: dict[str, str], _body: Any) -> Any:
        return {"recorded": False, "error": "OperationalError sqlstate=08006"}

    registry.register(ask.AUDIT_ROUTE, failing, instances=1, private=True)
    monkeypatch.setattr(ask, "_post_office", PostOffice(registry=registry))
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert await ask.record({"answer": {}}, settings) is False
    assert "OperationalError sqlstate=08006" in caplog.text
    await registry.bus.close()
    # the recorder itself: a store that fails is reported by type, never raised; an unknown event is a 400

    class Broken:
        name = "broken"

        async def record(self, _event: Any, _now: str) -> Any:
            raise RuntimeError("the row would be quoted here")

    monkeypatch.setattr(ask, "_store", Broken())
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert await ask.question_audit({}, {"record": event}) == {"recorded": False, "error": "RuntimeError"}
    assert "could not write - RuntimeError" in caplog.text and "quoted" not in caplog.text
    with pytest.raises(ask.AppException) as refused:
        await ask.question_audit({}, {"nothing": True})
    assert refused.value.status == 400


async def test_an_advice_is_recorded_and_delivered_without_a_ticket(
    configure: Callable[[dict[str, Any]], Any], channel: list[dict[str, Any]], recorder: Any
) -> None:
    configure({**BASE, ask.CHANNELS: ["ask.test.channel"]})
    office = PostOffice(registry=default_registry)
    advice = {
        "kind": "advice", "graph_id": "order-assistant", "description": "Customer-support assistant",
        "correlation_id": "order-1002-case", "turn": 3, "node": "acknowledge",
        "summary": "Your decision was received", "details": "The assistant continues.",
    }
    reply = await office.request(ask.ROUTE, advice, timeout_ms=5000)
    assert reply.get_status() == 200, reply.body
    assert reply.body == {"delivered": [{"route": "ask.test.channel", "ok": True, "status": 200}], "recorded": True}
    assert "ticket" not in channel[0] and "link" not in channel[0] and channel[0]["details"] == advice["details"]
    rows = await recorder.since(0, 10)
    assert len(rows) == 1 and rows[0].kind == "advice" and rows[0].state is None and rows[0].ticket is None
    assert rows[0].details == advice["details"] and rows[0].turn == 3


async def test_the_function_refuses_a_bad_message_a_mismatched_graph_and_a_disabled_extension(
    configure: Callable[[dict[str, Any]], Any], channel: list[dict[str, Any]]
) -> None:
    configure({**BASE, ask.CHANNELS: ["ask.test.channel"]})
    office = PostOffice(registry=default_registry)
    bad = await office.request(ask.ROUTE, approval_message(ttl="soon"), timeout_ms=5000)
    assert bad.get_status() == 400 and "ttl must be a duration" in str(bad.body)
    mismatch = await office.request(ask.ROUTE, approval_message(), headers={"x-graph-id": "other"},
                                    timeout_ms=5000)
    assert mismatch.get_status() == 400 and "does not match the x-graph-id header" in str(mismatch.body)
    matched = await office.request(ask.ROUTE, approval_message(), headers={"x-graph-id": "order-assistant"},
                                   timeout_ms=5000)
    assert matched.get_status() == 200
    configure({ask.ENABLED: False})
    off = await office.request(ask.ROUTE, approval_message(), timeout_ms=5000)
    assert off.get_status() == 503 and "ask.enabled is false" in str(off.body)
    assert len(channel) == 1


async def test_health_reports_the_setup(configure: Callable[[dict[str, Any]], Any], recorder: Any) -> None:
    configure({**BASE, ask.CHANNELS: ["a.route"], ask.GRAPHS: "g1=http://e/1"})
    info = await ask.ask_health({"type": "info"}, None)
    assert info == {
        "service": "person.ask", "href": "http://127.0.0.1", "enabled": True, "channels": ["a.route"],
        "graphs": ["g1"], "reply_url": REPLY_URL, "auth": "none", "audit": "memory", "rows": {},
    }
    assert await ask.ask_health({"type": "health"}, None) == "person.ask is running fine"


# --- the reply page and endpoint against a fake engine ---------------------------------------------


class FakeEngine:
    """A graph entry as the Playground serves it: a request without decisions re-advises, one with
    decisions answers, an expired case says nothing is waiting; and the issuer's JWKS document."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.jwks: dict[str, Any] = {"keys": []}

    async def graph(self, request: web.Request) -> web.Response:
        body = await request.json()
        cid = request.headers.get("x-correlation-id")
        self.requests.append({"graph": request.match_info["graph_id"], "cid": cid, "body": body})
        if cid == "expired-case":
            return web.json_response({"type": "nothing-waiting", "cid": cid}, status=410)
        if "decisions" in body:
            return web.json_response({"type": "answer", "turn": body["turn"] + 1, "run": "resume",
                                      "message": "Your refund has been issued."})
        return web.json_response({"type": "approval", "turn": body["turn"], "run": "resume", "calls": CALLS,
                                  "message": "A tool call waits for a supervisor's decision.", "cid": cid})

    async def keys(self, _request: web.Request) -> web.Response:
        return web.json_response(self.jwks)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/graph/{graph_id}", self.graph)
        app.router.add_get("/jwks", self.keys)
        return app


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[tuple[FakeEngine, str]]:
    fake = FakeEngine()
    runner, url = await _serve(fake.app())
    try:
        yield fake, url
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def host() -> AsyncIterator[str]:
    """The pack's host with the extension's routes mounted, as Platform.add_routes mounts them."""
    runner, url = await _serve(EventApiServer(FunctionRegistry()).create_app(routes=[ask.mount]))
    try:
        yield url
    finally:
        await runner.cleanup()
        await ask.shutdown()
        # the reply endpoint sends to the recorder over the default registry's bus, whose workers bind to
        # this test's loop
        await default_registry.bus.close()


def _ticket(graph_id: str = "order-assistant", cid: str = "order-1002-case", turn: int = 2,
            kind: str = "approval", expires_in: int = 3600, key: str = KEY) -> str:
    return ask.issue_ticket({"g": graph_id, "c": cid, "t": turn, "k": kind, "e": int(time.time()) + expires_in},
                            key.encode())


async def test_the_page_is_a_shell_and_the_wait_is_read_without_resuming(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant"})
    ticket = _ticket()
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/ask/{ticket}") as response:
            page = await response.text()
            assert response.status == 200 and response.content_type == "text/html"
        assert fake.requests == [], "the page itself never calls the engine - its script reads the wait"
        assert f"const TICKET = {json.dumps(ticket)};" in page and f'const WAIT = "/ask/{ticket}/wait";' in page
        assert 'const REPLY = "/ask/reply";' in page and 'const CONFIG = "/ask/config";' in page
        assert "<title>Approval - order-assistant</title>" in page
        # the script carries the form the page renders and the sign-in it may run
        assert "waits for a decision" in page and "a question" in page and "runs on resume" in page
        assert "code_challenge_method: 'S256'" in page and "authorization_endpoint" in page
        async with http.get(f"{host}/ask/config") as response:
            assert response.status == 200 and await response.json() == {"auth": "none"}
        async with http.get(f"{host}/ask/{ticket}/wait") as response:
            assert response.status == 200
            shown = await response.json()
    assert fake.requests == [{"graph": "order-assistant", "cid": "order-1002-case", "body": {"turn": 2}}]
    assert shown["payload"] == {"g": "order-assistant", "c": "order-1002-case", "t": 2, "k": "approval"}
    assert shown["advice"]["type"] == "approval" and shown["advice"]["calls"] == CALLS


async def test_the_page_refuses_bad_tickets_and_reports_an_expired_wait(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant"})
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/ask/{_ticket(expires_in=-5)}") as response:
            assert response.status == 410 and "Ticket expired" in await response.text()
        async with http.get(f"{host}/ask/{_ticket(key='another')}") as response:
            assert response.status == 403 and "signature is invalid" in await response.text()
        async with http.get(f"{host}/ask/v1.nonsense") as response:
            assert response.status == 400 and "malformed" in await response.text()
        async with http.get(f"{host}/ask/{_ticket(graph_id='other-graph')}") as response:
            assert response.status == 404 and "not configured" in await response.text()
        async with http.get(f"{host}/ask/{_ticket(expires_in=-5)}/wait") as response:
            assert response.status == 410 and await response.json() == {"type": "expired", "message": "Ticket expired"}
        async with http.get(f"{host}/ask/{_ticket(graph_id='other-graph')}/wait") as response:
            assert response.status == 404 and "not configured" in (await response.json())["message"]
        assert fake.requests == [], "no ticket above reached the engine"
        async with http.get(f"{host}/ask/{_ticket(cid='expired-case')}/wait") as response:
            assert response.status == 410
            assert (await response.json())["type"] == "nothing-waiting"
    assert len(fake.requests) == 1


async def test_a_reply_resumes_the_graph_with_who_decided_on_each_note(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant"})
    ticket = _ticket()
    decisions = [{"id": "toolu_01A", "approved": True, "note": "Carrier confirmed the loss"},
                 {"id": "toolu_02B", "answer": "refund"}]
    body = {"ticket": ticket, "decisions": decisions}
    async with aiohttp.ClientSession() as http, http.post(f"{host}/ask/reply", json=body) as response:
        outcome = await response.json()
        assert response.status == 200
    assert outcome == {"type": "answer", "turn": 3, "run": "resume", "message": "Your refund has been issued."}
    sent = fake.requests[0]
    assert sent["graph"] == "order-assistant" and sent["cid"] == "order-1002-case"
    assert sent["body"]["turn"] == 2
    note = sent["body"]["decisions"][0]["note"]
    assert note.startswith("[anonymous at 20") and note.endswith("Z] Carrier confirmed the loss")
    assert sent["body"]["decisions"][1] == {"id": "toolu_02B", "answer": "refund"}


async def test_a_reply_is_refused_before_the_engine_when_the_ticket_or_the_decisions_are_wrong(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant"})
    good = [{"id": "toolu_01A", "approved": True}]
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{host}/ask/reply", data=b"not json") as response:
            assert response.status == 400 and "JSON map" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json={"decisions": good}) as response:
            assert response.status == 400 and (await response.json())["message"] == "ticket is required"
        async with http.post(f"{host}/ask/reply", json={"ticket": _ticket(expires_in=-5), "decisions": good}) as response:
            assert response.status == 410 and await response.json() == {"type": "expired", "message": "Ticket expired"}
        async with http.post(f"{host}/ask/reply", json={"ticket": _ticket(), "decisions": []}) as response:
            assert response.status == 400 and "non-empty list" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json={"ticket": _ticket(graph_id="x"), "decisions": good}) as response:
            assert response.status == 404 and "not configured" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json={"ticket": _ticket(), "decisions": good, "decided_by": 5}) as response:
            assert response.status == 400 and "decided_by must be text" in (await response.json())["message"]
        assert fake.requests == []
        async with http.post(f"{host}/ask/reply", json={"ticket": _ticket(cid="expired-case"), "decisions": good}) as response:
            assert response.status == 410 and (await response.json())["type"] == "nothing-waiting"
    assert len(fake.requests) == 1


async def test_the_engine_s_outcome_is_passed_through_and_a_silent_engine_is_a_502(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure({**BASE, ask.GRAPHS: "order-assistant=http://127.0.0.1:9/api/graph/order-assistant",
               ask.ENGINE_TIMEOUT: "2s"})
    async with aiohttp.ClientSession() as http:
        body = {"ticket": _ticket(), "decisions": [{"id": "toolu_01A", "approved": True}]}
        async with http.post(f"{host}/ask/reply", json=body) as response:
            assert response.status == 502
            outcome = await response.json()
    assert outcome["type"] == "error" and outcome["message"].startswith("the graph did not answer - ")


async def test_a_reply_records_the_answer_with_who_decided_and_the_outcome(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str, recorder: Any
) -> None:
    _fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant",
               ask.APP_TOKEN: APP_TOKEN})
    # the wait as person.ask records it; the reply's ticket is another one for the same turn
    event = ask.build_event(ask.validate_message(approval_message()), KEY.encode(), REPLY_URL)
    recorded = await recorder.record(event, ask.iso_utc())
    assert recorded.state == "open"
    body = {"ticket": _ticket(), "decisions": [{"id": "toolu_01A", "approved": True, "note": "Carrier confirmed"}],
            "decided_by": "operator-9"}
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{host}/ask/reply", json=body,
                             headers={"authorization": f"Bearer {APP_TOKEN}"}) as response:
            assert response.status == 200
        rows = await recorder.since(0, 10)
        assert len(rows) == 1
        row = rows[0]
        assert row.id == recorded.id and row.state == "answered" and row.answered_by == "operator-9"
        assert row.answered_at is not None and row.answered_at.endswith("Z")
        assert row.decisions is not None and row.decisions[0]["note"].startswith("[operator-9 at ")
        assert row.decisions[0]["note"].endswith("] Carrier confirmed")
        assert row.outcome == {"status": 200, "body": {"type": "answer", "turn": 3, "run": "resume",
                                                       "message": "Your refund has been issued."}}
        # a wait decided elsewhere: the engine says nothing waits, the record keeps the first answer
        again = {"ticket": _ticket(cid="expired-case"), "decisions": [{"id": "toolu_01A", "approved": False}]}
        async with http.post(f"{host}/ask/reply", json=again) as response:
            assert response.status == 410
    assert len(await recorder.since(0, 10)) == 1, "a wait nobody recorded is not written"
    assert (await recorder.since(0, 10))[0].answered_by == "operator-9"


async def test_the_oauth2_mode_validates_an_access_token_and_the_page_learns_how_to_sign_in(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    fake.jwks = {"keys": [{**public_jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant",
               ask.REPLY_AUTH: "oauth2", ask.OAUTH2_ISSUER: ISSUER, ask.OAUTH2_AUDIENCE: AUDIENCE,
               ask.OAUTH2_JWKS_URL: f"{engine_url}/jwks", ask.OAUTH2_CLIENT_ID: "ask-page",
               ask.APP_TOKEN: APP_TOKEN})
    ask._verifier = None
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u-17", "preferred_username": "operator-17",
              "iat": now, "exp": now + 300}
    token = jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, other_key.private_bytes(serialization.Encoding.PEM,
                                                         serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()),
                        algorithm="RS256", headers={"kid": "k1"})
    wrong_audience = jwt.encode({**claims, "aud": "someone-else"}, pem, algorithm="RS256", headers={"kid": "k1"})
    body = {"ticket": _ticket(), "decisions": [{"id": "toolu_01A", "approved": True}]}
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/ask/config") as response:
            assert await response.json() == {
                "auth": "oauth2",
                "oauth2": {"issuer": ISSUER, "client_id": "ask-page", "scopes": "openid profile email",
                           "audience": AUDIENCE},
            }
        async with http.get(f"{host}/ask/{_ticket()}") as response:
            assert response.status == 200, "the shell is open: the script signs the person in"
        async with http.get(f"{host}/ask/{_ticket()}/wait") as response:
            assert response.status == 401 and "bearer access token" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json=body) as response:
            assert response.status == 401 and "bearer access token" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json=body, headers={"authorization": f"Bearer {forged}"}) as response:
            assert response.status == 401 and "InvalidSignatureError" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json=body,
                             headers={"authorization": f"Bearer {wrong_audience}"}) as response:
            assert response.status == 401 and "InvalidAudienceError" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json=body, headers={"authorization": f"Bearer {token}"}) as response:
            assert response.status == 200
        assert fake.requests[-1]["body"]["decisions"][0]["note"].startswith("[operator-17 at ")
        async with http.get(f"{host}/ask/{_ticket()}/wait", headers={"authorization": f"Bearer {token}"}) as response:
            assert response.status == 200 and (await response.json())["advice"]["type"] == "approval"
        # a channel app's bearer names the person it acts for, in this mode too
        async with http.post(f"{host}/ask/reply", json={**body, "decided_by": "operator-9"},
                             headers={"authorization": f"Bearer {APP_TOKEN}"}) as response:
            assert response.status == 200
        assert fake.requests[-1]["body"]["decisions"][0]["note"].startswith("[operator-9 at ")


# --- the PostgreSQL store on a recording fake of the pool -----------------------------------------


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
        self.queue: list[list[tuple[Any, ...]]] = []  # answers for the next executes, when a test scripts them
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
    return tuple(values[name.strip()] for name in ask.COLUMNS.split(","))


async def test_the_postgres_store_runs_the_statements_and_maps_the_records() -> None:
    pool = _FakePool([_record()])
    store = ask.PostgresQuestionStore(pool, 2.5)
    await store.open()
    assert pool.opened == (True, 2.5)
    now = "2026-11-19T09:12:05.123Z"
    event = ask.build_event(ask.validate_message(approval_message(requested_at="2026-11-19T09:12:04.118Z")),
                            KEY.encode(), REPLY_URL)
    row = await store.record(event, now)
    sql, params = pool.log[-1]
    assert sql == ask.SQL_RECORD_WAIT and params is not None
    assert params[1:5] == ("order-assistant", "order-1002-case", 2, "approval") and params[0] == event["ticket"]
    assert json.loads(params[7]) == event, "the message travels as text for the jsonb column"
    assert params[9] == "2026-11-19T09:12:04.118Z" and params[11] == now
    assert row.id == 7 and row.seq == 12 and row.requested_at == "2026-11-19T09:12:05.123Z"
    assert row.expires_at == "2026-11-21T09:12:05.123Z" and row.message["calls"] == CALLS
    advice = ask.build_event(ask.validate_message({
        "kind": "advice", "graph_id": "order-assistant", "description": "Customer-support assistant",
        "correlation_id": "order-1002-case", "summary": "The queue is empty",
    }), KEY.encode(), REPLY_URL)
    await store.record(advice, now)
    sql, params = pool.log[-1]
    assert sql == ask.SQL_RECORD_ADVICE and params is not None and params[:2] == ("order-assistant", "order-1002-case")
    # the guard kept a newer row (no row returned): the standing row is read
    pool.queue = [[], [_record(seq=13)]]
    stale = await store.record(event, now)
    assert [entry[0] for entry in pool.log[-2:]] == [ask.SQL_RECORD_WAIT, ask.SQL_WAIT] and stale.seq == 13
    answered = await store.answer(("order-assistant", "order-1002-case", 2), [{"id": "toolu_01A", "approved": True}],
                                  "operator-17", "answered", {"status": 200, "body": {"type": "answer"}}, now)
    sql, params = pool.log[-1]
    assert sql == ask.SQL_ANSWER and params is not None and answered is not None
    assert params[0] == "answered" and params[3] == "operator-17" and params[6:9] == ("order-assistant", "order-1002-case", 2)
    assert params[9] == "answered" and json.loads(params[1]) == [{"id": "toolu_01A", "approved": True}]
    # the guard kept the first decision (no row returned): the row as stored comes back
    pool.queue = [[], [_record(state="answered", answered_by="operator-3")]]
    kept = await store.answer(("order-assistant", "order-1002-case", 2), [], "operator-9", "expired",
                              {"status": 410, "body": {"type": "nothing-waiting"}}, now)
    assert kept is not None and kept.answered_by == "operator-3"
    await store.expire(now)
    assert pool.log[-1] == (ask.SQL_EXPIRE, (now, now))
    await store.since(3, 50)
    assert pool.log[-1] == (ask.SQL_SINCE, (3, 50))
    pool.queue = [[("open", 2), ("advice", 1)]]
    assert await store.counts() == {"open": 2, "advice": 1}
    await store.close()
    assert pool.closed
    mapped = ask.row_from_record(_record(kind="advice", state=None, ticket=None, link=None,
                                         decisions='[{"id":"x","approved":true}]'))
    assert mapped.state is None and mapped.decisions == [{"id": "x", "approved": True}] and mapped.outcome is None
    assert ask.failure(RuntimeError("quotes a row")) == "RuntimeError"


async def test_the_boot_registers_the_recorder_and_a_silent_database_does_not_stop_it(
    configure: Callable[[dict[str, Any]], Any], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("psycopg_pool")
    configure({**BASE, ask.AUDIT: "postgres", ask.AUDIT_POSTGRES_PREFIX + "host": "127.0.0.1",
               ask.AUDIT_POSTGRES_PREFIX + "port": "9", ask.AUDIT_POSTGRES_PREFIX + "ssl": "false",
               ask.AUDIT_POSTGRES_PREFIX + "timeout.ms": "300", ask.AUDIT_INSTANCES: "3"})
    fresh = Platform(FunctionRegistry())
    monkeypatch.setattr(ask, "platform", fresh)
    ask._attach()
    runner = web.AppRunner(fresh.create_app())
    with caplog.at_level("WARNING"):
        await runner.setup()
    try:
        assert default_registry.exists(ask.AUDIT_ROUTE)
        assert ask._store is not None and ask._store.name == "postgres"
        assert "is not answering at start" in caplog.text
        assert ask._sweeper is not None and not ask._sweeper.done()
    finally:
        await runner.cleanup()
    assert ask._store is None and ask._sweeper is None, "the shutdown closed the store and stopped the sweeper"


# --- the host hook, the boot and the inspect command -----------------------------------------------


async def test_the_host_mounts_the_routes_before_the_catch_all_and_keeps_its_own(
    configure: Callable[[dict[str, Any]], Any], host: str
) -> None:
    configure(BASE)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/nothing/here") as response:
            assert response.status == 404
            assert (await response.json())["status"] == 404, "the catch-all still answers the engines' shape"
        async with http.get(f"{host}/health") as response:
            assert response.status == 200, "the actuator stays the host's"
        async with http.post(f"{host}/ask/reply", json={}) as response:
            assert response.status == 400, "the extension's path wins over the catch-all"
    fresh = Platform(FunctionRegistry())
    fresh.add_routes(ask.mount)
    app = fresh.create_app()
    paths = [route.resource.canonical for route in app.router.routes() if route.resource is not None]
    assert paths.index("/ask/reply") > paths.index("/api/event")
    assert paths.index("/ask/{ticket}") < paths.index("/{unknown}")
    assert paths.index("/ask/config") < paths.index("/ask/{ticket}"), "the configuration is never read as a ticket"


async def test_the_platform_hooks_run_the_boot_and_fail_by_name(
    configure: Callable[[dict[str, Any]], Any], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure(BASE)
    fresh = Platform(FunctionRegistry())
    monkeypatch.setattr(ask, "platform", fresh)
    ask._attach()
    runner = web.AppRunner(fresh.create_app())
    with caplog.at_level("WARNING"):
        await runner.setup()
    try:
        assert "No channel configured" in caplog.text and "No graph configured" in caplog.text
        assert "question record is in memory" in caplog.text
        assert default_registry.exists(ask.AUDIT_ROUTE) and ask._store is not None and ask._store.name == "memory"
    finally:
        await runner.cleanup()
    app_config().set(ask.TICKET_KEY, "")
    ask._settings = None
    failing = Platform(FunctionRegistry())
    monkeypatch.setattr(ask, "platform", failing)
    ask._attach()
    runner = web.AppRunner(failing.create_app())
    with pytest.raises(ask.AskError) as refused:
        await runner.setup()
    assert "ask.ticket.key is not set" in str(refused.value)
    await runner.cleanup()


def test_inspect_reads_a_ticket_or_says_why_not(
    configure: Callable[[dict[str, Any]], Any], capsys: pytest.CaptureFixture[str]
) -> None:
    configure(BASE)
    ticket = _ticket()
    assert ask.inspect([ticket]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["g"] == "order-assistant" and shown["k"] == "approval" and shown["expires_at"].endswith("Z")
    assert ask.inspect([_ticket(expires_in=-1)]) == 1
    assert capsys.readouterr().out.strip() == "410 Ticket expired"
    assert ask.inspect([]) == 1


# --- a live PostgreSQL --------------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("ASK_AUDIT_TEST_URL"),
    reason="a live PostgreSQL: set ASK_AUDIT_TEST_URL, e.g. postgres://mercury:...@127.0.0.1:5432/mercury?sslmode=disable",
)
async def test_the_record_lands_on_a_live_postgres() -> None:
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(os.environ["ASK_AUDIT_TEST_URL"], min_size=1, max_size=2, open=False)
    store = ask.PostgresQuestionStore(pool, 10.0)
    await store.open()
    try:
        async with pool.connection() as connection:
            await connection.execute((_MODULE.parent / "schema" / "postgres.sql").read_bytes())
        cid = f"live-{int(datetime.now(timezone.utc).timestamp())}"
        key = KEY.encode()
        first = await store.record(ask.build_event(ask.validate_message(approval_message(
            correlation_id=cid, requested_at="2026-11-19T09:00:00.000Z")), key, REPLY_URL), ask.iso_utc())
        again = await store.record(ask.build_event(ask.validate_message(approval_message(
            correlation_id=cid, requested_at="2026-11-19T09:05:00.000Z")), key, REPLY_URL), ask.iso_utc())
        assert again.id == first.id and again.seq > first.seq and again.ticket != first.ticket, "the same turn upserts"
        stale = await store.record(ask.build_event(ask.validate_message(approval_message(
            correlation_id=cid, requested_at="2026-11-19T08:00:00.000Z")), key, REPLY_URL), ask.iso_utc())
        assert stale.seq == again.seq and stale.ticket == again.ticket, "an older message never overwrites a newer row"
        advice = await store.record(ask.build_event(ask.validate_message({
            "kind": "advice", "graph_id": "order-assistant", "description": "Customer-support assistant",
            "correlation_id": cid, "summary": "The queue is empty",
        }), key, REPLY_URL), ask.iso_utc())
        assert advice.id != first.id and advice.kind == "advice" and advice.state is None
        answered = await store.answer(("order-assistant", cid, 2), [{"id": "toolu_01A", "approved": True}],
                                      "operator-17", "answered", {"status": 200, "body": {"type": "answer"}}, ask.iso_utc())
        assert answered is not None and answered.state == "answered"
        kept = await store.answer(("order-assistant", cid, 2), [{"id": "toolu_01A", "approved": False}],
                                  "operator-9", "expired", {"status": 410, "body": {"type": "nothing-waiting"}}, ask.iso_utc())
        assert kept is not None and kept.answered_by == "operator-17", "the first accepted decision stands"
        assert await store.answer(("order-assistant", f"{cid}-none", 1), [], "x", "answered", {}, ask.iso_utc()) is None
        soon = ask.iso_utc(datetime.now(timezone.utc) - timedelta(seconds=1))
        past = await store.record({**ask.build_event(ask.validate_message(approval_message(
            correlation_id=f"{cid}-b")), key, REPLY_URL), "expires_at": soon}, ask.iso_utc())
        expired = await store.expire(ask.iso_utc())
        assert any(r.id == past.id and r.state == "expired" for r in expired)
        rows = await store.since(first.seq, 100)
        assert {r.id for r in rows} >= {first.id, advice.id, past.id}
        assert (await store.counts()).get("answered", 0) >= 1
    finally:
        await store.close()
