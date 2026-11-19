"""
Ask a person - the main flow of RFC-0014 (the engines' ``draft-design-specs/ask-a-person-channel.md``).
A graph's agent loop can wait for a person - a tool call marked ``write: true`` under the default
mode, or the ask row of the loop spec's section 7.3 - and the graph suspends durably on its state
store. This extension carries the wait to a person and brings the decision back; it knows no
channel, and it keeps no store: the engine's at-most-once resume is the authority.

- ``person.ask`` is a composable function the graph's hold path calls before it suspends. The
  message names the graph (``graph_id``, ``description``, ``node``), the transaction
  (``correlation_id``, ``turn``), the waiting ``calls`` exactly as the loop returned them, and the
  suspension's ``ttl``. The function issues a signed, stateless ticket, builds the resume link and
  sends the whole message as one event to every route in ``ask.channels`` - the agent-question
  app, a chat poster, a mail sender, a topic publisher - knowing nothing of what a route does with
  it; a channel that fails is logged by type and never fails the hold. A third kind, ``advice``, is
  a notification with no decision (a status update of a long run, an acknowledgement after a
  response): no ticket, no link.
- The resume link opens the wait on this process's reply page, ``GET /ask/{ticket}``: the page
  reads the current wait from the engine by the reference graph's re-advice (a request without
  decisions on the same entry and correlation id) and only a submit resumes. ``POST /ask/reply``
  with ``{ticket, decisions}`` - a person's browser, or a channel app on the person's behalf under
  its own bearer with ``decided_by`` - verifies the ticket, maps the graph id to the engine's entry
  through the ``ask.graphs`` allowlist (never to an address a message told it), prefixes who
  decided and when to each note, sends the resume (the same entry, the correlation id in
  ``x-correlation-id``, ``{turn, decisions}``) and returns the engine's outcome to the invoker.

Run:  mercury-serve extensions/ask-person/ask_person.py -Dask.ticket.key='${ASK_TICKET_KEY}'

The pure half - the ticket, the message's validation and the event, the resume request and the
outcome - is pinned by ``tests/vectors/ask-person-vectors.json``, shared byte for byte with the
Node.js twin (mercury-nodejs ``extensions/ask-person``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from aiohttp import web

try:
    import jwt
except ImportError:  # the oauth2 mode needs PyJWT - pip install 'mercury-composable[ask]'
    jwt = None

from mercury_composable import (
    AppConfig,
    AppException,
    Body,
    PostOffice,
    app_config,
    get_logger,
    iso_utc,
    platform,
    preload,
)

log = get_logger(__name__)

# --- the contract -----------------------------------------------------------------------------

ROUTE = "person.ask"
HEALTH_ROUTE = "ask.health"
INSTANCES = 10
KINDS = ("approval", "question", "advice")
WAIT_KINDS = ("approval", "question")
TICKET_VERSION = "v1"
SHOW_PATH = "/ask/{ticket}"
REPLY_PATH = "/ask/reply"
AUTH_MODES = ("none", "proxy", "oauth2")
# the signature algorithms an access token may carry (asymmetric only: the issuer's published keys)
JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
GRAPH_ID_HEADER = "x-graph-id"
CORRELATION_HEADER = "x-correlation-id"

# configuration keys
ENABLED = "ask.enabled"
CHANNELS = "ask.channels"
CHANNEL_TIMEOUT = "ask.channel.timeout"
CHANNEL_ENDPOINTS = "ask.channel.endpoints"
TICKET_KEY = "ask.ticket.key"
PREVIOUS_KEY = "ask.ticket.previous-key"
REPLY_URL = "ask.reply.url"
REPLY_AUTH = "ask.reply.auth"
AUTH_HEADER = "ask.reply.auth.header"
OAUTH2_ISSUER = "ask.reply.auth.oauth2.issuer"
OAUTH2_AUDIENCE = "ask.reply.auth.oauth2.audience"
OAUTH2_JWKS_URL = "ask.reply.auth.oauth2.jwks-url"
APP_TOKEN = "ask.reply.auth.token"
GRAPHS = "ask.graphs"
ENGINE_TIMEOUT = "ask.engine.timeout"

DURATION = re.compile(r"^(\d+)\s*(ms|s|m|h|d)?$")
# the base64url alphabet without padding - a ticket part outside it is malformed, never decoded leniently
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
DURATION_RULE = "a duration such as 30m, 12h or 2d (seconds, s, m, h, d)"

Decision = dict[str, Any]


class AskError(Exception):
    """A configuration the extension cannot start with - the boot fails by name."""


class TicketError(Exception):
    """A ticket that cannot be accepted: its status and the message the invoker sees."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ReplyError(Exception):
    """A reply that cannot be forwarded: its status and the message the invoker sees."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --- durations and times ----------------------------------------------------------------------


def duration_ms(value: Any) -> int | None:
    """Milliseconds from a whole number of seconds or a duration such as 500ms, 30m, 12h or
    2d; None when the value is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value * 1000 if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = DURATION.fullmatch(value.strip())
    if not match:
        return None
    unit = match.group(2) or "s"
    return int(match.group(1)) * UNIT_MS[unit]


def parse_iso(text: str) -> datetime | None:
    """An ISO-8601 time with an offset or ``Z``, as a UTC datetime; None when it is not one."""
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --- configuration ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    enabled: bool
    channels: tuple[str, ...]
    channel_timeout_ms: int
    channel_endpoints: dict[str, str]
    keys: tuple[bytes, ...]
    reply_url: str
    auth: str
    auth_header: str
    oauth2_issuer: str
    oauth2_audience: str
    oauth2_jwks_url: str
    app_token: str
    graphs: dict[str, str]
    engine_timeout_ms: int


def _text(config: AppConfig, key: str, default: str = "") -> str:
    """A text setting; an unset key (or one cleared to None) takes the default."""
    value = config.get(key)
    return default if value is None else str(value).strip()


def _flag(config: AppConfig, key: str, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def _names(value: Any) -> list[str]:
    """A list of names from a YAML list or a comma-separated text (a ``-D`` override)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _pairs(value: Any, key_name: str, value_name: str, setting: str) -> dict[str, str]:
    """A mapping from a YAML list of ``{key_name, value_name}`` maps or a comma-separated
    ``key=value`` text (a ``-D`` override)."""
    result: dict[str, str] = {}
    if value is None:
        return result
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, Mapping) or not item.get(key_name) or not item.get(value_name):
                raise AskError(f"{setting} entries need {key_name} and {value_name}")
            result[str(item[key_name]).strip()] = str(item[value_name]).strip()
        return result
    for part in str(value).split(","):
        if not part.strip():
            continue
        key, sep, target = part.partition("=")
        if not sep or not key.strip() or not target.strip():
            raise AskError(f"{setting} takes {key_name}={value_name} pairs")
        result[key.strip()] = target.strip()
    return result


def _timeout(config: AppConfig, key: str, default: str) -> int:
    value = config.get(key)
    ms = duration_ms(default if value is None else value)
    if ms is None or ms <= 0:
        raise AskError(f"{key} must be {DURATION_RULE} > 0")
    return ms


def load_settings(config: AppConfig) -> Settings:
    """The extension's settings from the application configuration, or an AskError naming the
    key that stops the boot."""
    enabled = _flag(config, ENABLED, True)
    current_key = _text(config, TICKET_KEY)
    # the current key signs; the previous one only verifies, so it never stands in for the current
    keys = tuple(key.encode("utf-8") for key in (current_key, _text(config, PREVIOUS_KEY)) if key)
    reply_url = _text(config, REPLY_URL).rstrip("/")
    auth = _text(config, REPLY_AUTH, "none").lower()
    auth_header = _text(config, AUTH_HEADER, "x-forwarded-user").lower()
    settings = Settings(
        enabled=enabled,
        channels=tuple(_names(config.get(CHANNELS))),
        channel_timeout_ms=_timeout(config, CHANNEL_TIMEOUT, "5s"),
        channel_endpoints=_pairs(config.get(CHANNEL_ENDPOINTS), "route", "url", CHANNEL_ENDPOINTS),
        keys=keys,
        reply_url=reply_url,
        auth=auth,
        auth_header=auth_header,
        oauth2_issuer=_text(config, OAUTH2_ISSUER).rstrip("/"),
        oauth2_audience=_text(config, OAUTH2_AUDIENCE),
        oauth2_jwks_url=_text(config, OAUTH2_JWKS_URL),
        app_token=_text(config, APP_TOKEN),
        graphs=_pairs(config.get(GRAPHS), "id", "entry", GRAPHS),
        engine_timeout_ms=_timeout(config, ENGINE_TIMEOUT, "60s"),
    )
    if not enabled:
        return settings
    if not current_key:
        raise AskError(f"{TICKET_KEY} is not set - the ticket's HMAC key, a ${{ASK_TICKET_KEY}} reference")
    if not reply_url.startswith(("http://", "https://")):
        raise AskError(f"{REPLY_URL} must be the public base of the resume link (http or https)")
    if auth not in AUTH_MODES:
        raise AskError(f"{REPLY_AUTH} must be one of {', '.join(AUTH_MODES)}")
    if auth == "proxy" and not auth_header:
        raise AskError(f"{AUTH_HEADER} names the header the gateway sets - it must not be empty")
    if auth == "oauth2":
        if jwt is None:
            raise AskError(
                f"{REPLY_AUTH} is oauth2 but PyJWT is not installed - pip install 'mercury-composable[ask]'"
            )
        if not settings.oauth2_issuer and not settings.oauth2_jwks_url:
            raise AskError(f"{REPLY_AUTH} is oauth2 - set {OAUTH2_ISSUER} or {OAUTH2_JWKS_URL}")
    for graph_id, entry in settings.graphs.items():
        if not entry.startswith(("http://", "https://")):
            raise AskError(f"{GRAPHS}: the entry of {graph_id} must be an http or https URL")
    return settings


_settings: Settings | None = None


def configure(config: AppConfig | None = None) -> Settings:
    """Load (or reload) the settings; the boot and the tests call it, the handlers read it."""
    global _settings
    _settings = load_settings(config or app_config())
    return _settings


def current_settings() -> Settings:
    return _settings if _settings is not None else configure()


# --- the ticket -------------------------------------------------------------------------------


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def canonical_json(value: Any) -> str:
    """The canonical JSON a ticket's payload is signed as: keys sorted, no spaces, Unicode kept -
    the same rule as the MCP binding's schema digest."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _mac(payload: bytes, key: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


def issue_ticket(payload: Mapping[str, Any], key: bytes) -> str:
    """``v1.<payload>.<mac>``: the canonical payload and its HMAC-SHA256, both base64url without
    padding."""
    encoded = canonical_json(dict(payload)).encode("utf-8")
    return f"{TICKET_VERSION}.{_b64u(encoded)}.{_b64u(_mac(encoded, key))}"


def _payload_is_valid(payload: Any) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"g", "c", "t", "k", "e"}:
        return False
    graph_id, cid, turn, kind, expiry = (payload[k] for k in ("g", "c", "t", "k", "e"))
    return (
        isinstance(graph_id, str)
        and bool(graph_id)
        and isinstance(cid, str)
        and bool(cid)
        and isinstance(turn, int)
        and not isinstance(turn, bool)
        and turn >= 0
        and kind in WAIT_KINDS
        and isinstance(expiry, int)
        and not isinstance(expiry, bool)
    )


def verify_ticket(ticket: str, keys: Sequence[bytes], now: float) -> dict[str, Any]:
    """The payload of a ticket this process issued, or a TicketError: 400 when the ticket is
    malformed or of another version, 403 when no key signs it, 410 when it has expired."""
    parts = ticket.split(".")
    if len(parts) != 3 or not all(parts):
        raise TicketError(400, "Ticket is malformed")
    version, encoded, signature = parts
    if version != TICKET_VERSION:
        raise TicketError(400, f"Ticket version {version} is not supported")
    if not BASE64URL.match(encoded) or not BASE64URL.match(signature):
        raise TicketError(400, "Ticket is malformed")
    try:
        payload_bytes = _b64u_decode(encoded)
        mac = _b64u_decode(signature)
    except (ValueError, UnicodeEncodeError) as exc:
        raise TicketError(400, "Ticket is malformed") from exc
    if not any(hmac.compare_digest(_mac(payload_bytes, key), mac) for key in keys):
        raise TicketError(403, "Ticket signature is invalid")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TicketError(400, "Ticket is malformed") from exc
    if not _payload_is_valid(payload):
        raise TicketError(400, "Ticket is malformed")
    if payload["e"] < now:
        raise TicketError(410, "Ticket expired")
    return payload


# --- the message and the event ----------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    kind: str
    graph_id: str
    description: str
    correlation_id: str
    summary: str
    requested_at: str
    node: str | None
    turn: int | None
    calls: list[dict[str, Any]] | None
    ttl: Any
    ttl_ms: int | None
    details: str | None


def _required_text(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if value is None:
        raise AppException(400, f"{name} is required")
    if not isinstance(value, str):
        raise AppException(400, f"{name} must be text")
    if not value.strip():
        raise AppException(400, f"{name} is required")
    return value


def _optional_text(body: Mapping[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AppException(400, f"{name} must be text")
    return value


def _turn(value: Any, required: bool) -> int | None:
    if value is None:
        if required:
            raise AppException(400, "turn is required for an approval or a question")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppException(400, "turn must be a whole number >= 0")
    return value


def validate_message(body: Any) -> Message:
    """The message a hold node sends, checked in a fixed order - the same in the Node.js twin -
    or an AppException(400) naming the rule."""
    if not isinstance(body, Mapping):
        raise AppException(400, "person.ask takes a map")
    kind = body.get("kind")
    if kind not in KINDS:
        raise AppException(400, "kind must be one of approval, question, advice")
    graph_id = _required_text(body, "graph_id")
    description = _required_text(body, "description")
    correlation_id = _required_text(body, "correlation_id")
    summary = _required_text(body, "summary")
    node = _optional_text(body, "node")
    requested_at_text = _optional_text(body, "requested_at")
    if requested_at_text is not None and parse_iso(requested_at_text) is None:
        raise AppException(400, "requested_at must be an ISO-8601 time")
    requested_at = requested_at_text or iso_utc()
    if kind == "advice":
        if "calls" in body or "ttl" in body:
            raise AppException(400, "an advice carries no calls and no ttl")
        return Message(
            kind=kind,
            graph_id=graph_id,
            description=description,
            correlation_id=correlation_id,
            summary=summary,
            requested_at=requested_at,
            node=node,
            turn=_turn(body.get("turn"), required=False),
            calls=None,
            ttl=None,
            ttl_ms=None,
            details=_optional_text(body, "details"),
        )
    turn = _turn(body.get("turn"), required=True)
    calls = body.get("calls")
    if (
        not isinstance(calls, list)
        or not calls
        or not all(isinstance(call, Mapping) and isinstance(call.get("id"), str) and call["id"]
                   for call in calls)
    ):
        raise AppException(400, "calls must be a non-empty list of the waiting calls, each with an id")
    if "ttl" not in body:
        raise AppException(400, "ttl is required for an approval or a question")
    ttl_ms = duration_ms(body["ttl"])
    if ttl_ms is None or ttl_ms <= 0:
        raise AppException(400, f"ttl must be {DURATION_RULE} > 0")
    return Message(
        kind=kind,
        graph_id=graph_id,
        description=description,
        correlation_id=correlation_id,
        summary=summary,
        requested_at=requested_at,
        node=node,
        turn=turn,
        calls=[dict(call) for call in calls],
        ttl=body["ttl"],
        ttl_ms=ttl_ms,
        details=None,
    )


def ticket_payload(message: Message, expires_at: datetime) -> dict[str, Any]:
    return {
        "g": message.graph_id,
        "c": message.correlation_id,
        "t": message.turn,
        "k": message.kind,
        "e": int(expires_at.timestamp()),
    }


def build_event(message: Message, key: bytes, reply_url: str) -> dict[str, Any]:
    """The message event the channels receive: the message, and for a wait the ticket, the
    resume link and the expiry (``requested_at`` plus ``ttl``)."""
    event: dict[str, Any] = {
        "kind": message.kind,
        "graph_id": message.graph_id,
        "description": message.description,
        "correlation_id": message.correlation_id,
    }
    if message.turn is not None:
        event["turn"] = message.turn
    if message.node is not None:
        event["node"] = message.node
    event["summary"] = message.summary
    if message.details is not None:
        event["details"] = message.details
    if message.calls is not None:
        event["calls"] = message.calls
    if message.ttl is not None:
        event["ttl"] = message.ttl
    event["requested_at"] = message.requested_at
    if message.kind in WAIT_KINDS and message.ttl_ms is not None:
        requested = parse_iso(message.requested_at) or datetime.now(timezone.utc)
        expires_at = requested + timedelta(milliseconds=message.ttl_ms)
        ticket = issue_ticket(ticket_payload(message, expires_at), key)
        event["expires_at"] = iso_utc(expires_at)
        event["ticket"] = ticket
        event["link"] = f"{reply_url.rstrip('/')}/ask/{ticket}"
    return event


# --- the resume and the outcome ---------------------------------------------------------------


@dataclass(frozen=True)
class EngineRequest:
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


def _entry(payload: Mapping[str, Any], graphs: Mapping[str, str]) -> str:
    entry = graphs.get(str(payload["g"]))
    if not entry:
        raise ReplyError(404, f"graph {payload['g']} is not configured for replies")
    return entry


def readvice_request(payload: Mapping[str, Any], graphs: Mapping[str, str]) -> EngineRequest:
    """The reference graph's re-advice: a request on the same entry and correlation id with the
    turn and no decisions - it answers the current wait and suspends again."""
    return EngineRequest(
        url=_entry(payload, graphs),
        headers={CORRELATION_HEADER: str(payload["c"])},
        body={"turn": payload["t"]},
    )


def validate_decisions(decisions: Any) -> list[Decision]:
    """The person's decisions, one per waiting call: ``{id, approved, note?}`` for a tool call,
    ``{id, answer}`` for a question - or a ReplyError(400) naming the rule."""
    if not isinstance(decisions, list) or not decisions:
        raise ReplyError(400, "decisions must be a non-empty list, one per waiting call")
    result: list[Decision] = []
    for decision in decisions:
        if not isinstance(decision, Mapping) or not isinstance(decision.get("id"), str) or not decision["id"]:
            raise ReplyError(400, "each decision needs the id of its call")
        has_approved = "approved" in decision
        has_answer = "answer" in decision
        if has_approved and has_answer:
            raise ReplyError(400, "each decision carries approved (with an optional note) or answer, not both")
        if not has_approved and not has_answer:
            raise ReplyError(400, "each decision carries approved (with an optional note) or answer")
        if has_approved and not isinstance(decision["approved"], bool):
            raise ReplyError(400, "approved must be true or false")
        if "note" in decision and decision["note"] is not None and not isinstance(decision["note"], str):
            raise ReplyError(400, "note must be text")
        if has_answer and "note" in decision:
            raise ReplyError(400, "a question's decision carries an answer and no note")
        result.append(dict(decision))
    return result


def stamp_decisions(decisions: Sequence[Decision], who: str, at: str) -> list[Decision]:
    """Who decided and when, prefixed to each tool call's note (a question's answer is the
    person's and travels unchanged), so the audit store carries it without a new field."""
    stamped: list[Decision] = []
    for decision in decisions:
        copy = dict(decision)
        if "approved" in copy:
            note = copy.get("note")
            prefix = f"[{who} at {at}]"
            copy["note"] = f"{prefix} {note}" if note else prefix
        stamped.append(copy)
    return stamped


def build_resume(
    payload: Mapping[str, Any],
    decisions: Any,
    who: str,
    at: str,
    graphs: Mapping[str, str],
) -> EngineRequest:
    """The resume the reply endpoint sends: the graph's entry from the allowlist, the correlation
    id, the turn and the stamped decisions."""
    checked = validate_decisions(decisions)
    return EngineRequest(
        url=_entry(payload, graphs),
        headers={CORRELATION_HEADER: str(payload["c"])},
        body={"turn": payload["t"], "decisions": stamp_decisions(checked, who, at)},
    )


def map_outcome(status: int | None, text: str | None, error: str | None) -> tuple[int, dict[str, Any]]:
    """What the invoker gets back: the engine's status and JSON body as they are, a 502 when the
    engine did not answer (``error`` names the failure's type) or answered outside the contract."""
    if error:
        return 502, {"type": "error", "message": f"the graph did not answer - {error}"}
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    if status is None or not isinstance(body, dict):
        return 502, {"type": "error", "message": "the graph answered outside the contract"}
    return status, body


# --- the function -----------------------------------------------------------------------------

_post_office: PostOffice | None = None
_http: aiohttp.ClientSession | None = None


def _po() -> PostOffice:
    global _post_office
    if _post_office is None:
        _post_office = PostOffice()
    return _post_office


def _session() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession()
    return _http


async def deliver(event: Mapping[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Send the event to every channel route, in parallel and best effort: the result says which
    routes took it. A failure is logged by type, never by content."""

    async def one(route: str) -> dict[str, Any]:
        endpoint = settings.channel_endpoints.get(route)
        try:
            reply = await _po().request(
                route, dict(event), timeout_ms=settings.channel_timeout_ms, endpoint=endpoint
            )
        except Exception as exc:  # noqa: BLE001 - a failure of any kind in a channel never fails the hold
            log.warning("Channel %s failed - %s", route, type(exc).__name__)
            return {"route": route, "ok": False, "error": type(exc).__name__}
        ok = reply.get_status() < 400
        if not ok:
            log.warning("Channel %s refused the message - status %d", route, reply.get_status())
        return {"route": route, "ok": ok, "status": reply.get_status()}

    if not settings.channels:
        return []
    return list(await asyncio.gather(*(one(route) for route in settings.channels)))


@preload(route=ROUTE, instances=INSTANCES)
async def person_ask(headers: dict[str, str], body: Body) -> Any:
    """Carry a wait, or an advice, to the configured channels; answer with the ticket, the link,
    the expiry and what was delivered (``delivered`` alone for an advice)."""
    settings = current_settings()
    if not settings.enabled:
        raise AppException(503, "person.ask is disabled - ask.enabled is false")
    message = validate_message(body)
    graph_header = headers.get(GRAPH_ID_HEADER)
    if graph_header and graph_header != message.graph_id:
        raise AppException(
            400, f"graph_id {message.graph_id} does not match the {GRAPH_ID_HEADER} header {graph_header}"
        )
    event = build_event(message, settings.keys[0], settings.reply_url)
    delivered = await deliver(event, settings)
    result: dict[str, Any] = {"delivered": delivered}
    if message.kind in WAIT_KINDS:
        result["ticket"] = event["ticket"]
        result["link"] = event["link"]
        result["expires_at"] = event["expires_at"]
    log.info(
        "person.ask %s for %s turn %s - %d of %d channel(s) took it",
        message.kind, message.graph_id, message.turn, sum(1 for d in delivered if d["ok"]),
        len(delivered),
    )
    return result


@preload(route=HEALTH_ROUTE, instances=5, private=True)
async def ask_health(headers: dict[str, str], _body: Body) -> Any:
    """Health check speaking the engines' interface contract (type=info / type=health)."""
    settings = current_settings()
    if headers.get("type") == "info":
        return {
            "service": ROUTE,
            "href": "http://127.0.0.1",
            "enabled": settings.enabled,
            "channels": list(settings.channels),
            "graphs": sorted(settings.graphs),
            "reply_url": settings.reply_url,
            "auth": settings.auth,
        }
    return "person.ask is running fine"


# --- identity ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    who: str
    via: str


def _bearer(request: web.Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class OAuth2Verifier:
    """An OAuth 2.0 access token (a JWT) validated against the issuer's published keys, fetched
    from the JWKS URL given or discovered from the issuer, and cached; a key id not in the cache
    refreshes it once."""

    def __init__(self, issuer: str, audience: str, jwks_url: str) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self._keys: list[dict[str, Any]] = []

    async def _fetch_json(self, url: str) -> Any:
        async with _session().get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                raise ReplyError(503, f"the issuer's keys are not reachable - {response.status}")
            return await response.json(content_type=None)

    async def _load_keys(self) -> list[dict[str, Any]]:
        url = self.jwks_url
        if not url:
            discovery = await self._fetch_json(f"{self.issuer}/.well-known/openid-configuration")
            url = str(discovery.get("jwks_uri", "")) if isinstance(discovery, dict) else ""
            if not url:
                raise ReplyError(503, "the issuer publishes no jwks_uri")
            self.jwks_url = url
        document = await self._fetch_json(url)
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list):
            raise ReplyError(503, "the issuer's JWKS document carries no keys")
        self._keys = [key for key in keys if isinstance(key, dict)]
        return self._keys

    async def verify(self, token: str) -> dict[str, Any]:
        if jwt is None:  # pragma: no cover - the boot refuses oauth2 without PyJWT
            raise ReplyError(503, "PyJWT is not installed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ReplyError(401, f"the access token was not accepted - {type(exc).__name__}") from exc
        kid = header.get("kid")
        alg = header.get("alg")
        keys = self._keys or await self._load_keys()
        chosen = next((key for key in keys if kid is None or key.get("kid") == kid), None)
        if chosen is None:
            keys = await self._load_keys()
            chosen = next((key for key in keys if kid is None or key.get("kid") == kid), None)
        if chosen is None:
            raise ReplyError(401, "the access token was not accepted - unknown key")
        algorithms = [alg] if alg in JWT_ALGORITHMS else list(JWT_ALGORITHMS)
        try:
            # a JWK that names no algorithm takes the token's, which the allowlist above has checked
            key = jwt.PyJWK(chosen, algorithm=None if chosen.get("alg") else algorithms[0]).key
            return jwt.decode(
                token,
                key=key,
                algorithms=algorithms,
                audience=self.audience or None,
                issuer=self.issuer or None,
                options={"require": ["exp"], "verify_aud": bool(self.audience)},
            )
        except jwt.PyJWTError as exc:
            raise ReplyError(401, f"the access token was not accepted - {type(exc).__name__}") from exc


_verifier: OAuth2Verifier | None = None


def _oauth2(settings: Settings) -> OAuth2Verifier:
    global _verifier
    if (
        _verifier is None
        or _verifier.issuer != settings.oauth2_issuer
        or _verifier.audience != settings.oauth2_audience
        or (settings.oauth2_jwks_url and _verifier.jwks_url != settings.oauth2_jwks_url)
    ):
        _verifier = OAuth2Verifier(settings.oauth2_issuer, settings.oauth2_audience, settings.oauth2_jwks_url)
    return _verifier


async def identify(
    request: web.Request, settings: Settings, decided_by: str | None, allow_app: bool
) -> Identity:
    """Who is answering: a channel app under its bearer (``decided_by`` is the person it acts
    for), or a person by the configured mode - nobody in development, the gateway's trusted
    header, or a validated access token."""
    bearer = _bearer(request)
    if allow_app and settings.app_token and bearer and hmac.compare_digest(bearer, settings.app_token):
        return Identity(decided_by or "channel", "app")
    if settings.auth == "none":
        return Identity(decided_by or "anonymous", "none")
    if settings.auth == "proxy":
        who = request.headers.get(settings.auth_header, "").strip()
        if not who:
            raise ReplyError(401, f"sign-in required - the {settings.auth_header} header names the person")
        return Identity(who, "proxy")
    if not bearer:
        raise ReplyError(401, "sign-in required - a bearer access token")
    claims = await _oauth2(settings).verify(bearer)
    who = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not who:
        raise ReplyError(401, "the access token names nobody")
    return Identity(str(who), "oauth2")


# --- the reply page and endpoint --------------------------------------------------------------

STYLE = (
    "body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:2rem;color:#222;background:#fafafa}"
    "h1{font-size:1.3rem}.meta{color:#666;font-size:.85rem}fieldset{background:#fff;border:1px solid #ddd;"
    "border-radius:8px;padding:1rem;margin:1rem 0;max-width:800px}legend{font-weight:600}"
    "pre{background:#f3f3f3;border-radius:6px;padding:.6rem .8rem;overflow:auto;font-size:.85rem}"
    "textarea,input[type=text]{width:100%;box-sizing:border-box;margin:.3rem 0}"
    "button{font-size:1rem;padding:.5rem 1.2rem}#outcome{margin-top:1rem}"
)

SCRIPT = """
const form = document.getElementById('decisions');
form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const decisions = [];
  for (const set of form.querySelectorAll('fieldset[data-id]')) {
    const id = set.dataset.id;
    if (set.dataset.kind === 'ask') {
      const picked = set.querySelector('input[type=radio]:checked');
      const text = set.querySelector('textarea').value.trim();
      decisions.push({id, answer: picked && picked.value !== '__other__' ? picked.value : text});
    } else {
      const choice = set.querySelector('input[type=radio]:checked');
      if (!choice) { alert('Approve or refuse every waiting call'); return; }
      const note = set.querySelector('input[type=text]').value.trim();
      const decision = {id, approved: choice.value === 'yes'};
      if (note) decision.note = note;
      decisions.push(decision);
    }
  }
  const out = document.getElementById('outcome');
  out.textContent = 'Sending...';
  const response = await fetch(form.dataset.reply, {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({ticket: form.dataset.ticket, decisions})});
  const body = await response.text();
  out.innerHTML = '<p class="meta">status ' + response.status + '</p><pre></pre>';
  out.querySelector('pre').textContent = body;
  if (response.ok) form.querySelector('button').disabled = true;
});
"""


def _page(title: str, body: str, script: str = "") -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f"<h1>{html.escape(title)}</h1>\n{body}\n"
        + (f"<script>{script}</script>\n" if script else "")
        + "</body>\n</html>\n"
    )


def _notice(status: int, title: str, text: str) -> web.Response:
    body = f"<p>{html.escape(text)}</p>"
    return web.Response(status=status, text=_page(title, body), content_type="text/html")


def _render_call(call: Mapping[str, Any]) -> str:
    """One waiting call as a fieldset: an answer for a question, approve or refuse with a note
    for a tool call, and a note alone for a call that runs on resume."""
    call_id = str(call.get("id", ""))
    name = html.escape(str(call.get("name", call_id)))
    inputs = call.get("input")
    shown = html.escape(json.dumps(inputs, indent=2, ensure_ascii=False)) if inputs is not None else ""
    if call.get("ask"):
        options = inputs.get("options") if isinstance(inputs, Mapping) else None
        radios = ""
        if isinstance(options, list) and options:
            for index, option in enumerate(options):
                text = html.escape(str(option))
                radios += (
                    f"<label><input type=\"radio\" name=\"a-{html.escape(call_id)}\" value=\"{text}\""
                    f"{' checked' if index == 0 else ''}> {text}</label><br>\n"
                )
            radios += (
                f"<label><input type=\"radio\" name=\"a-{html.escape(call_id)}\" value=\"__other__\">"
                " another answer:</label>\n"
            )
        return (
            f"<fieldset data-id=\"{html.escape(call_id)}\" data-kind=\"ask\">\n<legend>{name} - a question</legend>\n"
            f"<pre>{shown}</pre>\n{radios}<textarea rows=\"3\" placeholder=\"Your answer\"></textarea>\n</fieldset>\n"
        )
    if not call.get("wait", True):
        return (
            f"<fieldset>\n<legend>{name} - runs on resume</legend>\n<pre>{shown}</pre>\n</fieldset>\n"
        )
    route = html.escape(str(call.get("route", "")))
    return (
        f"<fieldset data-id=\"{html.escape(call_id)}\" data-kind=\"write\">\n"
        f"<legend>{name} - waits for a decision</legend>\n<p class=\"meta\">{route}</p>\n<pre>{shown}</pre>\n"
        f"<label><input type=\"radio\" name=\"d-{html.escape(call_id)}\" value=\"yes\"> approve</label> \n"
        f"<label><input type=\"radio\" name=\"d-{html.escape(call_id)}\" value=\"no\"> refuse</label>\n"
        "<input type=\"text\" placeholder=\"A note for the model (optional)\">\n</fieldset>\n"
    )


def render_wait(ticket: str, payload: Mapping[str, Any], advice: Mapping[str, Any]) -> str:
    """The page for a wait as the engine returned it on re-advice: the calls and the form."""
    calls = advice.get("calls")
    rendered = "".join(_render_call(call) for call in calls if isinstance(call, Mapping)) \
        if isinstance(calls, list) else "<p>The graph reported no waiting call.</p>"
    message = advice.get("message")
    intro = f"<p>{html.escape(str(message))}</p>\n" if message else ""
    meta = (
        f"<p class=\"meta\">graph {html.escape(str(payload['g']))} &middot; "
        f"correlation id {html.escape(str(payload['c']))} &middot; turn {html.escape(str(payload['t']))}</p>\n"
    )
    form = (
        f"<form id=\"decisions\" data-ticket=\"{html.escape(ticket)}\" data-reply=\"{REPLY_PATH}\">\n"
        f"{rendered}<button type=\"submit\">Send the decision</button>\n</form>\n<div id=\"outcome\"></div>"
    )
    return _page(f"{payload['k'].capitalize()} - {payload['g']}", meta + intro + form, SCRIPT)


async def _engine(request: EngineRequest, timeout_ms: int) -> tuple[int, dict[str, Any]]:
    """Send a request to the graph's entry and map what comes back."""
    headers = {**request.headers, "content-type": "application/json", "accept": "application/json"}
    try:
        async with _session().post(
            request.url,
            json=request.body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000 + 1),
        ) as response:
            text = await response.text()
            return map_outcome(response.status, text, None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("The graph at %s did not answer - %s", request.url, type(exc).__name__)
        return map_outcome(None, None, type(exc).__name__)


async def show_wait(request: web.Request) -> web.Response:
    """``GET /ask/{ticket}``: show the current wait and the decision form - never resume."""
    settings = current_settings()
    ticket = request.match_info["ticket"]
    try:
        payload = verify_ticket(ticket, settings.keys, time.time())
    except TicketError as exc:
        return _notice(exc.status, "This link cannot be used" if exc.status != 410 else "This wait has expired",
                       exc.message)
    try:
        await identify(request, settings, None, allow_app=False)
    except ReplyError as exc:
        return _notice(exc.status, "Sign-in required", exc.message)
    try:
        readvice = readvice_request(payload, settings.graphs)
    except ReplyError as exc:
        return _notice(exc.status, "This graph is not configured", exc.message)
    status, advice = await _engine(readvice, settings.engine_timeout_ms)
    if status == 410 or advice.get("type") == "nothing-waiting":
        return _notice(410, "This wait has expired", "Nothing is waiting for this decision any more - it was "
                       "decided already, or the suspension expired.")
    if status >= 400:
        return _notice(status, "The graph refused", json.dumps(advice, ensure_ascii=False))
    if advice.get("type") != "approval":
        return _notice(200, "The graph has moved on", json.dumps(advice, ensure_ascii=False))
    return web.Response(text=render_wait(ticket, payload, advice), content_type="text/html")


async def post_reply(request: web.Request) -> web.Response:
    """``POST /ask/reply``: verify the ticket, identify who decides, forward the resume and
    return the engine's outcome."""
    settings = current_settings()
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return web.json_response(
            {"type": "error", "message": "the reply must be a JSON map with ticket and decisions"}, status=400
        )
    ticket = body.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        return web.json_response({"type": "error", "message": "ticket is required"}, status=400)
    try:
        payload = verify_ticket(ticket, settings.keys, time.time())
    except TicketError as exc:
        kind = "expired" if exc.status == 410 else "error"
        return web.json_response({"type": kind, "message": exc.message}, status=exc.status)
    decided_by = body.get("decided_by")
    if decided_by is not None and not isinstance(decided_by, str):
        return web.json_response({"type": "error", "message": "decided_by must be text"}, status=400)
    try:
        identity = await identify(request, settings, decided_by or None, allow_app=True)
        resume = build_resume(payload, body.get("decisions"), identity.who, iso_utc(), settings.graphs)
    except ReplyError as exc:
        return web.json_response({"type": "error", "message": exc.message}, status=exc.status)
    status, outcome = await _engine(resume, settings.engine_timeout_ms)
    log.info(
        "reply for %s turn %s by %s via %s - the graph answered %d %s",
        payload["g"], payload["t"], identity.who, identity.via, status, outcome.get("type"),
    )
    return web.json_response(outcome, status=status)


def mount(app: web.Application) -> None:
    """The reply page and endpoint on the host (``Platform.add_routes``)."""
    app.router.add_post(REPLY_PATH, post_reply)
    app.router.add_get(SHOW_PATH, show_wait)


# --- boot -------------------------------------------------------------------------------------


async def boot() -> None:
    """Load the settings inside the serving loop, before the first request; a configuration the
    extension cannot start with stops the application by name."""
    settings = configure()
    if not settings.enabled:
        log.info("person.ask is disabled - %s is false", ENABLED)
        return
    if not settings.channels:
        log.warning("No channel configured (%s) - the link is returned to the graph only", CHANNELS)
    if not settings.graphs:
        log.warning("No graph configured (%s) - a reply cannot be forwarded", GRAPHS)
    log.info(
        "person.ask ready - %d channel(s) %s, %d graph(s), reply page %s/ask/{ticket}, auth %s",
        len(settings.channels), list(settings.channels), len(settings.graphs), settings.reply_url,
        settings.auth,
    )


async def shutdown() -> None:
    global _http
    if _http is not None and not _http.closed:
        await _http.close()
    _http = None
    if _post_office is not None:
        await _post_office.close()


def inspect(words: list[str]) -> int:
    """``inspect <ticket>``: print a ticket's payload, or why it is refused, under the current
    configuration - a developer's check, never a resume."""
    if len(words) != 1:
        print("usage: python ask_person.py inspect <ticket>", file=sys.stderr)
        return 1
    try:
        settings = configure()
    except AskError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 1
    try:
        payload = verify_ticket(words[0], settings.keys, time.time())
    except TicketError as exc:
        print(f"{exc.status} {exc.message}")
        return 1
    expires = datetime.fromtimestamp(payload["e"], tz=timezone.utc)
    print(json.dumps({**payload, "expires_at": iso_utc(expires)}, indent=2))
    return 0


def _attach() -> None:
    platform.on_startup(boot)
    platform.on_cleanup(shutdown)
    platform.add_routes(mount)


def _main(argv: list[str]) -> int:
    # -Dkey=value arguments belong to the configuration, which reads them from sys.argv itself
    words = [a for a in argv if not a.startswith("-D")]
    if words and words[0] == "inspect":
        return inspect(words[1:])
    _attach()
    platform.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
else:
    # loaded by mercury-serve: the boot runs inside the serving loop, before the first request
    _attach()
