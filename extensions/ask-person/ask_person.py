"""
Ask a person - the main flow of RFC-0014 (the engines' ``draft-design-specs/ask-a-person-channel.md``).
A graph's agent loop can wait for a person - a tool call marked ``write: true`` under the default
mode, or the ask row of the loop spec's section 7.3 - and the graph suspends durably on its state
store. This extension carries the wait to a person and brings the decision back; it knows no
channel, and it holds no state of the wait: the engine's at-most-once resume is the authority.

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
  decisions on the same entry and correlation id, ``GET /ask/{ticket}/wait``) and only a submit
  resumes. ``POST /ask/reply`` with ``{ticket, decisions}`` - a person's browser, or a channel app
  on the person's behalf under its own bearer with ``decided_by`` - verifies the ticket, maps the
  graph id to the engine's entry through the ``ask.graphs`` allowlist (never to an address a
  message told it), prefixes who decided and when to each note, sends the resume (the same entry,
  the correlation id in ``x-correlation-id``, ``{turn, decisions}``) and returns the engine's
  outcome to the invoker. The page and the endpoints are entries of ``resources/rest.yaml`` - the
  engines' REST automation, served by the pack's host - behind private functions of this process
  (``ask.page``, ``ask.wait``, ``ask.reply``, ``ask.config``); who is asking is resolved by the
  entries' authentication functions (``ask.identity`` for a person, ``ask.reply.identity`` for a
  person or a channel app) and read from the request's session map.
- The question record (round 4 of the paper, section 21): the main flow records and the channels
  deliver. Every wait and every advice ``person.ask`` sends, whatever channel delivers it, and every
  answer the reply endpoint brings back - who decided, when, the engine's outcome - is written to
  the question record, part of the transcript audit log, by ``question.audit``: a private
  composable function of this process behind local events, off the request path, whose writes
  are order-independent (a wait upserts on its key unless an older message arrives late, an advice
  appends, the first accepted answer stands), so it may run several instances. The store is
  PostgreSQL (``ask.audit: postgres``, ``schema/postgres.sql``) or memory for the tests; the
  contract is published so a field implements another database. A failure is logged by type and
  never fails a hold or a reply.

Run:  mercury-serve extensions/ask-person/ask_person.py -Dask.ticket.key='${ASK_TICKET_KEY}'

The pure half - the ticket, the message's validation and the event, the resume request and the
outcome, the row a message becomes, the state an answer leaves and the record's transitions - is
pinned by ``tests/vectors/ask-person-vectors.json``, shared byte for byte with the Node.js twin
(mercury-nodejs ``extensions/ask-person``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import importlib.util
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import aiohttp

try:
    import jwt
except ImportError:  # the oauth2 mode needs PyJWT - pip install 'mercury-composable[ask]'
    jwt = None

from mercury_composable import (
    AppConfig,
    AppException,
    AsyncHttpRequest,
    Body,
    EventEnvelope,
    PostOffice,
    app_config,
    default_registry,
    get_logger,
    iso_utc,
    platform,
    preload,
)

log = get_logger(__name__)

# --- the contract -----------------------------------------------------------------------------

ROUTE = "person.ask"
HEALTH_ROUTE = "ask.health"
AUDIT_ROUTE = "question.audit"
INSTANCES = 10
KINDS = ("approval", "question", "advice")
WAIT_KINDS = ("approval", "question")
# a wait's states; an advice has none (None): whether a person read it is the device's business
STATES = ("open", "answered", "expired")
TICKET_VERSION = "v1"
# the reply page and endpoint: the URLs of resources/rest.yaml and the private functions behind them
SHOW_PATH = "/ask/{ticket}"
WAIT_PATH = "/ask/{ticket}/wait"
REPLY_PATH = "/ask/reply"
CONFIG_PATH = "/ask/config"
PAGE_ROUTE = "ask.page"
WAIT_ROUTE = "ask.wait"
REPLY_ROUTE = "ask.reply"
CONFIG_ROUTE = "ask.config"
# the entries' authentication functions and the session keys they leave on the request
IDENTITY_ROUTE = "ask.identity"
REPLY_IDENTITY_ROUTE = "ask.reply.identity"
SESSION_WHO = "who"
SESSION_VIA = "via"
AUTH_MODES = ("none", "oauth2")
# the signature algorithms an access token may carry (asymmetric only: the issuer's published keys)
JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
GRAPH_ID_HEADER = "x-graph-id"
CORRELATION_HEADER = "x-correlation-id"
AUDITS = ("memory", "postgres")

# configuration keys
ENABLED = "ask.enabled"
CHANNELS = "ask.channels"
CHANNEL_TIMEOUT = "ask.channel.timeout"
CHANNEL_ENDPOINTS = "ask.channel.endpoints"
TICKET_KEY = "ask.ticket.key"
PREVIOUS_KEY = "ask.ticket.previous-key"
REPLY_URL = "ask.reply.url"
REPLY_AUTH = "ask.reply.auth"
OAUTH2_ISSUER = "ask.reply.auth.oauth2.issuer"
OAUTH2_AUDIENCE = "ask.reply.auth.oauth2.audience"
OAUTH2_JWKS_URL = "ask.reply.auth.oauth2.jwks-url"
OAUTH2_CLIENT_ID = "ask.reply.auth.oauth2.client-id"
OAUTH2_SCOPES = "ask.reply.auth.oauth2.scopes"
APP_TOKEN = "ask.reply.auth.token"
GRAPHS = "ask.graphs"
ENGINE_TIMEOUT = "ask.engine.timeout"
AUDIT = "ask.audit"
AUDIT_POSTGRES_PREFIX = "ask.audit.postgres."
AUDIT_SWEEP = "ask.audit.sweep"
AUDIT_INSTANCES = "ask.audit.instances"
AUDIT_TIMEOUT = "ask.audit.timeout"

MISSING_POSTGRES = "ask.audit is postgres but psycopg is not installed - pip install 'mercury-composable[postgres]'"
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
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
    oauth2_issuer: str
    oauth2_audience: str
    oauth2_jwks_url: str
    oauth2_client_id: str
    oauth2_scopes: str
    app_token: str
    graphs: dict[str, str]
    engine_timeout_ms: int
    audit: str
    audit_postgres: dict[str, Any]
    audit_postgres_timeout_s: float
    audit_sweep_ms: int
    audit_instances: int
    audit_timeout_ms: int


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


def _whole(config: AppConfig, key: str, default: int, minimum: int) -> int:
    """A whole number >= ``minimum`` (a count of instances, a number of milliseconds)."""
    value = config.get(key)
    text = str(default if value is None else value).strip()
    if not text.isdigit() or int(text) < minimum:
        raise AskError(f"{key} must be a whole number >= {minimum}")
    return int(text)


def _audit_postgres(config: AppConfig, timeout_ms: int) -> dict[str, Any]:
    """The connection keys of the question record's PostgreSQL store - the transcript audit
    store's keys under the ``ask.audit.postgres.`` prefix; the password a reference, never logged."""
    port_text = _text(config, AUDIT_POSTGRES_PREFIX + "port", "5432")
    if not port_text.isdigit() or int(port_text) <= 0:
        raise AskError(f"{AUDIT_POSTGRES_PREFIX}port must be a port number")
    ssl = _text(config, AUDIT_POSTGRES_PREFIX + "ssl", "true").lower() != "false"
    settings: dict[str, Any] = {
        "host": _text(config, AUDIT_POSTGRES_PREFIX + "host", "127.0.0.1"),
        "port": int(port_text),
        "dbname": _text(config, AUDIT_POSTGRES_PREFIX + "database", "mercury"),
        "user": _text(config, AUDIT_POSTGRES_PREFIX + "username") or None,
        "password": _text(config, AUDIT_POSTGRES_PREFIX + "password") or None,
        "sslmode": "require" if ssl else "disable",
        "connect_timeout": max(1, round(timeout_ms / 1000)),
        # the server-side bound on a statement, as the transcript audit store sets it
        "options": f"-c statement_timeout={timeout_ms}",
    }
    return {key: value for key, value in settings.items() if value is not None}


def load_settings(config: AppConfig) -> Settings:
    """The extension's settings from the application configuration, or an AskError naming the
    key that stops the boot."""
    enabled = _flag(config, ENABLED, True)
    current_key = _text(config, TICKET_KEY)
    # the current key signs; the previous one only verifies, so it never stands in for the current
    keys = tuple(key.encode("utf-8") for key in (current_key, _text(config, PREVIOUS_KEY)) if key)
    reply_url = _text(config, REPLY_URL).rstrip("/")
    auth = _text(config, REPLY_AUTH, "none").lower()
    audit = _text(config, AUDIT, "memory").lower()
    postgres_timeout_ms = _whole(config, AUDIT_POSTGRES_PREFIX + "timeout.ms", 5000, 1)
    settings = Settings(
        enabled=enabled,
        channels=tuple(_names(config.get(CHANNELS))),
        channel_timeout_ms=_timeout(config, CHANNEL_TIMEOUT, "5s"),
        channel_endpoints=_pairs(config.get(CHANNEL_ENDPOINTS), "route", "url", CHANNEL_ENDPOINTS),
        keys=keys,
        reply_url=reply_url,
        auth=auth,
        oauth2_issuer=_text(config, OAUTH2_ISSUER).rstrip("/"),
        oauth2_audience=_text(config, OAUTH2_AUDIENCE),
        oauth2_jwks_url=_text(config, OAUTH2_JWKS_URL),
        oauth2_client_id=_text(config, OAUTH2_CLIENT_ID),
        oauth2_scopes=_text(config, OAUTH2_SCOPES, "openid profile email"),
        app_token=_text(config, APP_TOKEN),
        graphs=_pairs(config.get(GRAPHS), "id", "entry", GRAPHS),
        engine_timeout_ms=_timeout(config, ENGINE_TIMEOUT, "60s"),
        audit=audit,
        audit_postgres=_audit_postgres(config, postgres_timeout_ms),
        audit_postgres_timeout_s=postgres_timeout_ms / 1000,
        audit_sweep_ms=_timeout(config, AUDIT_SWEEP, "30s"),
        audit_instances=_whole(config, AUDIT_INSTANCES, 1, 1),
        audit_timeout_ms=_timeout(config, AUDIT_TIMEOUT, "5s"),
    )
    if not enabled:
        return settings
    if not current_key:
        raise AskError(f"{TICKET_KEY} is not set - the ticket's HMAC key, a ${{ASK_TICKET_KEY}} reference")
    if not reply_url.startswith(("http://", "https://")):
        raise AskError(f"{REPLY_URL} must be the public base of the resume link (http or https)")
    if auth not in AUTH_MODES:
        raise AskError(f"{REPLY_AUTH} must be one of {', '.join(AUTH_MODES)}")
    if auth == "oauth2":
        if jwt is None:
            raise AskError(
                f"{REPLY_AUTH} is oauth2 but PyJWT is not installed - pip install 'mercury-composable[ask]'"
            )
        if not settings.oauth2_issuer and not settings.oauth2_jwks_url:
            raise AskError(f"{REPLY_AUTH} is oauth2 - set {OAUTH2_ISSUER} or {OAUTH2_JWKS_URL}")
        if not settings.oauth2_issuer or not settings.oauth2_client_id:
            log.warning(
                "%s is oauth2 without %s and %s - the reply page cannot sign a person in; a channel app's "
                "bearer and a token obtained elsewhere still work", REPLY_AUTH, OAUTH2_ISSUER, OAUTH2_CLIENT_ID,
            )
    if audit not in AUDITS:
        raise AskError(f"{AUDIT} must be one of {', '.join(AUDITS)}")
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
        requested = parse_iso(message.requested_at) or datetime.now(UTC)
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


# --- the question record (round 4) ------------------------------------------------------------
#
# The main flow records and the channels deliver: every wait and advice person.ask sends and every
# answer the reply endpoint brings back is written here, part of the transcript audit log, by the
# question.audit function behind local events. The row is what the agent-question app's feed
# carries to a device; the shape is pinned by the shared vectors.


@dataclass(frozen=True)
class Row:
    id: int
    seq: int
    kind: str
    graph_id: str
    correlation_id: str
    turn: int | None
    ticket: str | None
    link: str | None
    description: str
    summary: str
    details: str | None
    message: dict[str, Any]
    state: str | None
    requested_at: str
    expires_at: str | None
    updated_at: str
    answered_at: str | None = None
    answered_by: str | None = None
    decisions: list[dict[str, Any]] | None = None
    outcome: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """The row as a feed carries it."""
        return {
            "id": self.id, "seq": self.seq, "kind": self.kind, "graph_id": self.graph_id,
            "correlation_id": self.correlation_id, "turn": self.turn, "ticket": self.ticket, "link": self.link,
            "description": self.description, "summary": self.summary, "details": self.details,
            "calls": self.message.get("calls"), "state": self.state, "requested_at": self.requested_at,
            "expires_at": self.expires_at, "updated_at": self.updated_at, "answered_at": self.answered_at,
            "answered_by": self.answered_by, "decisions": self.decisions, "outcome": self.outcome,
        }  # fmt: skip


def wait_key(event: Mapping[str, Any]) -> tuple[str, str, int] | None:
    """The key a wait upserts on; an advice has none and appends."""
    if event.get("kind") not in WAIT_KINDS:
        return None
    return str(event["graph_id"]), str(event["correlation_id"]), int(event["turn"])


def row_fields(event: Mapping[str, Any], now: str) -> dict[str, Any]:
    """What a row takes from a message event: a wait starts open with its ticket, link and
    expiry; an advice has no ticket, no link, no expiry and no state."""
    wait = event["kind"] in WAIT_KINDS
    return {
        "kind": event["kind"],
        "graph_id": event["graph_id"],
        "correlation_id": event["correlation_id"],
        "turn": event.get("turn"),
        "ticket": event.get("ticket") if wait else None,
        "link": event.get("link") if wait else None,
        "description": event["description"],
        "summary": event["summary"],
        "details": event.get("details"),
        "message": dict(event),
        "state": "open" if wait else None,
        "requested_at": event.get("requested_at") or now,
        "expires_at": event.get("expires_at") if wait else None,
        "updated_at": now,
        "answered_at": None,
        "answered_by": None,
        "decisions": None,
        "outcome": None,
    }


def answer_state(status: int | None, body: Any) -> str:
    """The state an answer's outcome leaves: the engine accepted it (answered), nothing was
    waiting (expired), or the decision was refused or the engine could not be reached (open)."""
    kind = body.get("type") if isinstance(body, Mapping) else None
    if status == 410 or kind == "nothing-waiting":
        return "expired"
    if status is not None and 200 <= status < 300 and not (isinstance(body, Mapping) and body.get("error")):
        return "answered"
    return "open"


class QuestionStore(Protocol):
    """The question record's contract - published so a field implements it on another database.
    PostgreSQL is the reference; the memory store carries the same semantics for the tests."""

    name: str

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def record(self, event: Mapping[str, Any], now: str) -> Row:
        """A wait upserted on (graph_id, correlation_id, turn) - applied unless the row holds a
        newer message, so a late re-advice never overwrites a newer one - or an advice appended."""
        ...
    async def answer(self, key: tuple[str, str, int], decisions: list[dict[str, Any]], who: str,
                     state: str, outcome: dict[str, Any], now: str) -> Row | None:
        """The answer on the wait with the key, applied while the row is open or when the engine
        accepted the decision (answered always wins: the first accepted decision stands); the
        row as it stands otherwise, None when no wait was recorded."""
        ...
    async def expire(self, now: str) -> list[Row]: ...
    async def since(self, seq: int, limit: int) -> list[Row]: ...
    async def counts(self) -> dict[str, int]: ...


class MemoryQuestionStore:
    """The reference semantics in one process: ids and the change sequence assigned here, every
    write bumping the sequence a feed reads from."""

    name = "memory"

    def __init__(self) -> None:
        self._rows: dict[int, Row] = {}
        self._keys: dict[tuple[str, str, int], int] = {}
        self._next_id = 0
        self._seq = 0

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _bump(self) -> int:
        self._seq += 1
        return self._seq

    async def record(self, event: Mapping[str, Any], now: str) -> Row:
        fields = row_fields(event, now)
        key = wait_key(event)
        existing = self._rows.get(self._keys[key]) if key is not None and key in self._keys else None
        if existing is not None:
            if fields["requested_at"] < existing.requested_at:
                return existing  # an older message landing late never overwrites a newer row
            row = replace(existing, seq=self._bump(), **fields)
        else:
            self._next_id += 1
            row = Row(id=self._next_id, seq=self._bump(), **fields)
            if key is not None:
                self._keys[key] = row.id
        self._rows[row.id] = row
        return row

    async def answer(self, key: tuple[str, str, int], decisions: list[dict[str, Any]], who: str,
                     state: str, outcome: dict[str, Any], now: str) -> Row | None:
        row_id = self._keys.get(key)
        row = self._rows.get(row_id) if row_id is not None else None
        if row is None:
            return None
        if row.state != "open" and state != "answered":
            return row  # decided first by another device: a later nothing-waiting never overwrites it
        row = replace(row, seq=self._bump(), state=state, decisions=decisions, outcome=outcome,
                      answered_by=who, answered_at=now, updated_at=now)
        self._rows[row.id] = row
        return row

    async def expire(self, now: str) -> list[Row]:
        changed: list[Row] = []
        for row in list(self._rows.values()):
            if row.state == "open" and row.kind in WAIT_KINDS and row.expires_at and row.expires_at < now:
                row = replace(row, seq=self._bump(), state="expired", updated_at=now)
                self._rows[row.id] = row
                changed.append(row)
        return changed

    async def since(self, seq: int, limit: int) -> list[Row]:
        rows = sorted((r for r in self._rows.values() if r.seq > seq), key=lambda r: r.seq)
        return rows[:limit]

    async def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in self._rows.values():
            label = row.state or row.kind
            result[label] = result.get(label, 0) + 1
        return result


# the statements the reference store runs, with psycopg's placeholders. JSON travels as compact text
# cast to jsonb and stamps as text cast to timestamptz, so the Node.js twin runs the same statements.
COLUMNS = ("id, seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, message, link, "
           "state, requested_at, expires_at, updated_at, answered_at, answered_by, decisions, outcome")
SQL_RECORD_WAIT = (
    "INSERT INTO question (seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, "
    "message, link, state, requested_at, expires_at, updated_at) "
    "VALUES (nextval('question_seq'), %s, %s, %s, %s, %s, %s, %s, NULL, %s::jsonb, %s, 'open', "
    "%s::timestamptz, %s::timestamptz, %s::timestamptz) "
    "ON CONFLICT (graph_id, correlation_id, turn) WHERE kind <> 'advice' DO UPDATE SET "
    "seq = nextval('question_seq'), ticket = EXCLUDED.ticket, description = EXCLUDED.description, "
    "summary = EXCLUDED.summary, details = NULL, message = EXCLUDED.message, link = EXCLUDED.link, state = 'open', "
    "requested_at = EXCLUDED.requested_at, expires_at = EXCLUDED.expires_at, updated_at = EXCLUDED.updated_at, "
    "answered_at = NULL, answered_by = NULL, decisions = NULL, outcome = NULL "
    f"WHERE EXCLUDED.requested_at >= question.requested_at RETURNING {COLUMNS}"
)
SQL_RECORD_ADVICE = (
    "INSERT INTO question (seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, "
    "message, link, state, requested_at, expires_at, updated_at) "
    "VALUES (nextval('question_seq'), NULL, %s, %s, %s, 'advice', %s, %s, %s, %s::jsonb, NULL, NULL, "
    f"%s::timestamptz, NULL, %s::timestamptz) RETURNING {COLUMNS}"
)
SQL_WAIT = (
    f"SELECT {COLUMNS} FROM question WHERE graph_id = %s AND correlation_id = %s AND turn = %s AND kind <> 'advice'"
)
SQL_ANSWER = (
    "UPDATE question SET seq = nextval('question_seq'), state = %s, decisions = %s::jsonb, outcome = %s::jsonb, "
    "answered_by = %s, answered_at = %s::timestamptz, updated_at = %s::timestamptz "
    "WHERE graph_id = %s AND correlation_id = %s AND turn = %s AND kind <> 'advice' "
    f"AND (state = 'open' OR %s::text = 'answered') RETURNING {COLUMNS}"
)
SQL_EXPIRE = (
    "UPDATE question SET seq = nextval('question_seq'), state = 'expired', updated_at = %s::timestamptz "
    f"WHERE state = 'open' AND kind <> 'advice' AND expires_at < %s::timestamptz RETURNING {COLUMNS}"
)
SQL_SINCE = f"SELECT {COLUMNS} FROM question WHERE seq > %s ORDER BY seq LIMIT %s"
SQL_COUNTS = "SELECT COALESCE(state, kind), count(*) FROM question GROUP BY COALESCE(state, kind)"


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _stamp(value: Any) -> str | None:
    """A stamp as the row carries it: ISO-8601 UTC with milliseconds, from the driver's datetime
    or a text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return iso_utc(value)
    parsed = parse_iso(str(value))
    return iso_utc(parsed) if parsed else str(value)


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


def row_from_record(record: Sequence[Any]) -> Row:
    """A row from a record of COLUMNS, as psycopg returns it (jsonb as Python values, timestamptz
    as datetimes)."""
    (row_id, seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, message, link,
     state, requested_at, expires_at, updated_at, answered_at, answered_by, decisions, outcome) = record
    return Row(
        id=int(row_id), seq=int(seq), kind=str(kind), graph_id=str(graph_id), correlation_id=str(correlation_id),
        turn=None if turn is None else int(turn), ticket=ticket, link=link, description=str(description),
        summary=str(summary), details=details, message=_json(message) or {},
        state=None if state is None else str(state), requested_at=_stamp(requested_at) or "",
        expires_at=_stamp(expires_at), updated_at=_stamp(updated_at) or "", answered_at=_stamp(answered_at),
        answered_by=answered_by, decisions=_json(decisions), outcome=_json(outcome),
    )  # fmt: skip


class PostgresQuestionStore:
    """The reference store: psycopg 3 over an asynchronous connection pool (the ``postgres``
    extra). The function inserts and updates one table and nothing else (schema/postgres.sql);
    the agent-question app and people read."""

    name = "postgres"

    def __init__(self, pool: Any, timeout_s: float = 5.0) -> None:
        self._pool = pool  # psycopg_pool.AsyncConnectionPool, or a recording fake in the tests
        self._timeout_s = timeout_s

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=self._timeout_s)

    async def close(self) -> None:
        await self._pool.close()

    async def _one(self, sql: str, params: tuple[Any, ...]) -> Row | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(sql, params)
            records = await cursor.fetchall()
        return row_from_record(records[0]) if records else None

    async def _many(self, sql: str, params: tuple[Any, ...]) -> list[Row]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(sql, params)
            records = await cursor.fetchall()
        return [row_from_record(record) for record in records]

    async def record(self, event: Mapping[str, Any], now: str) -> Row:
        fields = row_fields(event, now)
        key = wait_key(event)
        if key is not None:
            row = await self._one(SQL_RECORD_WAIT, (
                fields["ticket"], fields["graph_id"], fields["correlation_id"], fields["turn"], fields["kind"],
                fields["description"], fields["summary"], _compact(fields["message"]), fields["link"],
                fields["requested_at"], fields["expires_at"], now,
            ))
            if row is None:  # the guard kept a newer row: answer with it as it stands
                row = await self._one(SQL_WAIT, key)
        else:
            row = await self._one(SQL_RECORD_ADVICE, (
                fields["graph_id"], fields["correlation_id"], fields["turn"], fields["description"],
                fields["summary"], fields["details"], _compact(fields["message"]), fields["requested_at"], now,
            ))
        if row is None:  # pragma: no cover - RETURNING or the read always yields the row
            raise AppException(500, "the question record returned no row")
        return row

    async def answer(self, key: tuple[str, str, int], decisions: list[dict[str, Any]], who: str,
                     state: str, outcome: dict[str, Any], now: str) -> Row | None:
        row = await self._one(SQL_ANSWER, (state, _compact(decisions), _compact(outcome), who, now, now,
                                           key[0], key[1], key[2], state))
        # no row: the guard kept a row another device decided first (or no wait was recorded) - as stored
        return row if row is not None else await self._one(SQL_WAIT, key)

    async def expire(self, now: str) -> list[Row]:
        return await self._many(SQL_EXPIRE, (now, now))

    async def since(self, seq: int, limit: int) -> list[Row]:
        return await self._many(SQL_SINCE, (seq, limit))

    async def counts(self) -> dict[str, int]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(SQL_COUNTS, ())
            records = await cursor.fetchall()
        return {str(label): int(count) for label, count in records}


def postgres_store(settings: Settings) -> PostgresQuestionStore:
    """The pool from the ask.audit.postgres.* keys; nothing connects until open()."""
    if any(importlib.util.find_spec(name) is None for name in ("psycopg", "psycopg_pool")):
        raise AskError(MISSING_POSTGRES)
    from psycopg_pool import AsyncConnectionPool  # the [postgres] extra; checked above

    pool = AsyncConnectionPool(kwargs=dict(settings.audit_postgres), min_size=1, max_size=4, open=False,
                               timeout=settings.audit_postgres_timeout_s)
    return PostgresQuestionStore(pool, settings.audit_postgres_timeout_s)


def failure(exc: BaseException) -> str:
    """A failure as a log may carry it: the type and the database's SQLSTATE, never a message that
    could quote a row (the transcript audit store's rule)."""
    name = type(exc).__name__
    state = getattr(exc, "sqlstate", None)
    return f"{name} sqlstate={state}" if state else name


_store: QuestionStore | None = None
_sweeper: asyncio.Task[None] | None = None


def question_store() -> QuestionStore:
    global _store
    if _store is None:
        _store = MemoryQuestionStore()
    return _store


async def question_audit(_headers: dict[str, str], body: Body) -> Any:
    """The recorder (round 4): ``{record: <message event>}`` from person.ask - a wait upserted, an
    advice appended - and ``{answer: {graph_id, correlation_id, turn, decisions, who, at, status,
    body}}`` from the reply endpoint. Registered at boot with ``ask.audit.instances`` workers: the
    writes are order-independent. A failure is logged by type and reported, never raised with a
    message that could quote a row."""
    event: dict[str, Any] = body if isinstance(body, dict) else {}
    store = question_store()
    now = iso_utc()
    try:
        if isinstance(event.get("record"), Mapping):
            row = await store.record(event["record"], now)
            log.info("%s recorded %s %d for %s turn %s (seq %d)", AUDIT_ROUTE, row.kind, row.id, row.graph_id,
                     row.turn, row.seq)
            return {"recorded": True, "id": row.id, "seq": row.seq, "state": row.state}
        answer = event.get("answer")
        if isinstance(answer, Mapping):
            key = (str(answer["graph_id"]), str(answer["correlation_id"]), int(answer["turn"]))
            state = answer_state(answer.get("status"), answer.get("body"))
            row = await store.answer(key, list(answer.get("decisions") or []), str(answer.get("who") or ""),
                                     state, {"status": answer.get("status"), "body": answer.get("body")}, now)
            if row is None:
                log.warning("%s has no wait for %s %s turn %s - the answer is not recorded", AUDIT_ROUTE, *key)
                return {"recorded": False, "error": "no wait recorded"}
            log.info("%s recorded the answer on %d by %s - %s", AUDIT_ROUTE, row.id, row.answered_by, row.state)
            return {"recorded": True, "id": row.id, "seq": row.seq, "state": row.state}
    except Exception as exc:  # noqa: BLE001 - off the request path: logged by type, reported to the caller
        log.warning("%s could not write - %s", AUDIT_ROUTE, failure(exc))
        return {"recorded": False, "error": failure(exc)}
    raise AppException(400, f"{AUDIT_ROUTE} takes record or answer")


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


async def record(event: dict[str, Any], settings: Settings) -> bool:
    """Tell the recorder, as a local request so the row is written before anyone is told of the
    message: the record of a message event, or an answer. A recorder that fails or does not
    answer is logged by type and never fails the hold or the reply."""
    try:
        reply = await _po().request(AUDIT_ROUTE, event, timeout_ms=settings.audit_timeout_ms)
    except Exception as exc:  # noqa: BLE001 - in-process; nothing here may fail the caller
        log.warning("%s did not take the write - %s", AUDIT_ROUTE, type(exc).__name__)
        return False
    result = reply.body if isinstance(reply.body, Mapping) else {}
    if reply.get_status() >= 400 or not result.get("recorded"):
        log.warning("%s did not record - status %d, %s", AUDIT_ROUTE, reply.get_status(),
                    result.get("error") or "no detail")
        return False
    return True


@preload(route=ROUTE, instances=INSTANCES)
async def person_ask(headers: dict[str, str], body: Body) -> Any:
    """Carry a wait, or an advice, to the configured channels; answer with the ticket, the link,
    the expiry and what was delivered (``delivered`` alone for an advice). The message is written
    to the question record first, so a channel's device finds the row it is told of."""
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
    recorded = await record({"record": event}, settings)
    delivered = await deliver(event, settings)
    result: dict[str, Any] = {"delivered": delivered, "recorded": recorded}
    if message.kind in WAIT_KINDS:
        result["ticket"] = event["ticket"]
        result["link"] = event["link"]
        result["expires_at"] = event["expires_at"]
    log.info(
        "person.ask %s for %s turn %s - recorded %s, %d of %d channel(s) took it",
        message.kind, message.graph_id, message.turn, recorded, sum(1 for d in delivered if d["ok"]),
        len(delivered),
    )
    return result


@preload(route=HEALTH_ROUTE, instances=5, private=True)
async def ask_health(headers: dict[str, str], _body: Body) -> Any:
    """Health check speaking the engines' interface contract (type=info / type=health)."""
    settings = current_settings()
    if headers.get("type") == "info":
        try:
            rows = await question_store().counts()
        except Exception as exc:  # noqa: BLE001 - health reports a store it cannot read by type
            rows = {"error": failure(exc)}
        return {
            "service": ROUTE,
            "href": "http://127.0.0.1",
            "enabled": settings.enabled,
            "channels": list(settings.channels),
            "graphs": sorted(settings.graphs),
            "reply_url": settings.reply_url,
            "auth": settings.auth,
            "audit": question_store().name,
            "rows": rows,
        }
    return "person.ask is running fine"


# --- identity ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    who: str
    via: str


def _bearer(request: AsyncHttpRequest) -> str | None:
    value = request.header("authorization") or ""
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
    request: AsyncHttpRequest, settings: Settings, decided_by: str | None, allow_app: bool
) -> Identity:
    """Who is answering: a channel app under its bearer (``decided_by`` is the person it acts
    for), or a person by the configured mode - nobody in development, or a validated OAuth 2.0
    access token (the page obtains one by Authorization Code with PKCE, a channel app's device
    through its own UI)."""
    bearer = _bearer(request)
    if allow_app and settings.app_token and bearer and hmac.compare_digest(bearer, settings.app_token):
        return Identity(decided_by or "channel", "app")
    if settings.auth == "none":
        return Identity(decided_by or "anonymous", "none")
    if not bearer:
        raise ReplyError(401, "sign-in required - a bearer access token")
    claims = await _oauth2(settings).verify(bearer)
    who = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not who:
        raise ReplyError(401, "the access token names nobody")
    return Identity(str(who), "oauth2")


def _claimed(request: AsyncHttpRequest) -> str | None:
    """``decided_by`` from a JSON body when it is text - the person a channel app acts for, or the
    name offered in development; anything else is left to the endpoint's own check."""
    body = request.body
    value = body.get("decided_by") if isinstance(body, Mapping) else None
    return value if isinstance(value, str) and value else None


async def _verdict(body: Body, allow_app: bool) -> EventEnvelope:
    """The authentication function's reply: ``true`` with who decides in the headers, which the edge
    leaves on the request as its session map; a refusal is the error the invoker sees."""
    request = AsyncHttpRequest(body)
    try:
        identity = await identify(request, current_settings(), _claimed(request) if allow_app else None,
                                  allow_app)
    except ReplyError as exc:
        raise AppException(exc.status, exc.message) from exc
    return (EventEnvelope().set_body(True)
            .set_header(SESSION_WHO, identity.who).set_header(SESSION_VIA, identity.via))


@preload(route=IDENTITY_ROUTE, instances=INSTANCES, private=True)
async def ask_identity(_headers: dict[str, str], body: Body) -> Any:
    """The authentication function of ``GET /ask/{ticket}/wait``: a person, by the configured mode."""
    return await _verdict(body, allow_app=False)


@preload(route=REPLY_IDENTITY_ROUTE, instances=INSTANCES, private=True)
async def ask_reply_identity(_headers: dict[str, str], body: Body) -> Any:
    """The authentication function of ``POST /ask/reply``: a channel app under its bearer, naming the
    person it acts for in ``decided_by``, or a person by the configured mode."""
    return await _verdict(body, allow_app=True)


def _identity(request: AsyncHttpRequest) -> Identity:
    """Who the entry's authentication function found, from the request's session map."""
    return Identity(request.session_info(SESSION_WHO) or "anonymous", request.session_info(SESSION_VIA) or "none")


# --- the reply page and endpoint --------------------------------------------------------------

STYLE = (
    "body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:2rem;color:#222;background:#fafafa}"
    "h1{font-size:1.3rem}.meta{color:#666;font-size:.85rem}fieldset{background:#fff;border:1px solid #ddd;"
    "border-radius:8px;padding:1rem;margin:1rem 0;max-width:800px}legend{font-weight:600}"
    "pre{background:#f3f3f3;border-radius:6px;padding:.6rem .8rem;overflow:auto;font-size:.85rem}"
    "textarea,input[type=text]{width:100%;box-sizing:border-box;margin:.3rem 0}"
    "button{font-size:1rem;padding:.5rem 1.2rem}#outcome{margin-top:1rem}"
)

# The reply page's script: the wait is read from this process (GET /ask/{ticket}/wait, which re-advises
# the engine and never resumes) and rendered here, so that under oauth2 the page can first sign the
# person in - Authorization Code with PKCE, a public client in the browser, the token kept for this tab
# and sent as a bearer on the wait and the reply (round 4, R12: no gateway header, no cookie session).
SCRIPT = r"""
const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const wait = document.getElementById('wait'), out = document.getElementById('outcome');
let token = null;
const store = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} },
  del: (k) => { try { sessionStorage.removeItem(k); } catch (e) {} }
};
function b64url(bytes) { return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }
function random(n) { const a = new Uint8Array(n); crypto.getRandomValues(a); return b64url(a); }
async function sha256(text) { return b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))); }
async function discovery(issuer) {
  const key = 'ask.oidc.' + issuer, cached = store.get(key);
  if (cached) return JSON.parse(cached);
  const res = await fetch(issuer.replace(/\/$/, '') + '/.well-known/openid-configuration');
  if (!res.ok) throw new Error('the issuer is not reachable: ' + res.status);
  const doc = await res.json(); store.set(key, JSON.stringify(doc)); return doc;
}
async function signIn(oauth2) {
  const held = store.get('ask.token'), until = Number(store.get('ask.token.exp') || 0);
  if (held && until > Date.now() / 1000 + 30) return held;
  const doc = await discovery(oauth2.issuer), here = new URL(location.href);
  const redirect = location.origin + location.pathname;
  const code = here.searchParams.get('code'), state = here.searchParams.get('state');
  if (code && state && state === store.get('ask.state')) {
    const body = new URLSearchParams({grant_type: 'authorization_code', code, redirect_uri: redirect,
      client_id: oauth2.client_id, code_verifier: store.get('ask.verifier') || ''});
    const res = await fetch(doc.token_endpoint, {method: 'POST', headers: {'content-type': 'application/x-www-form-urlencoded'}, body});
    if (!res.ok) throw new Error('the sign-in was refused: ' + res.status);
    const grant = await res.json();
    store.set('ask.token', grant.access_token);
    store.set('ask.token.exp', String(Math.floor(Date.now() / 1000) + (grant.expires_in || 300)));
    store.del('ask.state'); store.del('ask.verifier');
    history.replaceState(null, '', location.pathname);
    return grant.access_token;
  }
  const verifier = random(32), nonce = random(16);
  store.set('ask.verifier', verifier); store.set('ask.state', nonce);
  const params = new URLSearchParams({response_type: 'code', client_id: oauth2.client_id, redirect_uri: redirect,
    scope: oauth2.scopes || 'openid', state: nonce, code_challenge: await sha256(verifier), code_challenge_method: 'S256'});
  if (oauth2.audience) params.set('audience', oauth2.audience);
  location.assign(doc.authorization_endpoint + '?' + params.toString());
  return new Promise(() => {});
}
function headers(extra) { const h = Object.assign({}, extra || {}); if (token) h['authorization'] = 'Bearer ' + token; return h; }
function callForm(call, i) {
  const id = esc(call.id), input = call.input ? '<pre>' + esc(JSON.stringify(call.input, null, 2)) + '</pre>' : '';
  if (call.ask) {
    const opts = Array.isArray(call.input && call.input.options) ? call.input.options : [];
    const radios = opts.map((o, k) => '<label><input type="radio" name="a-' + i + '" value="' + esc(o) + '"' + (k === 0 ? ' checked' : '') + '> ' + esc(o) + '</label><br>').join('')
      + (opts.length ? '<label><input type="radio" name="a-' + i + '" value="__other__"> another answer:</label>' : '');
    return '<fieldset data-id="' + id + '" data-kind="ask"><legend>' + esc(call.name) + ' - a question</legend>' + input + radios + '<textarea rows="3" placeholder="Your answer"></textarea></fieldset>';
  }
  if (call.wait === false) return '<fieldset><legend>' + esc(call.name) + ' - runs on resume</legend>' + input + '</fieldset>';
  return '<fieldset data-id="' + id + '" data-kind="write"><legend>' + esc(call.name) + ' - waits for a decision</legend><p class="meta">' + esc(call.route || '') + '</p>' + input
    + '<label><input type="radio" name="d-' + i + '" value="yes"> approve</label> <label><input type="radio" name="d-' + i + '" value="no"> refuse</label>'
    + '<input type="text" placeholder="A note for the model (optional)"></fieldset>';
}
function notice(title, text) { wait.innerHTML = '<h2>' + esc(title) + '</h2><p>' + esc(text) + '</p>'; }
function render(payload, advice) {
  const calls = Array.isArray(advice.calls) ? advice.calls : [];
  let html = '<p class="meta">graph ' + esc(payload.g) + ' &middot; correlation id ' + esc(payload.c) + ' &middot; turn ' + esc(payload.t) + '</p>';
  if (advice.message) html += '<p>' + esc(advice.message) + '</p>';
  html += '<form id="decisions">' + (calls.length ? calls.map(callForm).join('') : '<p>The graph reported no waiting call.</p>')
    + '<button type="submit">Send the decision</button></form>';
  wait.innerHTML = html;
  document.getElementById('decisions').addEventListener('submit', submit);
}
async function submit(ev) {
  ev.preventDefault();
  const form = ev.target, decisions = [];
  for (const set of form.querySelectorAll('fieldset[data-id]')) {
    const id = set.dataset.id;
    if (set.dataset.kind === 'ask') {
      const picked = set.querySelector('input[type=radio]:checked'), text = set.querySelector('textarea').value.trim();
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
  out.textContent = 'Sending...';
  const response = await fetch(REPLY, {method: 'POST', credentials: 'same-origin',
    headers: headers({'content-type': 'application/json'}), body: JSON.stringify({ticket: TICKET, decisions})});
  const body = await response.text();
  out.innerHTML = '<p class="meta">status ' + response.status + '</p><pre></pre>';
  out.querySelector('pre').textContent = body;
  if (response.ok) form.querySelector('button').disabled = true;
}
async function load() {
  try {
    const config = await (await fetch(CONFIG, {credentials: 'same-origin'})).json();
    if (config.auth === 'oauth2') {
      if (!config.oauth2 || !config.oauth2.issuer || !config.oauth2.client_id) {
        notice('Sign-in required', 'This page cannot sign you in - the issuer or the client id is not configured.'); return;
      }
      token = await signIn(config.oauth2);
    }
    const res = await fetch(WAIT, {credentials: 'same-origin', headers: headers()});
    const body = await res.json().catch(() => ({}));
    if (res.status === 401) { store.del('ask.token'); notice('Sign-in required', body.message || 'a bearer access token'); return; }
    if (res.status === 410 || body.type === 'nothing-waiting') {
      notice('This wait has expired', 'Nothing is waiting for this decision any more - it was decided already, or the suspension expired.'); return;
    }
    if (!res.ok) { notice('The graph refused', JSON.stringify(body)); return; }
    if (!body.advice || body.advice.type !== 'approval') { notice('The graph has moved on', JSON.stringify(body.advice || body)); return; }
    render(body.payload, body.advice);
  } catch (e) { notice('Something went wrong', String((e && e.message) || e)); }
}
load();
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


def _html(status: int, text: str) -> EventEnvelope:
    return EventEnvelope().set_status(status).set_header("content-type", "text/html").set_body(text)


def _json_reply(body: Any, status: int = 200) -> EventEnvelope:
    """A JSON reply as the page's script and the UI read it: serialized here, so a null stays a null
    (the engines' renderer would omit it), under its content type."""
    return (EventEnvelope().set_status(status).set_header("content-type", "application/json")
            .set_body(json.dumps(body)))


def _notice(status: int, title: str, text: str) -> EventEnvelope:
    return _html(status, _page(title, f"<p>{html.escape(text)}</p>"))


def shell_page(ticket: str, payload: Mapping[str, Any]) -> str:
    """The page for a wait: the kind and the graph from the ticket, and the script that signs the
    person in when the installation asks for it, reads the wait from this process and renders the
    form. The ticket is verified before this is rendered, so its characters are base64url and dots."""
    constants = (
        f"const TICKET = {json.dumps(ticket)}; const WAIT = {json.dumps(WAIT_PATH.replace('{ticket}', ticket))}; "
        f"const REPLY = {json.dumps(REPLY_PATH)}; const CONFIG = {json.dumps(CONFIG_PATH)};"
    )
    body = "<div id=\"wait\"><p class=\"meta\">Loading the wait...</p></div>\n<div id=\"outcome\"></div>"
    return _page(f"{str(payload['k']).capitalize()} - {payload['g']}", body, constants + SCRIPT)


def page_config(settings: Settings) -> dict[str, Any]:
    """What the page needs to sign a person in: the auth mode and, under oauth2, the issuer, the
    public client id, the scopes and the audience - no secret, since a public client has none."""
    config: dict[str, Any] = {"auth": settings.auth}
    if settings.auth == "oauth2":
        config["oauth2"] = {
            "issuer": settings.oauth2_issuer, "client_id": settings.oauth2_client_id,
            "scopes": settings.oauth2_scopes, "audience": settings.oauth2_audience,
        }  # fmt: skip
    return config


@preload(route=CONFIG_ROUTE, instances=INSTANCES, private=True)
async def ask_config(_headers: dict[str, str], _body: Body) -> Any:
    """``GET /ask/config``: the sign-in configuration the page reads first - open, it holds no secret."""
    return _json_reply(page_config(current_settings()))


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
    except (TimeoutError, aiohttp.ClientError) as exc:
        log.warning("The graph at %s did not answer - %s", request.url, type(exc).__name__)
        return map_outcome(None, None, type(exc).__name__)


def _ticket_refusal(exc: TicketError) -> EventEnvelope:
    """A ticket that cannot be used, as the page's script reads it: ``expired`` for 410, else ``error``."""
    return _json_reply({"type": "expired" if exc.status == 410 else "error", "message": exc.message}, exc.status)


@preload(route=PAGE_ROUTE, instances=INSTANCES, private=True)
async def ask_page(_headers: dict[str, str], body: Body) -> Any:
    """``GET /ask/{ticket}``: the page for the wait - a shell whose script signs the person in when
    the installation asks for it and reads the wait from ``GET /ask/{ticket}/wait``. A ticket that
    cannot be used is told so here, before any sign-in; the graph is never resumed."""
    settings = current_settings()
    ticket = AsyncHttpRequest(body).path_parameter("ticket") or ""
    try:
        payload = verify_ticket(ticket, settings.keys, time.time())
    except TicketError as exc:
        return _notice(exc.status, "This link cannot be used" if exc.status != 410 else "This wait has expired",
                       exc.message)
    if str(payload["g"]) not in settings.graphs:
        return _notice(404, "This graph is not configured", f"graph {payload['g']} is not configured for replies")
    return _html(200, shell_page(ticket, payload))


@preload(route=WAIT_ROUTE, instances=INSTANCES, private=True)
async def ask_wait(_headers: dict[str, str], body: Body) -> Any:
    """``GET /ask/{ticket}/wait``: the current wait as the engine returns it on re-advice, for the
    page's script - the signed-in person's request (``ask.identity``); never a resume. 410
    ``nothing-waiting`` when the engine holds nothing; the engine's own status and body when it
    refuses."""
    settings = current_settings()
    ticket = AsyncHttpRequest(body).path_parameter("ticket") or ""
    try:
        payload = verify_ticket(ticket, settings.keys, time.time())
    except TicketError as exc:
        return _ticket_refusal(exc)
    try:
        readvice = readvice_request(payload, settings.graphs)
    except ReplyError as exc:
        raise AppException(exc.status, exc.message) from exc
    status, advice = await _engine(readvice, settings.engine_timeout_ms)
    if status == 410 or advice.get("type") == "nothing-waiting":
        return _json_reply({
            "type": "nothing-waiting",
            "message": "Nothing is waiting for this decision any more - it was decided already, or the "
                       "suspension expired.",
        }, 410)  # fmt: skip
    if status >= 400:
        return _json_reply(advice, status)
    shown = {key: payload[key] for key in ("g", "c", "t", "k")}
    return _json_reply({"payload": shown, "advice": advice})


@preload(route=REPLY_ROUTE, instances=INSTANCES, private=True)
async def ask_reply(_headers: dict[str, str], body: Body) -> Any:
    """``POST /ask/reply``: verify the ticket, take who decides from the entry's authentication
    function (``ask.reply.identity``), forward the resume and return the engine's outcome."""
    settings = current_settings()
    request = AsyncHttpRequest(body)
    reply = request.body
    if not isinstance(reply, Mapping):
        raise AppException(400, "the reply must be a JSON map with ticket and decisions")
    ticket = reply.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise AppException(400, "ticket is required")
    try:
        payload = verify_ticket(ticket, settings.keys, time.time())
    except TicketError as exc:
        return _ticket_refusal(exc)
    decided_by = reply.get("decided_by")
    if decided_by is not None and not isinstance(decided_by, str):
        raise AppException(400, "decided_by must be text")
    at = iso_utc()
    identity = _identity(request)
    try:
        resume = build_resume(payload, reply.get("decisions"), identity.who, at, settings.graphs)
    except ReplyError as exc:
        raise AppException(exc.status, exc.message) from exc
    status, outcome = await _engine(resume, settings.engine_timeout_ms)
    log.info(
        "reply for %s turn %s by %s via %s - the graph answered %d %s",
        payload["g"], payload["t"], identity.who, identity.via, status, outcome.get("type"),
    )
    # the answer on the question record: the stamped decisions, who decided, when, and the engine's
    # outcome, written before the invoker is answered so a device's feed finds the row changed
    await record({"answer": {
        "graph_id": payload["g"], "correlation_id": payload["c"], "turn": payload["t"], "ticket": ticket,
        "decisions": resume.body["decisions"], "who": identity.who, "via": identity.via, "at": at,
        "status": status, "body": outcome,
    }}, settings)  # fmt: skip
    return _json_reply(outcome, status)


# --- boot -------------------------------------------------------------------------------------


async def _sweep(interval_ms: int) -> None:
    """Expire the waits the engine stopped waiting for, so the record never says open past the
    expiry even when nobody asks."""
    while True:
        await asyncio.sleep(interval_ms / 1000)
        try:
            expired = await question_store().expire(iso_utc())
        except Exception as exc:  # noqa: BLE001 - the store may be down; the next interval retries
            log.warning("%s could not expire the waits - %s", AUDIT_ROUTE, failure(exc))
            continue
        if expired:
            log.info("%s expired %d wait(s)", AUDIT_ROUTE, len(expired))


async def boot() -> None:
    """Load the settings inside the serving loop, before the first request, open the question
    record and register its recorder; a configuration the extension cannot start with stops the
    application by name, a record store that does not answer is logged and the writes fail by type
    until it does."""
    global _store, _sweeper
    settings = configure()
    if not settings.enabled:
        log.info("person.ask is disabled - %s is false", ENABLED)
        return
    if not settings.channels:
        log.warning("No channel configured (%s) - the link is returned to the graph only", CHANNELS)
    if not settings.graphs:
        log.warning("No graph configured (%s) - a reply cannot be forwarded", GRAPHS)
    store: QuestionStore = postgres_store(settings) if settings.audit == "postgres" else MemoryQuestionStore()
    _store = store
    if settings.audit != "postgres":
        log.warning(
            "The question record is in memory - a restart forgets every row and no other process reads it; "
            "set %s=postgres for a deployment", AUDIT,
        )
    if not default_registry.exists(AUDIT_ROUTE):
        default_registry.register(AUDIT_ROUTE, question_audit, instances=settings.audit_instances, private=True)
        log.info("Loaded PRIVATE %s, instances=%d - question record %s", AUDIT_ROUTE, settings.audit_instances,
                 store.name)
    try:
        await store.open()
    except Exception as exc:  # noqa: BLE001 - an outage, not a deployment error: the writes fail by type until it answers
        log.warning("The question record (%s) is not answering at start - %s", store.name, failure(exc))
    if _sweeper is None:
        _sweeper = asyncio.get_running_loop().create_task(_sweep(settings.audit_sweep_ms), name="question-audit-sweep")
    log.info(
        "person.ask ready - %d channel(s) %s, %d graph(s), reply page %s/ask/{ticket}, auth %s, record %s",
        len(settings.channels), list(settings.channels), len(settings.graphs), settings.reply_url,
        settings.auth, store.name,
    )


async def shutdown() -> None:
    global _http, _sweeper, _store, _post_office
    if _sweeper is not None:
        _sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _sweeper
        _sweeper = None
    if _store is not None:
        await _store.close()
        _store = None
    if _http is not None and not _http.closed:
        await _http.close()
    _http = None
    if _post_office is not None:
        await _post_office.close()
        _post_office = None  # a closed PostOffice is never reused: the next send builds a fresh one


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
    expires = datetime.fromtimestamp(payload["e"], tz=UTC)
    print(json.dumps({**payload, "expires_at": iso_utc(expires)}, indent=2))
    return 0


def _attach() -> None:
    # the reply page and endpoint are rest.yaml entries behind this module's functions - no route is mounted
    platform.on_startup(boot)
    platform.on_cleanup(shutdown)


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
