"""Ask a person - the main flow (extensions/ask-person/ask_person.py).

The shared vectors first (tests/vectors/ask-person-vectors.json): the ticket issued, verified, tampered,
expired and rotated; the message validated in its fixed order and the event built; the re-advice and the
resume requests the reply page and endpoint send; the outcomes the invoker gets back. Then the function
on the bus with channels that take, refuse and miss the event, and the reply page and endpoint against a
fake engine served in-process: the page shown and never resuming, the decisions forwarded with who
decided prefixed to each note, an expired ticket refused before the engine is called, the three auth
modes (none, the gateway's header, a signed access token against a JWKS the fake serves) and a channel
app's bearer, the host hook that mounts the routes before the catch-all, the boot that fails by name,
the health route and the inspect command.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
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
    assert VECTORS["contract"] == "ask-person" and VECTORS["version"] == 1
    for section in ("ticket_cases", "verify_cases", "message_cases", "readvice_cases", "reply_cases",
                    "outcome_cases"):
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
        ({ask.REPLY_AUTH: "basic"}, "ask.reply.auth must be one of none, proxy, oauth2"),
        ({ask.REPLY_AUTH: "proxy", ask.AUTH_HEADER: ""}, "ask.reply.auth.header names the header"),
        ({ask.REPLY_AUTH: "oauth2"}, "set ask.reply.auth.oauth2.issuer or ask.reply.auth.oauth2.jwks-url"),
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


@pytest_asyncio.fixture
async def channel() -> AsyncIterator[list[dict[str, Any]]]:
    """A channel route on the default registry that records every event it takes, and one that
    refuses with a 500; the bus is closed afterwards, as the other extension tests do."""
    taken: list[dict[str, Any]] = []

    async def take(_headers: dict[str, str], body: Any) -> Any:
        taken.append(body)
        return {"stored": True}

    async def refuse(_headers: dict[str, str], _body: Any) -> Any:
        raise ask.AppException(500, "the channel is down")

    default_registry.register("ask.test.channel", take, instances=1)
    default_registry.register("ask.test.refusing", refuse, instances=1)
    try:
        yield taken
    finally:
        await default_registry.bus.close()


async def test_person_ask_delivers_the_event_to_every_channel_best_effort(
    configure: Callable[[dict[str, Any]], Any], channel: list[dict[str, Any]]
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
    assert body["link"] == f"{REPLY_URL}/ask/{body['ticket']}"
    assert len(channel) == 1
    event = channel[0]
    assert event["ticket"] == body["ticket"] and event["link"] == body["link"]
    assert event["expires_at"] == body["expires_at"] and event["calls"] == CALLS
    assert event["kind"] == "approval" and event["graph_id"] == "order-assistant" and event["turn"] == 2
    payload = ask.verify_ticket(body["ticket"], [KEY.encode()], time.time())
    assert payload["g"] == "order-assistant" and payload["c"] == "order-1002-case" and payload["t"] == 2


async def test_an_advice_is_delivered_without_a_ticket(
    configure: Callable[[dict[str, Any]], Any], channel: list[dict[str, Any]]
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
    assert reply.body == {"delivered": [{"route": "ask.test.channel", "ok": True, "status": 200}]}
    assert "ticket" not in channel[0] and "link" not in channel[0] and channel[0]["details"] == advice["details"]


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


async def test_health_reports_the_setup(configure: Callable[[dict[str, Any]], Any]) -> None:
    configure({**BASE, ask.CHANNELS: ["a.route"], ask.GRAPHS: "g1=http://e/1"})
    info = await ask.ask_health({"type": "info"}, None)
    assert info == {
        "service": "person.ask", "href": "http://127.0.0.1", "enabled": True, "channels": ["a.route"],
        "graphs": ["g1"], "reply_url": REPLY_URL, "auth": "none",
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


def _ticket(graph_id: str = "order-assistant", cid: str = "order-1002-case", turn: int = 2,
            kind: str = "approval", expires_in: int = 3600, key: str = KEY) -> str:
    return ask.issue_ticket({"g": graph_id, "c": cid, "t": turn, "k": kind, "e": int(time.time()) + expires_in},
                            key.encode())


async def test_the_page_shows_the_wait_and_never_resumes(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant"})
    ticket = _ticket()
    async with aiohttp.ClientSession() as http, http.get(f"{host}/ask/{ticket}") as response:
        page = await response.text()
        assert response.status == 200 and response.content_type == "text/html"
    assert fake.requests == [{"graph": "order-assistant", "cid": "order-1002-case", "body": {"turn": 2}}]
    assert "issue_refund - waits for a decision" in page and "ask_owner - a question" in page
    assert "order_lookup - runs on resume" in page
    assert 'value="refund"' in page and 'value="replacement"' in page
    assert f'data-ticket="{ticket}"' in page and 'data-reply="/ask/reply"' in page
    assert "&quot;order_id&quot;: &quot;ORD-1002&quot;" in page, "the arguments are shown, escaped"


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
        assert fake.requests == [], "no ticket above reached the engine"
        async with http.get(f"{host}/ask/{_ticket(cid='expired-case')}") as response:
            assert response.status == 410 and "This wait has expired" in await response.text()
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


async def test_the_proxy_mode_takes_the_gateway_s_header_and_a_channel_app_its_bearer(
    configure: Callable[[dict[str, Any]], Any], engine: tuple[FakeEngine, str], host: str
) -> None:
    fake, engine_url = engine
    configure({**BASE, ask.GRAPHS: f"order-assistant={engine_url}/api/graph/order-assistant",
               ask.REPLY_AUTH: "proxy", ask.APP_TOKEN: APP_TOKEN})
    body = {"ticket": _ticket(), "decisions": [{"id": "toolu_01A", "approved": False}]}
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{host}/ask/{_ticket()}") as response:
            assert response.status == 401 and "Sign-in required" in await response.text()
        async with http.post(f"{host}/ask/reply", json=body) as response:
            assert response.status == 401 and "x-forwarded-user" in (await response.json())["message"]
        async with http.post(f"{host}/ask/reply", json=body, headers={"x-forwarded-user": "operator-17"}) as response:
            assert response.status == 200
        assert fake.requests[-1]["body"]["decisions"][0]["note"].startswith("[operator-17 at ")
        async with http.post(f"{host}/ask/reply", json={**body, "decided_by": "operator-9"},
                             headers={"authorization": f"Bearer {APP_TOKEN}"}) as response:
            assert response.status == 200
        assert fake.requests[-1]["body"]["decisions"][0]["note"].startswith("[operator-9 at ")
        async with http.post(f"{host}/ask/reply", json=body,
                             headers={"authorization": "Bearer wrong"}) as response:
            assert response.status == 401
        async with http.get(f"{host}/ask/{_ticket()}", headers={"x-forwarded-user": "operator-17"}) as response:
            assert response.status == 200


async def test_the_oauth2_mode_validates_an_access_token_against_the_issuer_s_keys(
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
               ask.OAUTH2_JWKS_URL: f"{engine_url}/jwks"})
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
        async with http.get(f"{host}/ask/{_ticket()}", headers={"authorization": f"Bearer {token}"}) as response:
            assert response.status == 200


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
