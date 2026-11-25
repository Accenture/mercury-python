"""
The agent-question app - the first channel of RFC-0014 (the engines' ``draft-design-specs/ask-a-person-channel.md``,
Part II) and, since round 4 of the paper (section 21.3), the backend of the React UI maintained in the Node.js pack:
a reader of the question record the main flow keeps in the audit database, a feed of its changes for a person's
device, and the automated click that answers a wait. The main flow (``person.ask`` in ``extensions/ask-person``)
knows nothing of it: it records every message and answer itself, then sends the message event to the routes it is
configured with, and this application is one of those routes.

- ``agent.question.inbox`` is the intake, a composable function reached over Event-over-HTTP like any peer function.
  It writes nothing: the record is the recorder's (``question.audit`` in the ask-person process). It checks the event
  and wakes the feed, so a connected device sees the row the moment it is recorded.
- The browser side is ``resources/rest.yaml`` - the engines' REST automation, served by the pack's host in front of
  this process's private functions. ``GET /questions/config`` (``question.config``) tells the UI how to sign in;
  ``GET /questions?since=<seq>`` (``question.list``) is the replay a reconnecting device calls first - every row that
  changed since its cursor, oldest change first; ``GET /questions?before=<seq>&limit=<n>`` the history's older page,
  newest first, for an infinite scroll; ``GET /questions/events`` (``question.events``, a streaming entry) the stream
  a connected device holds, each ``question`` event a changed row whose ``seq`` is the cursor, ``Last-Event-ID`` the
  same cursor. ``GET /transcripts`` and ``GET /transcripts/{conversation}`` (``transcript.list``, ``transcript.show``)
  browse the transcript audit store the LLM helper writes beside the record. The person selects which card to
  answer; the card's button automates the click: ``POST /questions/{id}/decide`` (``question.decide``) posts
  ``{ticket, decisions, decided_by}`` to the main flow's reply endpoint - the link's own base, under this app's
  bearer - and the answer lands on the row through the recorder; the feed carries the change to every device. Who is
  at the UI is resolved once by the entries' authentication function, ``question.identity``, and read from the
  request's session map.
- The React UI (mercury-nodejs ``extensions/agent-question-ui``, the one front-end of both packs' backends) is served
  by the host's static content service from ``resources/public`` next to this file, at the application's root; the
  bundle is built in the Node.js pack and committed here. This app generates no page of its own.
- The store this app reads is PostgreSQL (``question.store: postgres``, the ``postgres`` extra, the audit database the
  main flow and the LLM helper write) or, for the tests, memory - nothing writes a memory store at run time. The
  feed polls the record's change sequence (``question.poll``) and the intake wakes it. Who may use the UI is
  ``question.auth``: ``none`` in development, ``oauth2`` (an OAuth 2.0 access token validated against the issuer's
  published keys; the UI obtains one by Authorization Code with PKCE from the issuer named by ``/questions/config``).

Run:  mercury-serve extensions/agent-question/agent_question.py -Dquestion.store=postgres -Dquestion.store.postgres.ssl=false

The pure half - the event the intake accepts, the request the click sends, the pages the feed and the history read -
is pinned by ``tests/vectors/agent-question-vectors.json``, shared byte for byte with the Node.js twin.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    EventStreamWriter,
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
# a wait's states; an advice has none (None) - whether a person read it is the device's business
STATES = ("open", "answered", "expired")
# the person's side: the URLs of resources/rest.yaml and the private functions behind them
LIST_PATH = "/questions"
EVENTS_PATH = "/questions/events"
CONFIG_PATH = "/questions/config"
DECIDE_PATH = "/questions/{id}/decide"
TRANSCRIPTS_PATH = "/transcripts"
TRANSCRIPT_PATH = "/transcripts/{conversation}"
LIST_ROUTE = "question.list"
EVENTS_ROUTE = "question.events"
CONFIG_ROUTE = "question.config"
DECIDE_ROUTE = "question.decide"
TRANSCRIPTS_ROUTE = "transcript.list"
TRANSCRIPT_ROUTE = "transcript.show"
# the entries' authentication function and the session keys it leaves on the request
IDENTITY_ROUTE = "question.identity"
SESSION_WHO = "who"
SESSION_VIA = "via"
INSTANCES = 10
# a device's stream is quiet for as long as nothing changes: its idle allowance (the head's x-ttl)
# is a day, after which the device reconnects from its cursor
STREAM_IDLE_SECONDS = 86_400
AUTH_MODES = ("none", "oauth2")
JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
LIST_LIMIT = 200
LIST_LIMIT_MAX = 1000
TRANSCRIPTS_LIMIT = 50
TRANSCRIPTS_LIMIT_MAX = 200
CONVERSATION_ID = re.compile(r"^[0-9a-f]{32}$")

# configuration keys
STORE = "question.store"
POSTGRES_PREFIX = "question.store.postgres."
POLL = "question.poll"
AUTH = "question.auth"
OAUTH2_ISSUER = "question.auth.oauth2.issuer"
OAUTH2_AUDIENCE = "question.auth.oauth2.audience"
OAUTH2_JWKS_URL = "question.auth.oauth2.jwks-url"
OAUTH2_CLIENT_ID = "question.auth.oauth2.client-id"
OAUTH2_SCOPES = "question.auth.oauth2.scopes"
REPLY_TOKEN = "question.reply.token"
REPLY_TIMEOUT = "question.reply.timeout"
PAGE_TITLE = "question.page.title"

MISSING_POSTGRES = "question.store is postgres but psycopg is not installed - pip install 'mercury-composable[postgres]'"
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
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# --- configuration ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    store: str
    postgres: dict[str, Any]
    postgres_timeout_s: float
    poll_ms: int
    auth: str
    oauth2_issuer: str
    oauth2_audience: str
    oauth2_jwks_url: str
    oauth2_client_id: str
    oauth2_scopes: str
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
    store = _text(config, STORE, "memory").lower()
    if store not in ("memory", "postgres"):
        raise QuestionError(f"{STORE} must be memory or postgres")
    auth = _text(config, AUTH, "none").lower()
    if auth not in AUTH_MODES:
        raise QuestionError(f"{AUTH} must be one of {', '.join(AUTH_MODES)}")
    issuer = _text(config, OAUTH2_ISSUER).rstrip("/")
    jwks_url = _text(config, OAUTH2_JWKS_URL)
    client_id = _text(config, OAUTH2_CLIENT_ID)
    if auth == "oauth2":
        if jwt is None:
            raise QuestionError(f"{AUTH} is oauth2 but PyJWT is not installed - pip install 'mercury-composable[ask]'")
        if not issuer and not jwks_url:
            raise QuestionError(f"{AUTH} is oauth2 - set {OAUTH2_ISSUER} or {OAUTH2_JWKS_URL}")
        if not issuer or not client_id:
            log.warning("%s is oauth2 without %s and %s - the UI cannot sign a person in; a token obtained elsewhere "
                        "still works", AUTH, OAUTH2_ISSUER, OAUTH2_CLIENT_ID)
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
        store=store,
        postgres={key: value for key, value in postgres.items() if value is not None},
        postgres_timeout_s=timeout_ms / 1000,
        poll_ms=_timeout(config, POLL, "1s"),
        auth=auth,
        oauth2_issuer=issuer,
        oauth2_audience=_text(config, OAUTH2_AUDIENCE),
        oauth2_jwks_url=jwks_url,
        oauth2_client_id=client_id,
        oauth2_scopes=_text(config, OAUTH2_SCOPES, "openid profile email"),
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
    """A row of the question record, as the main flow's recorder writes it (extensions/ask-person)."""

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
        """The row as the UI, the replay and the stream carry it."""
        return {
            "id": self.id, "seq": self.seq, "kind": self.kind, "graph_id": self.graph_id,
            "correlation_id": self.correlation_id, "turn": self.turn, "ticket": self.ticket, "link": self.link,
            "description": self.description, "summary": self.summary, "details": self.details,
            "calls": self.message.get("calls"), "state": self.state, "requested_at": self.requested_at,
            "expires_at": self.expires_at, "updated_at": self.updated_at, "answered_at": self.answered_at,
            "answered_by": self.answered_by, "decisions": self.decisions, "outcome": self.outcome,
        }  # fmt: skip


def validate_event(event: Any) -> dict[str, Any]:
    """The message event the main flow sends, checked for what a channel needs - or an AppException(400)."""
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


def reply_request(link: str, ticket: str, decisions: Sequence[Mapping[str, Any]], who: str) -> tuple[str, dict[str, Any]]:
    """The click, automated: the reply endpoint is the link's own base, the body is what a person's browser
    would post, with the person named for the app's bearer."""
    base, marker, _ = link.partition("/ask/")
    if not marker:
        raise ReplyError(409, "the row's link does not name a reply endpoint")
    return f"{base}/ask/reply", {"ticket": ticket, "decisions": [dict(d) for d in decisions], "decided_by": who}


def page_since(rows: Sequence[Row], since: int, limit: int) -> dict[str, Any]:
    """The replay's page: the rows whose sequence passed the cursor, oldest change first, and the cursor to
    continue from."""
    changed = sorted((row for row in rows if row.seq > since), key=lambda row: row.seq)[:limit]
    return {"rows": [row.public() for row in changed], "next": max([since] + [row.seq for row in changed]),
            "more": len(changed) >= limit}


def page_before(rows: Sequence[Row], before: int | None, limit: int) -> dict[str, Any]:
    """The history's page: the rows whose sequence is before the cursor (every row when there is none), newest
    change first, and the cursor of the older page (the smallest sequence shown; None when nothing was)."""
    older = sorted((row for row in rows if before is None or row.seq < before), key=lambda row: -row.seq)[:limit]
    return {"rows": [row.public() for row in older], "next": min((row.seq for row in older), default=None),
            "more": len(older) >= limit}


# --- the readers ------------------------------------------------------------------------------
#
# The app reads the question record the main flow writes and the transcript audit store the LLM helper writes,
# both in the audit database, under a reader's role; it writes nothing. The contract below is what another
# database implements; the memory reader carries its semantics for the tests.


class Reader(Protocol):
    name: str

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def get(self, row_id: int) -> Row | None: ...
    async def since(self, seq: int, limit: int) -> list[Row]: ...
    async def before(self, seq: int | None, limit: int) -> list[Row]: ...
    async def latest(self) -> int:
        """The newest change sequence (0 when the record is empty): where the feed starts following."""
        ...
    async def counts(self) -> dict[str, int]: ...
    async def conversations(self, before: str | None, limit: int, correlation_id: str | None,
                            graph_id: str | None) -> list[dict[str, Any]]:
        """The transcript audit store's conversation rows, newest first, before an instant when given."""
        ...
    async def conversation(self, conversation: str) -> dict[str, Any] | None:
        """One conversation row with its turns in order, or None."""
        ...


class MemoryReader:
    """The reader's semantics in one process, for the tests: rows and conversations put here by the test
    stand for what the recorder and the helper would have written."""

    name = "memory"

    def __init__(self) -> None:
        self._rows: dict[int, Row] = {}
        self._conversations: dict[str, dict[str, Any]] = {}
        self._turns: dict[str, list[dict[str, Any]]] = {}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def put(self, row: Row) -> Row:
        self._rows[row.id] = row
        return row

    def put_conversation(self, conversation: Mapping[str, Any], turns: Sequence[Mapping[str, Any]]) -> None:
        key = str(conversation["conversation"])
        self._conversations[key] = dict(conversation)
        self._turns[key] = [dict(turn) for turn in turns]

    async def get(self, row_id: int) -> Row | None:
        return self._rows.get(row_id)

    async def since(self, seq: int, limit: int) -> list[Row]:
        return sorted((r for r in self._rows.values() if r.seq > seq), key=lambda r: r.seq)[:limit]

    async def before(self, seq: int | None, limit: int) -> list[Row]:
        return sorted((r for r in self._rows.values() if seq is None or r.seq < seq), key=lambda r: -r.seq)[:limit]

    async def latest(self) -> int:
        return max((r.seq for r in self._rows.values()), default=0)

    async def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in self._rows.values():
            label = row.state or row.kind
            result[label] = result.get(label, 0) + 1
        return result

    async def conversations(self, before: str | None, limit: int, correlation_id: str | None,
                            graph_id: str | None) -> list[dict[str, Any]]:
        rows = [
            {key: value for key, value in c.items() if key not in ("system", "tools")}
            for c in self._conversations.values()
            if (before is None or str(c["started_at"]) < before)
            and (correlation_id is None or c.get("correlation_id") == correlation_id)
            and (graph_id is None or c.get("graph_id") == graph_id)
        ]
        return sorted(rows, key=lambda c: str(c["started_at"]), reverse=True)[:limit]

    async def conversation(self, conversation: str) -> dict[str, Any] | None:
        held = self._conversations.get(conversation)
        if held is None:
            return None
        return {"conversation": dict(held), "turns": [dict(t) for t in self._turns.get(conversation, [])]}


# the statements the reference reader runs, with psycopg's placeholders - selects only, on the question record
# (extensions/ask-person/schema/postgres.sql) and the transcript audit tables (extensions/llm-helper/schema/postgres.sql)
COLUMNS = ("id, seq, ticket, graph_id, correlation_id, turn, kind, description, summary, details, message, link, "
           "state, requested_at, expires_at, updated_at, answered_at, answered_by, decisions, outcome")
SQL_GET = f"SELECT {COLUMNS} FROM question WHERE id = %s"
SQL_SINCE = f"SELECT {COLUMNS} FROM question WHERE seq > %s ORDER BY seq LIMIT %s"
SQL_BEFORE = f"SELECT {COLUMNS} FROM question WHERE %s::bigint IS NULL OR seq < %s::bigint ORDER BY seq DESC LIMIT %s"
SQL_LATEST = "SELECT COALESCE(max(seq), 0) FROM question"
SQL_COUNTS = "SELECT COALESCE(state, kind) AS label, count(*) FROM question GROUP BY COALESCE(state, kind)"
CONVERSATION_COLUMNS = ("conversation, correlation_id, graph_id, node, model, status, turns, started_at, updated_at, "
                        "ended_at, expires_at, input_tokens, output_tokens, cache_creation_input_tokens, "
                        "cache_read_input_tokens")
SQL_CONVERSATIONS = (
    f"SELECT {CONVERSATION_COLUMNS} FROM transcript "
    "WHERE (%s::timestamptz IS NULL OR started_at < %s::timestamptz) AND (%s::text IS NULL OR correlation_id = %s::text) "
    "AND (%s::text IS NULL OR graph_id = %s::text) ORDER BY started_at DESC LIMIT %s"
)
SQL_CONVERSATION = f"SELECT {CONVERSATION_COLUMNS}, system, tools FROM transcript WHERE conversation = %s"
TURN_COLUMNS = ("turn, trace_id, trace_path, correlation_id, started_at, ended_at, stop_reason, status, request_id, "
                "input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, request, response, "
                "calls")
SQL_TURNS = f"SELECT {TURN_COLUMNS} FROM transcript_turn WHERE conversation = %s ORDER BY turn"


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _stamp(value: Any) -> str | None:
    """A stamp as a row carries it: ISO-8601 UTC with milliseconds, from the driver's datetime or a text."""
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
        summary=str(summary), details=details, message=_json(message) or {},
        state=None if state is None else str(state), requested_at=_stamp(requested_at) or "",
        expires_at=_stamp(expires_at), updated_at=_stamp(updated_at) or "", answered_at=_stamp(answered_at),
        answered_by=answered_by, decisions=_json(decisions), outcome=_json(outcome),
    )  # fmt: skip


def conversation_from_record(record: Sequence[Any]) -> dict[str, Any]:
    """A conversation row of the transcript audit store, as the UI shows it (the snapshot columns when selected)."""
    names = [name.strip() for name in CONVERSATION_COLUMNS.split(",")]
    if len(record) > len(names):
        names += ["system", "tools"]
    result: dict[str, Any] = {}
    for name, value in zip(names, record, strict=False):
        if name in ("started_at", "updated_at", "ended_at", "expires_at"):
            result[name] = _stamp(value)
        elif name in ("turns", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            result[name] = int(value or 0)
        elif name == "tools":
            result[name] = _json(value)
        else:
            result[name] = value if value is None else str(value)
    return result


def turn_from_record(record: Sequence[Any]) -> dict[str, Any]:
    """A turn row of the transcript audit store: the facts, and the request and response as the model exchanged them."""
    names = [name.strip() for name in TURN_COLUMNS.split(",")]
    result: dict[str, Any] = {}
    for name, value in zip(names, record, strict=False):
        if name in ("started_at", "ended_at"):
            result[name] = _stamp(value)
        elif name in ("turn", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            result[name] = int(value or 0)
        elif name in ("request", "response", "calls"):
            result[name] = _json(value)
        else:
            result[name] = value if value is None else str(value)
    return result


class PostgresReader:
    """The reference reader: psycopg 3 over an asynchronous connection pool (the ``postgres`` extra), selects
    only - a reader role suffices."""

    name = "postgres"

    def __init__(self, pool: Any, timeout_s: float = 5.0) -> None:
        self._pool = pool  # psycopg_pool.AsyncConnectionPool, or a recording fake in the tests
        self._timeout_s = timeout_s

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=self._timeout_s)

    async def close(self) -> None:
        await self._pool.close()

    async def _records(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(sql, params)
            return list(await cursor.fetchall())

    async def get(self, row_id: int) -> Row | None:
        records = await self._records(SQL_GET, (row_id,))
        return row_from_record(records[0]) if records else None

    async def since(self, seq: int, limit: int) -> list[Row]:
        return [row_from_record(record) for record in await self._records(SQL_SINCE, (seq, limit))]

    async def before(self, seq: int | None, limit: int) -> list[Row]:
        return [row_from_record(record) for record in await self._records(SQL_BEFORE, (seq, seq, limit))]

    async def latest(self) -> int:
        records = await self._records(SQL_LATEST, ())
        return int(records[0][0]) if records and records[0] and records[0][0] is not None else 0

    async def counts(self) -> dict[str, int]:
        return {str(label): int(count) for label, count in await self._records(SQL_COUNTS, ())}

    async def conversations(self, before: str | None, limit: int, correlation_id: str | None,
                            graph_id: str | None) -> list[dict[str, Any]]:
        records = await self._records(SQL_CONVERSATIONS, (before, before, correlation_id, correlation_id, graph_id,
                                                          graph_id, limit))
        return [conversation_from_record(record) for record in records]

    async def conversation(self, conversation: str) -> dict[str, Any] | None:
        records = await self._records(SQL_CONVERSATION, (conversation,))
        if not records:
            return None
        turns = await self._records(SQL_TURNS, (conversation,))
        return {"conversation": conversation_from_record(records[0]), "turns": [turn_from_record(t) for t in turns]}


def postgres_reader(settings: Settings) -> PostgresReader:
    """The pool from the question.store.postgres.* keys; nothing connects until open()."""
    if any(importlib.util.find_spec(name) is None for name in ("psycopg", "psycopg_pool")):
        raise QuestionError(MISSING_POSTGRES)
    from psycopg_pool import AsyncConnectionPool  # the [postgres] extra; checked above

    pool = AsyncConnectionPool(kwargs=dict(settings.postgres), min_size=1, max_size=4, open=False,
                               timeout=settings.postgres_timeout_s)
    return PostgresReader(pool, settings.postgres_timeout_s)


def failure(exc: BaseException) -> str:
    """A failure as a log may carry it: the type and the database's SQLSTATE, never a message that could
    quote a row (the transcript audit store's rule)."""
    name = type(exc).__name__
    state = getattr(exc, "sqlstate", None)
    return f"{name} sqlstate={state}" if state else name


# --- the wake-ups and the feed ----------------------------------------------------------------


class Wakeups:
    """Every connected device holds a queue; a changed row goes to all of them. ``close`` ends every
    stream (``None`` on the queue) when the application stops, so the host can close its connections."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Row | None]] = set()
        self.closing = False

    def subscribe(self) -> asyncio.Queue[Row | None]:
        queue: asyncio.Queue[Row | None] = asyncio.Queue()
        self._queues.add(queue)
        if self.closing:
            queue.put_nowait(None)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Row | None]) -> None:
        self._queues.discard(queue)

    def publish(self, rows: Sequence[Row]) -> None:
        for queue in list(self._queues):
            for row in rows:
                queue.put_nowait(row)

    def close(self) -> None:
        """The application is stopping: every connected device's stream ends now, and a stream opened
        from here on ends at once; the devices reconnect to the next instance."""
        self.closing = True
        for queue in list(self._queues):
            queue.put_nowait(None)

    @property
    def connected(self) -> int:
        return len(self._queues)


class Feed:
    """The record's changes, read by their sequence and handed to the connected devices: a poll every
    ``question.poll`` and a wake-up from the intake or a click, whichever comes first."""

    def __init__(self) -> None:
        self.cursor = 0
        self._kick: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    def kick(self) -> None:
        if self._kick is not None:
            self._kick.set()

    async def poll_once(self) -> list[Row]:
        """Read what changed since the cursor, tell every device, move the cursor; a store that cannot be read
        is logged by type and the next poll retries."""
        try:
            rows = await reader().since(self.cursor, LIST_LIMIT_MAX)
        except Exception as exc:  # noqa: BLE001 - the store may be down; the next poll retries
            log.warning("The feed could not read the record - %s", failure(exc))
            return []
        if rows:
            self.cursor = max(self.cursor, *(row.seq for row in rows))
            _wakeups.publish(rows)
        return rows

    async def _run(self, poll_ms: int) -> None:
        kick = self._kick = asyncio.Event()
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(kick.wait(), poll_ms / 1000)
            kick.clear()
            await self.poll_once()

    def start(self, poll_ms: int) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(poll_ms), name="question-feed")

    async def stop(self) -> None:
        task, self._task, self._kick = self._task, None, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


_reader: Reader | None = None
_wakeups = Wakeups()
_feed = Feed()
_http: aiohttp.ClientSession | None = None


def reader() -> Reader:
    global _reader
    if _reader is None:
        _reader = MemoryReader()
    return _reader


def _session() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession()
    return _http


# --- the intake -------------------------------------------------------------------------------


@preload(route=INTAKE_ROUTE, instances=5)
async def intake(_headers: dict[str, str], body: Body) -> Any:
    """Take a message event from the main flow: nothing to write - the recorder has the row - but every
    connected device is told to look, so the card is on its screen the moment it is recorded."""
    event = validate_event(body)
    _feed.kick()
    log.info("Intake took %s for %s turn %s - the feed looks", event["kind"], event["graph_id"], event.get("turn"))
    return {"accepted": True, "kind": event["kind"], "graph_id": event["graph_id"], "turn": event.get("turn")}


@preload(route=HEALTH_ROUTE, instances=5, private=True)
async def question_health(headers: dict[str, str], _body: Body) -> Any:
    settings = current_settings()
    if headers.get("type") == "info":
        try:
            counts = await reader().counts()
        except Exception as exc:  # noqa: BLE001 - health reports a store it cannot read by type
            counts = {"error": failure(exc)}
        return {
            "service": INTAKE_ROUTE, "href": "http://127.0.0.1", "store": reader().name, "rows": counts,
            "feed": _feed.cursor, "connected": _wakeups.connected, "auth": settings.auth,
        }  # fmt: skip
    return "agent.question.inbox is running fine"


# --- identity ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    who: str
    via: str


def _bearer(request: AsyncHttpRequest) -> str | None:
    scheme, _, token = (request.header("authorization") or "").partition(" ")
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


async def identify(request: AsyncHttpRequest, settings: Settings, claimed: str | None) -> Identity:
    """Who is at the UI: nobody is asked in development (a name the page offers is taken as given), or a
    validated access token - the UI obtains one by Authorization Code with PKCE."""
    if settings.auth == "none":
        return Identity(claimed or "anonymous", "none")
    bearer = _bearer(request)
    if not bearer:
        raise ReplyError(401, "sign-in required - a bearer access token")
    claims = await _oauth2(settings).verify(bearer)
    who = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not who:
        raise ReplyError(401, "the access token names nobody")
    return Identity(str(who), "oauth2")


def _claimed(request: AsyncHttpRequest) -> str | None:
    """``decided_by`` from a JSON body when it is text - the name the page offers in development; anything
    else is left to the click's own check."""
    body = request.body
    value = body.get("decided_by") if isinstance(body, Mapping) else None
    return value if isinstance(value, str) and value else None


@preload(route=IDENTITY_ROUTE, instances=INSTANCES, private=True)
async def question_identity(_headers: dict[str, str], body: Body) -> Any:
    """The authentication function of the feed, the history, the transcripts and the click: ``true`` with who is
    at the UI in the headers, which the edge leaves on the request as its session map; a refusal is the error the
    UI sees."""
    request = AsyncHttpRequest(body)
    try:
        identity = await identify(request, current_settings(), _claimed(request))
    except ReplyError as exc:
        raise AppException(exc.status, exc.message) from exc
    return EventEnvelope().set_body(True).set_header(SESSION_WHO, identity.who).set_header(SESSION_VIA, identity.via)


def _identity(request: AsyncHttpRequest) -> Identity:
    """Who the entry's authentication function found, from the request's session map."""
    return Identity(request.session_info(SESSION_WHO) or "anonymous", request.session_info(SESSION_VIA) or "none")


# --- the browser side -------------------------------------------------------------------------


def _json_reply(body: Any, status: int = 200) -> EventEnvelope:
    """A JSON reply as the UI reads it: serialized here, so a null stays a null (an advice's ``state``, a silent
    engine's ``status`` - the engines' renderer would omit them), under its content type."""
    return EventEnvelope().set_status(status).set_header("content-type", "application/json").set_body(json.dumps(body))


def ui_config(settings: Settings) -> dict[str, Any]:
    """What the UI needs before it signs in: the auth mode, the title and, under oauth2, the issuer, the public
    client id, the scopes and the audience - no secret, since a public client has none."""
    config: dict[str, Any] = {"auth": settings.auth, "title": settings.page_title}
    if settings.auth == "oauth2":
        config["oauth2"] = {
            "issuer": settings.oauth2_issuer, "client_id": settings.oauth2_client_id,
            "scopes": settings.oauth2_scopes, "audience": settings.oauth2_audience,
        }  # fmt: skip
    return config


@preload(route=CONFIG_ROUTE, instances=INSTANCES, private=True)
async def question_config(_headers: dict[str, str], _body: Body) -> Any:
    """``GET /questions/config``: the UI's sign-in configuration - open, it holds no secret."""
    return _json_reply(ui_config(current_settings()))


def _whole(text: str | None, default: int) -> int:
    if text is None or text == "":
        return default
    if not text.isdigit():
        raise ValueError(text)
    return int(text)


@preload(route=LIST_ROUTE, instances=INSTANCES, private=True)
async def question_list(_headers: dict[str, str], body: Body) -> Any:
    """``GET /questions?since=<seq>&limit=<n>``: every row that changed since the cursor, oldest change first,
    and the cursor to continue from - the replay a reconnecting device calls before it listens.
    ``GET /questions?before=<seq>&limit=<n>``: the history's older page, newest change first, and the cursor of
    the page before it - the infinite scroll."""
    request = AsyncHttpRequest(body)
    paging = "before" in request.query_parameters_map
    before: int | None = None
    since = 0
    try:
        limit = min(LIST_LIMIT_MAX, max(1, _whole(request.query_parameter("limit"), LIST_LIMIT)))
        if paging:
            text = request.query_parameter("before")
            before = _whole(text, 0) if text else None
        else:
            since = _whole(request.query_parameter("since"), 0)
    except ValueError:
        raise AppException(400, "since, before and limit must be whole numbers") from None
    try:
        if paging:
            older = await reader().before(before, limit)
            return _json_reply(page_before(older, None, limit) | {"before": before})
        rows = await reader().since(since, limit)
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a 503 by type
        log.warning("The record could not be read - %s", failure(exc))
        raise AppException(503, "the question record is not available") from None
    return _json_reply(page_since(rows, since, limit) | {"since": since})


def _frame(row: Row) -> str:
    """A changed row as the ``question`` event's data: serialized here, so a null stays a null."""
    return json.dumps(row.public())


_streams: set[asyncio.Task[None]] = set()


async def _follow(out: EventStreamWriter, since: int) -> None:
    """One device's stream: the rows since its cursor first, then every change the feed finds, until the device
    leaves (the edge closes the reply sink and ``connected`` turns false) or the application stops (the wake-ups
    close); the edge sends the keep-alive comments while nothing changes."""
    queue = _wakeups.subscribe()
    ping_ms = keep_alive_ms()
    check_seconds = ping_ms / 1000 if ping_ms > 0 else 30.0
    try:
        for row in await reader().since(since, LIST_LIMIT_MAX):
            out.write_named("question", _frame(row))
            since = row.seq
        while out.connected:
            try:
                row = await asyncio.wait_for(queue.get(), check_seconds)
            except TimeoutError:
                continue
            if row is None:
                out.close()  # the application is stopping; the device reconnects
                break
            if row.seq > since and out.connected:
                out.write_named("question", _frame(row))
                since = row.seq
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read ends the stream; the device reconnects
        log.warning("The stream could not read the record - %s", failure(exc))
        out.fail(AppException(503, "the question record is not available"))
    finally:
        _wakeups.unsubscribe(queue)


@preload(route=EVENTS_ROUTE, instances=INSTANCES, interceptor=True, private=True)
async def question_events(_headers: dict[str, str], event: EventEnvelope) -> None:
    """``GET /questions/events`` (a streaming entry): the stream a connected device holds. The head goes out at
    once, with a day's idle allowance; the rows since ``Last-Event-ID`` (or ``since``) and every later change
    follow from a task of their own, so the function's workers are never held by the devices."""
    request = AsyncHttpRequest(event.body)
    try:
        since = int(request.header("last-event-id") or request.query_parameter("since") or "0")
    except ValueError:
        raise AppException(400, "Last-Event-ID must be a whole number") from None
    out = EventStreamWriter.from_request(event)
    out.first(200, "text/event-stream", ttl_seconds=STREAM_IDLE_SECONDS)
    out.write(None)  # the head alone: the stream is open before the first row
    task = asyncio.get_running_loop().create_task(_follow(out, since), name="question-stream")
    _streams.add(task)
    task.add_done_callback(_streams.discard)


@preload(route=TRANSCRIPTS_ROUTE, instances=INSTANCES, private=True)
async def transcript_list(_headers: dict[str, str], body: Body) -> Any:
    """``GET /transcripts?before=<iso>&limit=<n>&correlation_id=&graph_id=``: the transcript audit store's
    conversations, newest first, and the instant to continue from."""
    request = AsyncHttpRequest(body)
    before = request.query_parameter("before") or None
    if before is not None:
        parsed = parse_iso(before)
        if parsed is None:
            raise AppException(400, "before must be an ISO-8601 time")
        before = iso_utc(parsed)
    try:
        limit = min(TRANSCRIPTS_LIMIT_MAX, max(1, _whole(request.query_parameter("limit"), TRANSCRIPTS_LIMIT)))
    except ValueError:
        raise AppException(400, "limit must be a whole number") from None
    try:
        rows = await reader().conversations(before, limit, request.query_parameter("correlation_id") or None,
                                            request.query_parameter("graph_id") or None)
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a 503 by type
        log.warning("The transcripts could not be read - %s", failure(exc))
        raise AppException(503, "the transcript audit store is not available") from None
    oldest = rows[-1]["started_at"] if rows else None
    return _json_reply({"conversations": rows, "next": oldest, "more": len(rows) >= limit})


@preload(route=TRANSCRIPT_ROUTE, instances=INSTANCES, private=True)
async def transcript_show(_headers: dict[str, str], body: Body) -> Any:
    """``GET /transcripts/{conversation}``: one conversation of the transcript audit store with its turns, as the
    model exchanged them."""
    conversation = AsyncHttpRequest(body).path_parameter("conversation") or ""
    if not CONVERSATION_ID.fullmatch(conversation):
        raise AppException(400, "a conversation is 32 hexadecimal characters")
    try:
        found = await reader().conversation(conversation)
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a 503 by type
        log.warning("The transcript could not be read - %s", failure(exc))
        raise AppException(503, "the transcript audit store is not available") from None
    if found is None:
        raise AppException(404, f"conversation {conversation} is not in the audit store")
    return _json_reply(found)


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
    except (TimeoutError, aiohttp.ClientError) as exc:
        return None, None, type(exc).__name__


@preload(route=DECIDE_ROUTE, instances=INSTANCES, private=True)
async def question_decide(_headers: dict[str, str], body: Body) -> Any:
    """``POST /questions/{id}/decide`` with ``{decisions, decided_by?}``: the click, automated - the decisions
    go to the link's reply endpoint under this app's bearer, the recorder behind that endpoint writes the answer
    on the row, and the invoker gets the row as it then stands with the engine's outcome; the feed carries the
    change to every device. Who decides is the entry's authentication function's finding."""
    settings = current_settings()
    request = AsyncHttpRequest(body)
    decision = request.body
    if not isinstance(decision, Mapping):
        raise AppException(400, "the decision must be a JSON map with decisions")
    claimed = decision.get("decided_by")
    if claimed is not None and not isinstance(claimed, str):
        raise AppException(400, "decided_by must be text")
    identity = _identity(request)
    decisions = decision.get("decisions")
    if not isinstance(decisions, list) or not decisions or not all(isinstance(d, Mapping) for d in decisions):
        raise AppException(400, "decisions must be a non-empty list, one per waiting call")
    try:
        row_id = int(request.path_parameter("id") or "")
    except ValueError:
        raise AppException(400, "the row id must be a whole number") from None
    try:
        row = await reader().get(row_id)
    except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a 503 by type
        log.warning("The record could not be read - %s", failure(exc))
        raise AppException(503, "the question record is not available") from None
    if row is None:
        raise AppException(404, f"row {row_id} is not in the record")
    if row.kind not in WAIT_KINDS or not row.ticket or not row.link:
        raise AppException(409, f"row {row_id} is an advice - nothing to decide")
    if row.state != "open":
        raise AppException(409, f"row {row_id} is {row.state} - decided by {row.answered_by or 'nobody'}")
    try:
        url, payload = reply_request(row.link, row.ticket, decisions, identity.who)
    except ReplyError as exc:
        raise AppException(exc.status, exc.message) from exc
    status, answer, error = await _forward(url, payload, settings)
    outcome: dict[str, Any] = {"status": status, "body": answer}
    if error:
        outcome = {"status": 502, "body": {"type": "error", "message": f"the reply endpoint did not answer - {error}"}}
    # the recorder behind the reply endpoint has written the answer by now: read the row as it stands and let
    # the feed carry the change to every device
    _feed.kick()
    try:
        updated = await reader().get(row_id)
    except Exception as exc:  # noqa: BLE001 - the decision reached the engine; the row is reported as last read
        log.warning("The record could not be re-read after a decision - %s", failure(exc))
        updated = None
    shown = updated if updated is not None else row
    log.info("Row %d decided by %s via %s - %s -> %s", row_id, identity.who, identity.via, outcome["status"], shown.state)
    return _json_reply({"row": shown.public(), "outcome": outcome})


# --- boot -------------------------------------------------------------------------------------


async def boot() -> None:
    """Load the settings and open the reader inside the serving loop, before the first request; a configuration
    the application cannot start with, or a database that does not answer, stops it by name. The feed starts
    at the record's newest sequence and follows from there."""
    global _reader
    settings = configure()
    if settings.store == "postgres":
        _reader = postgres_reader(settings)
    else:
        _reader = MemoryReader()
        log.warning("The store is in memory - nothing is recorded there at run time; set %s=postgres to read the "
                    "question record and the transcripts the main flow and the LLM helper write", STORE)
    try:
        await _reader.open()
        _feed.cursor = await _reader.latest()
    except QuestionError:
        raise
    except Exception as exc:
        raise QuestionError(f"the {settings.store} store could not open - {failure(exc)}") from exc
    _feed.start(settings.poll_ms)
    log.info("agent-question ready - store %s (feed from %d), stream %s, poll %d ms, auth %s",
             _reader.name, _feed.cursor, EVENTS_PATH, settings.poll_ms, settings.auth)


async def close_streams() -> None:
    """The application is stopping (``Platform.on_shutdown``): end every connected device's stream, so the
    host can close its connections and exit within a container manager's grace period; a device reconnects to
    the next instance from its last sequence and misses nothing."""
    _wakeups.close()


async def shutdown() -> None:
    global _http
    for task in list(_streams):
        task.cancel()
    await _feed.stop()
    if _reader is not None:
        await _reader.close()
    if _http is not None and not _http.closed:
        await _http.close()
    _http = None


def _attach() -> None:
    # the person's side is rest.yaml entries behind this module's functions - no route is mounted
    platform.on_startup(boot)
    platform.on_shutdown(close_streams)
    platform.on_cleanup(shutdown)


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
