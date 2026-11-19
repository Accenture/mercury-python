"""
The agent-question app - the first channel of RFC-0014 (the engines' ``draft-design-specs/ask-a-person-channel.md``,
Part II): a store-and-forward inbox for a person whose device is not always connected, standing beside a chat, a
mail server or a topic as one channel among equals. The main flow (``person.ask`` in ``extensions/ask-person``)
knows nothing of it: it sends one message event with the resume link to the routes it is configured with, and this
application is one of those routes.

- ``agent.question.inbox`` is the intake, a composable function reached over Event-over-HTTP like any peer function.
  A wait (``kind`` ``approval`` or ``question``) is upserted on ``(graph_id, correlation_id, turn)`` - a re-advice for
  the same turn updates the card rather than adding one - and an advice (a status update, an acknowledgement, an
  outcome) is appended, since those accumulate. Write first: the row is committed before any push to a browser.
- The browser side runs under the pack's host (``Platform.add_routes``): ``GET /questions/app`` is the page,
  ``GET /questions?since=<seq>`` the replay a reconnecting device calls first - every row that changed since its
  cursor, waits and advices alike - and ``GET /questions/events`` the stream a connected device holds, each event
  naming a row and carrying it; ``Last-Event-ID`` is the same cursor, so a browser's reconnect misses nothing. The
  person selects which card to answer; the card's button automates the click: ``POST /questions/{id}/decide`` posts
  ``{ticket, decisions, decided_by}`` to the main flow's reply endpoint - the link's own base, under this app's
  bearer - records the outcome on the row and tells every connected device. ``POST /questions/{id}/seen`` marks an
  advice read.
- The inbox is PostgreSQL (``question.inbox: postgres``, the ``postgres`` extra, ``schema/postgres.sql``) or, for
  development and tests, memory. The engine stays the authority on an answer: a second device on the same card gets
  ``nothing waiting`` from the engine, and the row records the first. Rows are never deleted here; ``schema/purge.sql``
  is the installation's. Who may use the page is ``question.auth``: ``none`` in development, ``proxy`` (the
  installation's SSO, a trusted header set by the gateway) or ``oauth2`` (an access token validated against the
  issuer's published keys).

Run:  mercury-serve extensions/agent-question/agent_question.py

The pure half - the row a message becomes, the state an outcome leaves, the request the click sends, the inbox's
transitions - is pinned by ``tests/vectors/agent-question-vectors.json``, shared byte for byte with the Node.js twin.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

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
    app_config,
    get_logger,
    iso_utc,
    platform,
    preload,
)
from mercury_composable.event_stream import keep_alive_ms

log = get_logger(__name__)

# --- the contract -----------------------------------------------------------------------------

INTAKE_ROUTE = "agent.question.inbox"
HEALTH_ROUTE = "question.health"
KINDS = ("approval", "question", "advice")
WAIT_KINDS = ("approval", "question")
STATES = ("open", "answered", "expired", "seen")
PAGE_PATH = "/questions/app"
LIST_PATH = "/questions"
EVENTS_PATH = "/questions/events"
DECIDE_PATH = "/questions/{id}/decide"
SEEN_PATH = "/questions/{id}/seen"
AUTH_MODES = ("none", "proxy", "oauth2")
JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
LIST_LIMIT = 200
LIST_LIMIT_MAX = 1000

# configuration keys
INBOX = "question.inbox"
POSTGRES_PREFIX = "question.inbox.postgres."
SWEEP = "question.sweep"
AUTH = "question.auth"
AUTH_HEADER = "question.auth.header"
OAUTH2_ISSUER = "question.auth.oauth2.issuer"
OAUTH2_AUDIENCE = "question.auth.oauth2.audience"
OAUTH2_JWKS_URL = "question.auth.oauth2.jwks-url"
REPLY_TOKEN = "question.reply.token"
REPLY_TIMEOUT = "question.reply.timeout"
PAGE_TITLE = "question.page.title"

MISSING_POSTGRES = "question.inbox is postgres but psycopg is not installed - pip install 'mercury-composable[postgres]'"
DURATION = re.compile(r"^(\d+)\s*(ms|s|m|h|d)?$")
UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


class QuestionError(Exception):
    """A configuration the application cannot start with - the boot fails by name."""


class ReplyError(Exception):
    """A request the page or a channel client made that cannot be served: its status and message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def duration_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value * 1000 if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = DURATION.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group(1)) * UNIT_MS[match.group(2) or "s"]


def parse_iso(text: str) -> datetime | None:
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
    inbox: str
    postgres: dict[str, Any]
    postgres_timeout_s: float
    sweep_ms: int
    auth: str
    auth_header: str
    oauth2_issuer: str
    oauth2_audience: str
    oauth2_jwks_url: str
    reply_token: str
    reply_timeout_ms: int
    page_title: str


def _text(config: AppConfig, key: str, default: str = "") -> str:
    value = config.get(key)
    return default if value is None else str(value).strip()


def _timeout(config: AppConfig, key: str, default: str) -> int:
    value = config.get(key)
    ms = duration_ms(default if value is None else value)
    if ms is None or ms <= 0:
        raise QuestionError(f"{key} must be a duration such as 10s or 2m (seconds, s, m, h, d) > 0")
    return ms


def _whole_ms(config: AppConfig, key: str, default: int) -> int:
    """A key named in milliseconds (``timeout.ms``): a whole number > 0."""
    value = config.get(key)
    text = str(default if value is None else value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise QuestionError(f"{key} must be a whole number of milliseconds > 0")
    return int(text)


def load_settings(config: AppConfig) -> Settings:
    inbox = _text(config, INBOX, "memory").lower()
    if inbox not in ("memory", "postgres"):
        raise QuestionError(f"{INBOX} must be memory or postgres")
    auth = _text(config, AUTH, "none").lower()
    if auth not in AUTH_MODES:
        raise QuestionError(f"{AUTH} must be one of {', '.join(AUTH_MODES)}")
    auth_header = _text(config, AUTH_HEADER, "x-forwarded-user").lower()
    if auth == "proxy" and not auth_header:
        raise QuestionError(f"{AUTH_HEADER} names the header the gateway sets - it must not be empty")
    issuer = _text(config, OAUTH2_ISSUER).rstrip("/")
    jwks_url = _text(config, OAUTH2_JWKS_URL)
    if auth == "oauth2":
        if jwt is None:
            raise QuestionError(f"{AUTH} is oauth2 but PyJWT is not installed - pip install 'mercury-composable[ask]'")
        if not issuer and not jwks_url:
            raise QuestionError(f"{AUTH} is oauth2 - set {OAUTH2_ISSUER} or {OAUTH2_JWKS_URL}")
    timeout_ms = _whole_ms(config, POSTGRES_PREFIX + "timeout.ms", 5000)
    port_text = _text(config, POSTGRES_PREFIX + "port", "5432")
    if not port_text.isdigit() or int(port_text) <= 0:
        raise QuestionError(f"{POSTGRES_PREFIX}port must be a port number")
    postgres: dict[str, Any] = {
        "host": _text(config, POSTGRES_PREFIX + "host", "127.0.0.1"),
        "port": int(port_text),
        "dbname": _text(config, POSTGRES_PREFIX + "database", "mercury"),
        "user": _text(config, POSTGRES_PREFIX + "username") or None,
        "password": _text(config, POSTGRES_PREFIX + "password") or None,
        "sslmode": "disable" if _text(config, POSTGRES_PREFIX + "ssl", "true").lower() == "false" else "require",
        "connect_timeout": max(1, round(timeout_ms / 1000)),
        # the server-side bound on a statement, as the transcript audit store sets it
        "options": f"-c statement_timeout={timeout_ms}",
    }
    return Settings(
        inbox=inbox,
        postgres={key: value for key, value in postgres.items() if value is not None},
        postgres_timeout_s=timeout_ms / 1000,
        sweep_ms=_timeout(config, SWEEP, "30s"),
        auth=auth,
        auth_header=auth_header,
        oauth2_issuer=issuer,
        oauth2_audience=_text(config, OAUTH2_AUDIENCE),
        oauth2_jwks_url=jwks_url,
        reply_token=_text(config, REPLY_TOKEN),
        reply_timeout_ms=_timeout(config, REPLY_TIMEOUT, "10s"),
        page_title=_text(config, PAGE_TITLE, "Agent questions"),
    )


_settings: Settings | None = None


def configure(config: AppConfig | None = None) -> Settings:
    global _settings
    _settings = load_settings(config or app_config())
    return _settings


def current_settings() -> Settings:
    return _settings if _settings is not None else configure()


# --- the row and the pure half ----------------------------------------------------------------


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
    state: str
    requested_at: str
    expires_at: str | None
    updated_at: str
    answered_at: str | None = None
    answered_by: str | None = None
    decisions: list[dict[str, Any]] | None = None
    outcome: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """The row as the page and the replay carry it."""
        return {
            "id": self.id, "seq": self.seq, "kind": self.kind, "graph_id": self.graph_id,
            "correlation_id": self.correlation_id, "turn": self.turn, "ticket": self.ticket, "link": self.link,
            "description": self.description, "summary": self.summary, "details": self.details,
            "calls": self.message.get("calls"), "state": self.state, "requested_at": self.requested_at,
            "expires_at": self.expires_at, "updated_at": self.updated_at, "answered_at": self.answered_at,
            "answered_by": self.answered_by, "decisions": self.decisions, "outcome": self.outcome,
        }  # fmt: skip


def validate_event(event: Any) -> dict[str, Any]:
    """The message event the main flow sends, checked for what a row stores - or an AppException(400)."""
    if not isinstance(event, Mapping):
        raise AppException(400, "the inbox takes the message event as a map")
    kind = event.get("kind")
    if kind not in KINDS:
        raise AppException(400, "kind must be one of approval, question, advice")
    for name in ("graph_id", "description", "correlation_id", "summary"):
        value = event.get(name)
        if not isinstance(value, str) or not value.strip():
            raise AppException(400, f"{name} is required")
    turn = event.get("turn")
    if turn is not None and (isinstance(turn, bool) or not isinstance(turn, int) or turn < 0):
        raise AppException(400, "turn must be a whole number >= 0")
    if kind in WAIT_KINDS:
        if turn is None:
            raise AppException(400, "turn is required for an approval or a question")
        for name in ("ticket", "link", "expires_at"):
            if not isinstance(event.get(name), str) or not event[name]:
                raise AppException(400, f"{name} is required for an approval or a question")
        calls = event.get("calls")
        if not isinstance(calls, list) or not calls:
            raise AppException(400, "calls must be a non-empty list for an approval or a question")
    for name in ("details", "requested_at"):
        if event.get(name) is not None and not isinstance(event[name], str):
            raise AppException(400, f"{name} must be text")
    return dict(event)


def wait_key(event: Mapping[str, Any]) -> tuple[str, str, int] | None:
    """The key a wait upserts on; an advice has none and appends."""
    if event.get("kind") not in WAIT_KINDS:
        return None
    return str(event["graph_id"]), str(event["correlation_id"]), int(event["turn"])


def row_fields(event: Mapping[str, Any], now: str) -> dict[str, Any]:
    """What a row takes from a message event: every wait starts open with its ticket, link and expiry; an
    advice starts open with no ticket, no link and no expiry, and is marked seen by the person."""
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
        "state": "open",
        "requested_at": event.get("requested_at") or now,
        "expires_at": event.get("expires_at") if wait else None,
        "updated_at": now,
        "answered_at": None,
        "answered_by": None,
        "decisions": None,
        "outcome": None,
    }


def state_after(status: int | None, body: Any) -> str:
    """The state a decision's outcome leaves: the engine accepted it (answered), nothing was waiting
    (expired), or the decision was refused or the engine could not be reached (the wait stays open)."""
    kind = body.get("type") if isinstance(body, Mapping) else None
    if status == 410 or kind == "nothing-waiting":
        return "expired"
    if status is not None and 200 <= status < 300 and not (isinstance(body, Mapping) and body.get("error")):
        return "answered"
    return "open"


def reply_request(link: str, ticket: str, decisions: Sequence[Mapping[str, Any]], who: str) -> tuple[str, dict[str, Any]]:
    """The click, automated: the reply endpoint is the link's own base, the body is what a person's browser
    would post, with the person named for the app's bearer."""
    base, marker, _ = link.partition("/ask/")
    if not marker:
        raise ReplyError(409, "the row's link does not name a reply endpoint")
    return f"{base}/ask/reply", {"ticket": ticket, "decisions": [dict(d) for d in decisions], "decided_by": who}


# --- the inboxes ------------------------------------------------------------------------------


class Inbox(Protocol):
    name: str

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def take(self, event: Mapping[str, Any], now: str) -> Row: ...
    async def get(self, row_id: int) -> Row | None: ...
    async def since(self, seq: int, limit: int) -> list[Row]: ...
    async def decided(self, row_id: int, decisions: list[dict[str, Any]], who: str, state: str,
                      outcome: dict[str, Any], now: str) -> Row | None: ...
    async def seen(self, row_id: int, now: str) -> Row | None: ...
    async def expire(self, now: str) -> list[Row]: ...
    async def counts(self) -> dict[str, int]: ...


class MemoryInbox:
    """The reference semantics in one process: ids and a change sequence assigned here, a wait upserted on
    its key, an advice appended, every change bumping the sequence a replay reads from."""

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

    async def take(self, event: Mapping[str, Any], now: str) -> Row:
        fields = row_fields(event, now)
        key = wait_key(event)
        existing = self._rows.get(self._keys[key]) if key is not None and key in self._keys else None
        if existing is not None:
            row = replace(existing, seq=self._bump(), **fields)
        else:
            self._next_id += 1
            row = Row(id=self._next_id, seq=self._bump(), **fields)
            if key is not None:
                self._keys[key] = row.id
        self._rows[row.id] = row
        return row

    async def get(self, row_id: int) -> Row | None:
        return self._rows.get(row_id)

    async def since(self, seq: int, limit: int) -> list[Row]:
        rows = sorted((r for r in self._rows.values() if r.seq > seq), key=lambda r: r.seq)
        return rows[:limit]

    async def decided(self, row_id: int, decisions: list[dict[str, Any]], who: str, state: str,
                      outcome: dict[str, Any], now: str) -> Row | None:
        row = self._rows.get(row_id)
        if row is None:
            return None
        if row.state != "open" and state != "answered":
            return row  # decided first by another device: a later nothing-waiting never overwrites it
        row = replace(row, seq=self._bump(), state=state, decisions=decisions, outcome=outcome,
                      answered_by=who, answered_at=now, updated_at=now)
        self._rows[row_id] = row
        return row

    async def seen(self, row_id: int, now: str) -> Row | None:
        row = self._rows.get(row_id)
        if row is None:
            return None
        row = replace(row, seq=self._bump(), state="seen", updated_at=now)
        self._rows[row_id] = row
        return row

    async def expire(self, now: str) -> list[Row]:
        changed: list[Row] = []
        for row in list(self._rows.values()):
            if row.state == "open" and row.kind in WAIT_KINDS and row.expires_at and row.expires_at < now:
                row = replace(row, seq=self._bump(), state="expired", updated_at=now)
                self._rows[row.id] = row
                changed.append(row)
        return changed

    async def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in self._rows.values():
            result[row.state] = result.get(row.state, 0) + 1
        return result


# the statements the reference inbox runs, with psycopg's placeholders. JSON travels as compact text cast
# to jsonb and stamps as text cast to timestamptz, so the Node.js twin runs the same statements.
COLUMNS = ("id, seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, message, link, "
           "state, requested_at, expires_at, updated_at, answered_at, answered_by, decisions, outcome")
SQL_UPSERT_WAIT = (
    "INSERT INTO question_inbox (seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, "
    "message, link, state, requested_at, expires_at, updated_at) "
    "VALUES (nextval('question_inbox_seq'), %s, %s, %s, %s, %s, %s, %s, NULL, %s::jsonb, %s, 'open', "
    "%s::timestamptz, %s::timestamptz, %s::timestamptz) "
    "ON CONFLICT (graph_id, correlation_id, turn) WHERE kind <> 'advice' DO UPDATE SET "
    "seq = nextval('question_inbox_seq'), ticket = EXCLUDED.ticket, description = EXCLUDED.description, "
    "summary = EXCLUDED.summary, details = NULL, message = EXCLUDED.message, link = EXCLUDED.link, state = 'open', "
    "requested_at = EXCLUDED.requested_at, expires_at = EXCLUDED.expires_at, updated_at = EXCLUDED.updated_at, "
    "answered_at = NULL, answered_by = NULL, decisions = NULL, outcome = NULL "
    f"RETURNING {COLUMNS}"
)
SQL_INSERT_ADVICE = (
    "INSERT INTO question_inbox (seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, "
    "message, link, state, requested_at, expires_at, updated_at) "
    "VALUES (nextval('question_inbox_seq'), NULL, %s, %s, %s, 'advice', %s, %s, %s, %s::jsonb, NULL, 'open', "
    f"%s::timestamptz, NULL, %s::timestamptz) RETURNING {COLUMNS}"
)
SQL_GET = f"SELECT {COLUMNS} FROM question_inbox WHERE id = %s"
SQL_SINCE = f"SELECT {COLUMNS} FROM question_inbox WHERE seq > %s ORDER BY seq LIMIT %s"
SQL_DECIDED = (
    "UPDATE question_inbox SET seq = nextval('question_inbox_seq'), state = %s, decisions = %s::jsonb, "
    "outcome = %s::jsonb, answered_by = %s, answered_at = %s::timestamptz, updated_at = %s::timestamptz "
    f"WHERE id = %s AND (state = 'open' OR %s::text = 'answered') RETURNING {COLUMNS}"
)
SQL_SEEN = (
    "UPDATE question_inbox SET seq = nextval('question_inbox_seq'), state = 'seen', updated_at = %s::timestamptz "
    f"WHERE id = %s AND kind = 'advice' RETURNING {COLUMNS}"
)
SQL_EXPIRE = (
    "UPDATE question_inbox SET seq = nextval('question_inbox_seq'), state = 'expired', updated_at = %s::timestamptz "
    f"WHERE state = 'open' AND kind <> 'advice' AND expires_at < %s::timestamptz RETURNING {COLUMNS}"
)
SQL_COUNTS = "SELECT state, count(*) FROM question_inbox GROUP BY state"


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _stamp(value: Any) -> str | None:
    """A stamp as the row carries it: ISO-8601 UTC with milliseconds, from the driver's datetime or a text."""
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
    """A row from a record of COLUMNS, as psycopg returns it (jsonb as Python values, timestamptz as datetimes)."""
    (row_id, seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, message, link,
     state, requested_at, expires_at, updated_at, answered_at, answered_by, decisions, outcome) = record
    return Row(
        id=int(row_id), seq=int(seq), kind=str(kind), graph_id=str(graph_id), correlation_id=str(correlation_id),
        turn=None if turn is None else int(turn), ticket=ticket, link=link, description=str(description),
        summary=str(summary), details=details, message=_json(message) or {}, state=str(state),
        requested_at=_stamp(requested_at) or "", expires_at=_stamp(expires_at), updated_at=_stamp(updated_at) or "",
        answered_at=_stamp(answered_at), answered_by=answered_by, decisions=_json(decisions), outcome=_json(outcome),
    )  # fmt: skip


class PostgresInbox:
    """The reference inbox: psycopg 3 over an asynchronous connection pool (the ``postgres`` extra). The
    application inserts, selects and updates one table and nothing else (schema/postgres.sql); people read."""

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

    async def take(self, event: Mapping[str, Any], now: str) -> Row:
        fields = row_fields(event, now)
        if wait_key(event) is not None:
            row = await self._one(SQL_UPSERT_WAIT, (
                fields["ticket"], fields["graph_id"], fields["correlation_id"], fields["turn"], fields["kind"],
                fields["description"], fields["summary"], _compact(fields["message"]), fields["link"],
                fields["requested_at"], fields["expires_at"], now,
            ))
        else:
            row = await self._one(SQL_INSERT_ADVICE, (
                fields["graph_id"], fields["correlation_id"], fields["turn"], fields["description"],
                fields["summary"], fields["details"], _compact(fields["message"]), fields["requested_at"], now,
            ))
        if row is None:  # pragma: no cover - RETURNING always yields the row
            raise AppException(500, "the inbox returned no row")
        return row

    async def get(self, row_id: int) -> Row | None:
        return await self._one(SQL_GET, (row_id,))

    async def since(self, seq: int, limit: int) -> list[Row]:
        return await self._many(SQL_SINCE, (seq, limit))

    async def decided(self, row_id: int, decisions: list[dict[str, Any]], who: str, state: str,
                      outcome: dict[str, Any], now: str) -> Row | None:
        row = await self._one(SQL_DECIDED, (state, _compact(decisions), _compact(outcome), who, now, now, row_id, state))
        # no row: the guard kept a row another device decided first (or the row is gone) - answer with it as stored
        return row if row is not None else await self.get(row_id)

    async def seen(self, row_id: int, now: str) -> Row | None:
        return await self._one(SQL_SEEN, (now, row_id))

    async def expire(self, now: str) -> list[Row]:
        return await self._many(SQL_EXPIRE, (now, now))

    async def counts(self) -> dict[str, int]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(SQL_COUNTS, ())
            records = await cursor.fetchall()
        return {str(state): int(count) for state, count in records}


def postgres_inbox(settings: Settings) -> PostgresInbox:
    """The pool from the question.inbox.postgres.* keys; nothing connects until open()."""
    if any(importlib.util.find_spec(name) is None for name in ("psycopg", "psycopg_pool")):
        raise QuestionError(MISSING_POSTGRES)
    from psycopg_pool import AsyncConnectionPool  # the [postgres] extra; checked above

    pool = AsyncConnectionPool(kwargs=dict(settings.postgres), min_size=1, max_size=4, open=False,
                               timeout=settings.postgres_timeout_s)
    return PostgresInbox(pool, settings.postgres_timeout_s)


def failure(exc: BaseException) -> str:
    """A failure as a log may carry it: the type and the database's SQLSTATE, never a message that could
    quote a row (the transcript audit store's rule)."""
    name = type(exc).__name__
    state = getattr(exc, "sqlstate", None)
    return f"{name} sqlstate={state}" if state else name


# --- the wake-ups -----------------------------------------------------------------------------


class Wakeups:
    """Every connected device holds a queue; a changed row goes to all of them."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Row]] = set()

    def subscribe(self) -> asyncio.Queue[Row]:
        queue: asyncio.Queue[Row] = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Row]) -> None:
        self._queues.discard(queue)

    def publish(self, rows: Sequence[Row]) -> None:
        for queue in list(self._queues):
            for row in rows:
                queue.put_nowait(row)

    @property
    def connected(self) -> int:
        return len(self._queues)


_inbox: Inbox | None = None
_wakeups = Wakeups()
_http: aiohttp.ClientSession | None = None
_sweeper: asyncio.Task[None] | None = None


def inbox() -> Inbox:
    global _inbox
    if _inbox is None:
        _inbox = MemoryInbox()
    return _inbox


def _session() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession()
    return _http


# --- the intake -------------------------------------------------------------------------------


@preload(route=INTAKE_ROUTE, instances=1)
async def intake(_headers: dict[str, str], body: Body) -> Any:
    """Take a message event from the main flow: a wait upserted, an advice appended, every connected device
    told. One instance orders the writes of this process; the row is committed before the push."""
    event = validate_event(body)
    try:
        row = await inbox().take(event, iso_utc())
    except Exception as exc:
        log.warning("The inbox could not take a %s for %s - %s", event["kind"], event["graph_id"], failure(exc))
        raise AppException(503, "the inbox is not available") from exc
    _wakeups.publish([row])
    log.info("Inbox took %s %d for %s turn %s (seq %d)", row.kind, row.id, row.graph_id, row.turn, row.seq)
    return {"stored": True, "id": row.id, "seq": row.seq, "state": row.state}


@preload(route=HEALTH_ROUTE, instances=5, private=True)
async def question_health(headers: dict[str, str], _body: Body) -> Any:
    settings = current_settings()
    if headers.get("type") == "info":
        try:
            counts = await inbox().counts()
        except Exception as exc:  # noqa: BLE001 - health reports a store it cannot read by type
            counts = {"error": failure(exc)}
        return {
            "service": INTAKE_ROUTE, "href": "http://127.0.0.1", "inbox": inbox().name, "rows": counts,
            "connected": _wakeups.connected, "auth": settings.auth,
        }  # fmt: skip
    return "agent.question.inbox is running fine"


# --- identity ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    who: str
    via: str


def _bearer(request: web.Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class OAuth2Verifier:
    """An OAuth 2.0 access token validated against the issuer's published keys - the same verifier as the
    main flow's reply endpoint (extensions/ask-person)."""

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
            key = jwt.PyJWK(chosen, algorithm=None if chosen.get("alg") else algorithms[0]).key
            return jwt.decode(token, key=key, algorithms=algorithms, audience=self.audience or None,
                              issuer=self.issuer or None,
                              options={"require": ["exp"], "verify_aud": bool(self.audience)})
        except jwt.PyJWTError as exc:
            raise ReplyError(401, f"the access token was not accepted - {type(exc).__name__}") from exc


_verifier: OAuth2Verifier | None = None


def _oauth2(settings: Settings) -> OAuth2Verifier:
    global _verifier
    if (_verifier is None or _verifier.issuer != settings.oauth2_issuer
            or _verifier.audience != settings.oauth2_audience
            or (settings.oauth2_jwks_url and _verifier.jwks_url != settings.oauth2_jwks_url)):
        _verifier = OAuth2Verifier(settings.oauth2_issuer, settings.oauth2_audience, settings.oauth2_jwks_url)
    return _verifier


async def identify(request: web.Request, settings: Settings, claimed: str | None) -> Identity:
    """Who is at the page: nobody is asked in development (a name the page offers is taken as given), the
    gateway's trusted header under the installation's SSO, or a validated access token."""
    if settings.auth == "none":
        return Identity(claimed or "anonymous", "none")
    if settings.auth == "proxy":
        who = request.headers.get(settings.auth_header, "").strip()
        if not who:
            raise ReplyError(401, f"sign-in required - the {settings.auth_header} header names the person")
        return Identity(who, "proxy")
    bearer = _bearer(request)
    if not bearer:
        raise ReplyError(401, "sign-in required - a bearer access token")
    claims = await _oauth2(settings).verify(bearer)
    who = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not who:
        raise ReplyError(401, "the access token names nobody")
    return Identity(str(who), "oauth2")


# --- the browser side -------------------------------------------------------------------------


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"type": "error", "message": message}, status=status)


async def _expire_now() -> None:
    """Open waits past their expiry become expired, and every device is told - run before a replay and by
    the sweeper, so a card never says open after the engine stopped waiting."""
    try:
        changed = await inbox().expire(iso_utc())
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read is logged by type; the replay still answers
        log.warning("The inbox could not expire its waits - %s", failure(exc))
        return
    if changed:
        _wakeups.publish(changed)


async def list_rows(request: web.Request) -> web.Response:
    """``GET /questions?since=<seq>&limit=<n>``: every row that changed since the cursor, oldest change first,
    and the cursor to continue from - the replay a reconnecting device calls before it listens."""
    settings = current_settings()
    try:
        await identify(request, settings, None)
    except ReplyError as exc:
        return _error(exc.status, exc.message)
    try:
        since = int(request.query.get("since", "0"))
        limit = min(LIST_LIMIT_MAX, max(1, int(request.query.get("limit", str(LIST_LIMIT)))))
    except ValueError:
        return _error(400, "since and limit must be whole numbers")
    await _expire_now()
    try:
        rows = await inbox().since(since, limit)
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a 503 by type
        log.warning("The inbox could not list - %s", failure(exc))
        return _error(503, "the inbox is not available")
    return web.json_response({
        "since": since, "rows": [row.public() for row in rows],
        "next": max([since] + [row.seq for row in rows]), "more": len(rows) >= limit,
    })  # fmt: skip


def _frame(row: Row) -> bytes:
    return f"id: {row.seq}\nevent: question\ndata: {_compact(row.public())}\n\n".encode()


async def events(request: web.Request) -> web.StreamResponse:
    """``GET /questions/events``: the stream a connected device holds. Rows since ``Last-Event-ID`` (or
    ``since``) first, then every change as it happens; keep-alive comments while nothing changes."""
    settings = current_settings()
    try:
        await identify(request, settings, None)
    except ReplyError as exc:
        return _error(exc.status, exc.message)
    try:
        since = int(request.headers.get("last-event-id") or request.query.get("since", "0"))
    except ValueError:
        return _error(400, "Last-Event-ID must be a whole number")
    response = web.StreamResponse(status=200)
    response.headers["content-type"] = "text/event-stream"
    response.headers["cache-control"] = "no-cache"
    response.headers["x-accel-buffering"] = "no"
    await response.prepare(request)
    queue = _wakeups.subscribe()
    ping_ms = keep_alive_ms()
    try:
        await _expire_now()
        for row in await inbox().since(since, LIST_LIMIT_MAX):
            await response.write(_frame(row))
            since = row.seq
        while True:
            try:
                row = await asyncio.wait_for(queue.get(), (ping_ms / 1000) if ping_ms > 0 else None)
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
                continue
            if row.seq > since:
                await response.write(_frame(row))
                since = row.seq
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        _wakeups.unsubscribe(queue)
    with contextlib.suppress(ConnectionError):
        await response.write_eof()
    return response


async def _forward(url: str, body: dict[str, Any], settings: Settings) -> tuple[int | None, Any, str | None]:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if settings.reply_token:
        headers["authorization"] = f"Bearer {settings.reply_token}"
    try:
        async with _session().post(url, json=body, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=settings.reply_timeout_ms / 1000)) as response:
            text = await response.text()
            try:
                parsed: Any = json.loads(text) if text else None
            except ValueError:
                parsed = {"text": text[:500]}
            return response.status, parsed, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return None, None, type(exc).__name__


async def decide(request: web.Request) -> web.Response:
    """``POST /questions/{id}/decide`` with ``{decisions, decided_by?}``: the click, automated - the decisions
    go to the link's reply endpoint under this app's bearer, the outcome lands on the row, every device is
    told, and the invoker gets the row with the outcome. The first accepted decision stands: an acceptance always
    wins the row, and a nothing-waiting that lands after one never overwrites it."""
    settings = current_settings()
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _error(400, "the decision must be a JSON map with decisions")
    claimed = body.get("decided_by")
    if claimed is not None and not isinstance(claimed, str):
        return _error(400, "decided_by must be text")
    try:
        identity = await identify(request, settings, claimed or None)
    except ReplyError as exc:
        return _error(exc.status, exc.message)
    decisions = body.get("decisions")
    if not isinstance(decisions, list) or not decisions or not all(isinstance(d, Mapping) for d in decisions):
        return _error(400, "decisions must be a non-empty list, one per waiting call")
    try:
        row_id = int(request.match_info["id"])
    except ValueError:
        return _error(400, "the row id must be a whole number")
    row = await inbox().get(row_id)
    if row is None:
        return _error(404, f"row {row_id} is not in the inbox")
    if row.kind not in WAIT_KINDS or not row.ticket or not row.link:
        return _error(409, f"row {row_id} is an advice - nothing to decide")
    if row.state != "open":
        return _error(409, f"row {row_id} is {row.state} - decided by {row.answered_by or 'nobody'}")
    try:
        url, payload = reply_request(row.link, row.ticket, decisions, identity.who)
    except ReplyError as exc:
        return _error(exc.status, exc.message)
    status, answer, error = await _forward(url, payload, settings)
    outcome: dict[str, Any] = {"status": status, "body": answer}
    if error:
        outcome = {"status": 502, "body": {"type": "error", "message": f"the reply endpoint did not answer - {error}"}}
    state = state_after(outcome["status"], outcome["body"])
    now = iso_utc()
    updated = await inbox().decided(row_id, [dict(d) for d in decisions], identity.who, state, outcome, now)
    if updated is None:
        return _error(404, f"row {row_id} is not in the inbox")
    if updated.answered_by != identity.who or updated.answered_at != now:
        # two devices on one card: the other one's decision was accepted while this one was in flight, so the engine
        # answered nothing-waiting here and the row stands as the first decision left it - this device sees who
        log.info("Row %d: %s's decision met a row %s by %s - the row stands", row_id, identity.who, updated.state,
                 updated.answered_by)
        return web.json_response({"row": updated.public(), "outcome": outcome})
    _wakeups.publish([updated])
    log.info("Row %d decided by %s via %s - %s -> %s", row_id, identity.who, identity.via, outcome["status"], state)
    return web.json_response({"row": updated.public(), "outcome": outcome})


async def mark_seen(request: web.Request) -> web.Response:
    """``POST /questions/{id}/seen``: an advice the person has read."""
    settings = current_settings()
    try:
        await identify(request, settings, None)
    except ReplyError as exc:
        return _error(exc.status, exc.message)
    try:
        row_id = int(request.match_info["id"])
    except ValueError:
        return _error(400, "the row id must be a whole number")
    row = await inbox().get(row_id)
    if row is None:
        return _error(404, f"row {row_id} is not in the inbox")
    if row.kind != "advice":
        return _error(409, f"row {row_id} is a wait - decide it instead")
    updated = await inbox().seen(row_id, iso_utc())
    if updated is None:
        return _error(404, f"row {row_id} is not in the inbox")
    _wakeups.publish([updated])
    return web.json_response({"row": updated.public()})


STYLE = (
    "body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;color:#222;background:#f4f5f7}"
    "header{background:#1f2a44;color:#fff;padding:.8rem 1.2rem;display:flex;gap:1rem;align-items:center}"
    "header h1{font-size:1.1rem;margin:0;flex:1}header input{padding:.3rem .5rem;border-radius:6px;border:1px solid #889}"
    "#status{font-size:.8rem;opacity:.85}main{padding:1rem 1.2rem;max-width:1000px;margin:0 auto}"
    ".case{background:#fff;border:1px solid #dde;border-radius:10px;margin:1rem 0;padding:.8rem 1rem}"
    ".case h2{font-size:1rem;margin:0 0 .3rem}.meta{color:#667;font-size:.8rem}"
    ".card{border-top:1px solid #eee;padding:.7rem 0}.card.open{border-left:4px solid #e0a800;padding-left:.8rem}"
    ".card.answered{border-left:4px solid #2e9e5b;padding-left:.8rem;opacity:.85}"
    ".card.expired{border-left:4px solid #999;padding-left:.8rem;opacity:.7}.card.seen{opacity:.6}"
    ".card.advice{border-left:4px solid #3a7bd5;padding-left:.8rem}pre{background:#f3f3f3;border-radius:6px;"
    "padding:.5rem .7rem;overflow:auto;font-size:.8rem}fieldset{border:1px solid #ddd;border-radius:8px;margin:.5rem 0}"
    "textarea,input[type=text]{width:100%;box-sizing:border-box;margin:.2rem 0}button{padding:.4rem 1rem;border-radius:6px;"
    "border:1px solid #556;background:#fff;cursor:pointer}button.primary{background:#1f2a44;color:#fff}"
    ".outcome{font-size:.85rem;color:#334;margin-top:.4rem}.empty{color:#667;padding:2rem;text-align:center}"
    "header{position:sticky;top:0;z-index:2}.section{display:flex;align-items:center;gap:.5rem;font-size:.95rem;color:#445;margin:1.2rem 0 .4rem}.count{background:#e0a800;color:#222;border-radius:999px;padding:0 .5rem;font-size:.8rem;min-width:1.2rem;text-align:center}.count:empty{display:none}#history-section .count{background:#dde;color:#334}.panel{max-height:45vh;overflow-y:auto;border:1px solid #dde;border-radius:10px;background:#fafbfc;padding:0 .8rem}.panel .case{margin:.6rem 0}"
)

SCRIPT = r"""
const rows = new Map(); let cursor = 0; let source = null;
const status = document.getElementById('status');
const nameBox = document.getElementById('name');
try { nameBox.value = localStorage.getItem('agent-question.name') || ''; } catch (e) {}
nameBox.addEventListener('change', () => { try { localStorage.setItem('agent-question.name', nameBox.value); } catch (e) {} });
const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function merge(list) { for (const r of list) { rows.set(r.id, r); if (r.seq > cursor) cursor = r.seq; } render(); }
async function replay() {
  const res = await fetch(LIST + '?since=' + cursor, {credentials: 'same-origin'});
  if (!res.ok) { status.textContent = 'replay failed: ' + res.status; return; }
  const body = await res.json(); merge(body.rows); if (body.more) return replay();
}
function listen() {
  if (source) source.close();
  source = new EventSource(EVENTS + '?since=' + cursor, {withCredentials: true});
  source.addEventListener('question', (ev) => { merge([JSON.parse(ev.data)]); });
  source.onopen = () => { status.textContent = 'connected'; };
  source.onerror = () => { status.textContent = 'reconnecting...'; };
}
function callForm(call, i) {
  const id = esc(call.id), input = call.input ? '<pre>' + esc(JSON.stringify(call.input, null, 2)) + '</pre>' : '';
  if (call.ask) {
    const opts = Array.isArray(call.input && call.input.options) ? call.input.options : [];
    const radios = opts.map((o, k) => '<label><input type="radio" name="a-' + i + '" value="' + esc(o) + '"' + (k === 0 ? ' checked' : '') + '> ' + esc(o) + '</label><br>').join('')
      + (opts.length ? '<label><input type="radio" name="a-' + i + '" value="__other__"> another answer:</label>' : '');
    return '<fieldset data-id="' + id + '" data-kind="ask"><legend>' + esc(call.name) + ' - a question</legend>' + input + radios + '<textarea rows="2" placeholder="Your answer"></textarea></fieldset>';
  }
  if (call.wait === false) return '<fieldset><legend>' + esc(call.name) + ' - runs on resume</legend>' + input + '</fieldset>';
  return '<fieldset data-id="' + id + '" data-kind="write"><legend>' + esc(call.name) + ' - waits for a decision</legend>' + input
    + '<label><input type="radio" name="d-' + i + '" value="yes"> approve</label> <label><input type="radio" name="d-' + i + '" value="no"> refuse</label>'
    + '<input type="text" placeholder="A note for the model (optional)"></fieldset>';
}
function card(r) {
  const when = esc((r.updated_at || '').replace('T', ' ').slice(0, 19));
  let body = '<div class="card ' + esc(r.state) + ' ' + esc(r.kind) + '" data-row="' + r.id + '"><div><strong>' + esc(r.summary) + '</strong> <span class="meta">' + esc(r.kind) + (r.turn != null ? ' · turn ' + r.turn : '') + ' · ' + esc(r.state) + ' · ' + when + '</span></div>';
  if (r.details) body += '<p>' + esc(r.details) + '</p>';
  if (r.kind === 'advice') { if (r.state === 'open') body += '<button data-seen="' + r.id + '">Seen</button>'; }
  else if (r.state === 'open') {
    body += '<form data-decide="' + r.id + '">' + (r.calls || []).map(callForm).join('') + '<button class="primary" type="submit">Send the decision</button></form>';
    if (r.expires_at) body += '<div class="meta">expires ' + esc(r.expires_at.replace('T', ' ').slice(0, 19)) + '</div>';
  } else {
    body += '<div class="outcome">' + (r.answered_by ? 'decided by ' + esc(r.answered_by) + ' at ' + esc((r.answered_at || '').replace('T', ' ').slice(0, 19)) : '') + (r.outcome ? ' · ' + esc((r.outcome.body && (r.outcome.body.type || r.outcome.body.message)) || r.outcome.status) : '') + '</div>';
  }
  return body + '</div>';
}
function caseHtml(list) {
  list.sort((a, b) => a.seq - b.seq);
  const first = list[0];
  return '<section class="case"><h2>' + esc(first.description) + '</h2><div class="meta">' + esc(first.graph_id) + ' · ' + esc(first.correlation_id) + '</div>' + list.map(card).join('') + '</section>';
}
function render() {
  // what waits for the person first - every case with an open card - and the closed cases in the history panel,
  // newest activity first on both sides; the panel keeps its scroll position across updates
  const waiting = document.getElementById('cases'), history = document.getElementById('history');
  const groups = new Map();
  for (const r of rows.values()) { const key = r.graph_id + '\u0000' + r.correlation_id; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(r); }
  const newest = (list) => Math.max(...list.map(r => r.seq));
  const ordered = [...groups.values()].sort((a, b) => newest(b) - newest(a));
  const open = ordered.filter(list => list.some(r => r.state === 'open'));
  const closed = ordered.filter(list => !list.some(r => r.state === 'open'));
  const openCards = open.reduce((n, list) => n + list.filter(r => r.state === 'open').length, 0);
  document.getElementById('waiting-count').textContent = openCards ? String(openCards) : '';
  document.getElementById('history-count').textContent = closed.length ? String(closed.length) : '';
  document.title = (openCards ? '(' + openCards + ') ' : '') + TITLE;
  waiting.innerHTML = open.length ? open.map(caseHtml).join('') : '<div class="empty">Nothing waits for you.</div>';
  const keep = history.scrollTop;
  history.innerHTML = closed.length ? closed.map(caseHtml).join('') : '<div class="empty">Nothing yet.</div>';
  history.scrollTop = keep;
}
document.getElementById('cases').addEventListener('click', async (ev) => {
  const seen = ev.target.closest('button[data-seen]');
  if (seen) { await fetch(LIST + '/' + seen.dataset.seen + '/seen', {method: 'POST', credentials: 'same-origin'}); }
});
document.getElementById('cases').addEventListener('submit', async (ev) => {
  const form = ev.target.closest('form[data-decide]'); if (!form) return;
  ev.preventDefault();
  const decisions = [];
  for (const set of form.querySelectorAll('fieldset[data-id]')) {
    if (set.dataset.kind === 'ask') {
      const picked = set.querySelector('input[type=radio]:checked'); const text = set.querySelector('textarea').value.trim();
      decisions.push({id: set.dataset.id, answer: picked && picked.value !== '__other__' ? picked.value : text});
    } else {
      const choice = set.querySelector('input[type=radio]:checked');
      if (!choice) { alert('Approve or refuse every waiting call'); return; }
      const note = set.querySelector('input[type=text]').value.trim();
      const d = {id: set.dataset.id, approved: choice.value === 'yes'}; if (note) d.note = note; decisions.push(d);
    }
  }
  const button = form.querySelector('button'); button.disabled = true; button.textContent = 'Sending...';
  const res = await fetch(LIST + '/' + form.dataset.decide + '/decide', {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json'}, body: JSON.stringify({decisions, decided_by: nameBox.value.trim() || undefined})});
  if (!res.ok) { const err = await res.json().catch(() => ({})); alert(err.message || ('failed: ' + res.status)); button.disabled = false; button.textContent = 'Send the decision'; return; }
  const body = await res.json(); merge([body.row]);
});
replay().then(listen);
"""


def page(title: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f"<header><h1>{html.escape(title)}</h1><label class=\"meta\">your name <input id=\"name\" type=\"text\" "
        "placeholder=\"as the model will see it\"></label><span id=\"status\">connecting...</span></header>\n"
        "<main><section id=\"waiting-section\"><h2 class=\"section\">Waiting for you <span id=\"waiting-count\" "
        "class=\"count\"></span></h2><div id=\"cases\"><div class=\"empty\">Loading...</div></div></section>\n"
        "<section id=\"history-section\"><h2 class=\"section\">History <span id=\"history-count\" class=\"count\"></span>"
        "</h2><div id=\"history\" class=\"panel\"><div class=\"empty\">Loading...</div></div></section></main>\n"
        f"<script>const LIST = {json.dumps(LIST_PATH)}; const EVENTS = {json.dumps(EVENTS_PATH)}; "
        f"const TITLE = {json.dumps(title)};{SCRIPT}</script>\n"
        "</body>\n</html>\n"
    )


async def serve_page(request: web.Request) -> web.Response:
    settings = current_settings()
    try:
        await identify(request, settings, None)
    except ReplyError as exc:
        return web.Response(status=exc.status, text=page(f"Sign-in required - {exc.message}"), content_type="text/html")
    return web.Response(text=page(settings.page_title), content_type="text/html")


def mount(app: web.Application) -> None:
    """The browser side on the host (``Platform.add_routes``)."""
    app.router.add_get(PAGE_PATH, serve_page)
    app.router.add_get(EVENTS_PATH, events)
    app.router.add_get(LIST_PATH, list_rows)
    app.router.add_post(DECIDE_PATH, decide)
    app.router.add_post(SEEN_PATH, mark_seen)


# --- boot -------------------------------------------------------------------------------------


async def _sweep(interval_ms: int) -> None:
    """Expire the waits the engine stopped waiting for, so a card never says open past its expiry even
    when no device is asking."""
    while True:
        await asyncio.sleep(interval_ms / 1000)
        await _expire_now()


async def boot() -> None:
    """Load the settings and open the inbox inside the serving loop, before the first request; a
    configuration the application cannot start with, or a database that does not answer, stops it by name."""
    global _inbox, _sweeper
    settings = configure()
    if settings.inbox == "postgres":
        _inbox = postgres_inbox(settings)
    else:
        _inbox = MemoryInbox()
        log.warning("The inbox is in memory - a restart forgets every card; set %s=postgres for a deployment", INBOX)
    try:
        await _inbox.open()
    except QuestionError:
        raise
    except Exception as exc:
        raise QuestionError(f"the {settings.inbox} inbox could not open - {failure(exc)}") from exc
    _sweeper = asyncio.get_running_loop().create_task(_sweep(settings.sweep_ms))
    log.info("agent-question ready - inbox %s, page %s, stream %s, auth %s",
             _inbox.name, PAGE_PATH, EVENTS_PATH, settings.auth)


async def shutdown() -> None:
    global _http, _sweeper
    if _sweeper is not None:
        _sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _sweeper
        _sweeper = None
    if _inbox is not None:
        await _inbox.close()
    if _http is not None and not _http.closed:
        await _http.close()
    _http = None


def _attach() -> None:
    platform.on_startup(boot)
    platform.on_cleanup(shutdown)
    platform.add_routes(mount)


def _main(argv: list[str]) -> int:
    # -Dkey=value arguments belong to the configuration, which reads them from sys.argv itself
    _attach()
    platform.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
else:
    # loaded by mercury-serve: the boot runs inside the serving loop, before the first request
    _attach()
