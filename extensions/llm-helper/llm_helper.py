"""
The LLM helper - a dedicated polyglot function host for the AI nodes of the
agent-orchestration experiment: ``llm.chat``, ``llm.stream`` and ``llm.health``, on the
official Anthropic SDK. The engines stay LLM-free: a graph or a flow reaches these routes
through declarative Event-over-HTTP like any other function, so the certified model decides
control flow while the LLM advises within it.

Run:  pip install 'mercury-composable[llm]'
      mercury-serve extensions/llm-helper/llm_helper.py

The credential comes from the environment, never from a config file: ANTHROPIC_API_KEY for
the Claude API, the AWS credential chain (or AWS_BEARER_TOKEN_BEDROCK) for AWS Bedrock.
Settings live in resources/application.yml (or -Dkey=value): llm.backend (anthropic or
bedrock), llm.aws.api (mantle or runtime), llm.aws.region, llm.model, llm.max.tokens,
llm.timeout.ms, llm.max.retries, llm.fallbacks, llm.effort, and for the agent loop's turns
llm.transcript.store (memory or redis), llm.transcript.ttl, llm.transcript.retention and the
llm.transcript.redis.* connection keys; the optional transcript audit store (RFC-0012) adds
llm.transcript.audit (postgres), llm.transcript.audit.retention, .grace, .sweep.ms, .single (a
call without tools recorded too, RFC-0015) and the llm.transcript.audit.postgres.* connection
keys; llm.attachments.root, .max.count and .max.bytes let a single llm.chat call name images and
PDF pages by reference (RFC-0016). The contract, the keys and the backends are documented in
README.md next to this file. The Node.js twin (mercury-nodejs
extensions/llm-helper) speaks the same contract, and both are pinned by shared vector files.

Then map the routes from a Mercury engine application (event-over-http.yaml):

    event:
      http:
        - route: 'llm.chat'
          target: 'http://127.0.0.1:8086/api/event'
        - route: 'llm.stream'
          target: 'http://127.0.0.1:8086/api/event'
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

try:
    import anthropic
except ImportError as missing_sdk:  # the SDK is this app's one dependency - teach the fix
    raise SystemExit(
        "The LLM helper needs the Anthropic SDK - pip install 'mercury-composable[llm]'"
    ) from missing_sdk

from mercury_composable import (
    AppException,
    AsyncHttpRequest,
    Body,
    EventEnvelope,
    EventStreamWriter,
    PostOffice,
    UploadedFile,
    annotate_trace,
    app_config,
    default_registry,
    get_logger,
    get_trace,
    iso_utc,
    platform,
    preload,
)

log = get_logger(__name__)

# --- the contract's defaults and limits ----------------------------------------------

DEFAULT_PROVIDER = "anthropic"
DEFAULT_BACKEND = "anthropic"
BEDROCK_BACKEND = "bedrock"
# the two Bedrock surfaces: the Anthropic-shaped endpoint (the default) and the classic runtime
AWS_API_MANTLE = "mantle"
AWS_API_RUNTIME = "runtime"
AWS_APIS = (AWS_API_MANTLE, AWS_API_RUNTIME)
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT_MS = 60000
DEFAULT_MAX_RETRIES = 2
PROVIDERS = (DEFAULT_PROVIDER,)
BACKENDS = (DEFAULT_BACKEND, BEDROCK_BACKEND)
# the Anthropic-shaped surface spells a model id with the vendor prefix; a bare id gets it, a
# prefixed one is kept. The classic runtime takes an inference profile id exactly as the account
# lists it (they carry dates and versions no rule can derive), so there the id is sent as written
BEDROCK_MODEL_PREFIX = "anthropic."
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("user", "assistant")
# per-call params. Anything else is rejected: the current models take no sampling
# parameters, and a mistyped key must not vanish silently
PARAMS = ("provider", "model", "max_tokens", "timeout_ms", "effort", "stop_sequences")
TEXT_EVENT_STREAM = "text/event-stream"
# server-side refusal fallbacks, "default" form: the Claude API only, current models only
FALLBACKS_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")
MISSING_CREDENTIAL = "LLM provider credential missing - set ANTHROPIC_API_KEY in the environment"
MISSING_AWS_CREDENTIAL = (
    "AWS credential missing - configure the AWS credential chain (AWS_ACCESS_KEY_ID and "
    "AWS_SECRET_ACCESS_KEY, a profile, or a role) or set AWS_BEARER_TOKEN_BEDROCK"
)
MISSING_AWS_REGION = "AWS region missing - set llm.aws.region or AWS_REGION in the environment"
MISSING_BEDROCK_SDK = "The Bedrock backend needs boto3 - pip install 'mercury-composable[bedrock]'"
# the SDK raises a bare TypeError, before sending anything, when no credential resolves
CREDENTIAL_TYPE_ERROR = "Could not resolve authentication method"
# the SDK's Bedrock client raises a bare RuntimeError, while signing, when the AWS chain is empty:
# "Could not resolve AWS credentials from session" (its legacy client says "credentials from
# session" without "AWS"); matched without case on the words both carry
AWS_CREDENTIAL_RUNTIME_ERROR = ("could not resolve", "credentials from session")

# --- the loop contract's defaults and limits (RFC-0011) -----------------------------------
# a request that carries tools is one turn of an agent loop; the other loop fields need tools
LOOP_FIELDS = ("conversation", "turn", "results", "ttl", "retention")
LOOP_KEYS = ("tools", *LOOP_FIELDS)
TOOL_KEYS = ("name", "description", "input_schema", "strict")
# the API's rule for an advertised name: a route name such as orders.search is mapped by the
# loop's catalog to an API-safe name, never advertised
TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,128}")
CONVERSATION_ID = re.compile(r"[0-9a-f]{32}")
DURATION = re.compile(r"(\d+)([smhd]?)")
DURATION_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULT_TRANSCRIPT_TTL = "7d"
DEFAULT_TRANSCRIPT_RETENTION = "0"
STORE_MEMORY = "memory"
STORE_REDIS = "redis"
STORES = (STORE_MEMORY, STORE_REDIS)
# the store's key prefix, so a Redis ACL can confine this app's user to ~transcript:*
TRANSCRIPT_PREFIX = "transcript:"
DONE_STOPS = ("end_turn", "stop_sequence")
# a turn cut off by a limit: the loop names which one in its envelope
CAPS = {"max_tokens": "max_tokens", "model_context_window_exceeded": "context"}
CACHE_CONTROL = {"type": "ephemeral"}
MISSING_STORE = (
    "transcript store not configured - set llm.transcript.store "
    "(redis, or memory for a single process)"
)
MISSING_REDIS_SDK = (
    "The redis transcript store needs redis - pip install 'mercury-composable[redis]'"
)
STREAM_LOOP = "tools and the loop fields are not part of the streaming contract - use llm.chat"

# --- attachments on a single llm.chat call (RFC-0016) ------------------------------------------
# a request names a file inside one configured root and never carries one: the helper reads it,
# decides its kind by its first bytes and sends it as an image or a document block
ATTACHMENTS_ROOT = "llm.attachments.root"
ATTACHMENTS_MAX_COUNT = "llm.attachments.max.count"
ATTACHMENTS_MAX_BYTES = "llm.attachments.max.bytes"
# 20: the most image and document blocks a request carries before the provider tightens each
# image's dimension limit
DEFAULT_ATTACHMENTS_MAX_COUNT = 20
DEFAULT_ATTACHMENTS_MAX_BYTES = 20 * 1024 * 1024
ATTACHMENT_KEYS = ("path", "pages")
PAGES = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")
PDF_MEDIA_TYPE = "application/pdf"
# an image's limit as the provider counts it, base64-encoded: by the route to the models
IMAGE_MAX_ENCODED = 10 * 1024 * 1024
BEDROCK_IMAGE_MAX_ENCODED = 5 * 1024 * 1024
STREAM_ATTACHMENTS = "attachments are not part of the streaming contract - use llm.chat"
LOOP_ATTACHMENTS = "attachments are not part of the loop contract - a loop turn carries text"
ATTACHMENTS_SHAPE = "attachments must be a non-empty list of paths or {path, pages}"
MISSING_ATTACHMENTS_ROOT = "attachments not configured - set llm.attachments.root"
MISSING_PDF_SDK = (
    "attachments: pages needs pypdf - pip install 'mercury-composable[documents]'"
)

# --- the transcript audit store's defaults and limits (RFC-0012) ----------------------------
# a durable, queryable record of every model turn, written by the transcript.audit function behind
# a local event, never on the turn's path; PostgreSQL is the reference store, its schema published
AUDIT_ROUTE = "transcript.audit"
AUDIT_POSTGRES = "postgres"
AUDITS = (AUDIT_POSTGRES,)
DEFAULT_AUDIT_RETENTION = "365d"
DEFAULT_AUDIT_GRACE = "1d"
DEFAULT_AUDIT_SWEEP_MS = 60000
# the sweep's lock on Redis, under the store's prefix so one ACL covers it; never a conversation key
AUDIT_SWEEP_LOCK = TRANSCRIPT_PREFIX + "audit:sweep"
# the graph context the engines' loop sends on a turn, recorded at the first turn (null without)
GRAPH_ID_HEADER = "x-graph-id"
NODE_HEADER = "x-node"
MISSING_AUDIT = "transcript audit store not configured - set llm.transcript.audit (postgres)"
# a call without tools in the audit store (RFC-0015): a conversation of one turn, recorded only
# where the installation switched it on - on by itself it would multiply what the store holds
AUDIT_SINGLE = "llm.transcript.audit.single"
MISSING_SINGLE_STORE = (
    "llm.transcript.audit.single needs the working store - set llm.transcript.store "
    "(redis, or memory for a single process)"
)
MISSING_POSTGRES_SDK = (
    "The postgres transcript audit store needs psycopg - pip install 'mercury-composable[postgres]'"
)


def _prop(key: str, default: str | None = None) -> str | None:
    return app_config().get_property(key, default)


# --- the request ---------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    """A file a single call names (RFC-0016): its path inside the root and, for a PDF, the pages
    to send - ranges as the request named them, in its order - with the request's own text."""

    path: str
    pages: tuple[tuple[int, int], ...] | None = None
    pages_text: str | None = None


@dataclass(frozen=True)
class LlmRequest:
    """A validated request. The contract's one request surface, shared by both routes."""

    model: str
    max_tokens: int
    timeout_ms: int
    max_retries: int
    messages: list[dict[str, Any]]  # text turns; a turn with attachments holds content blocks
    system: str | None
    effort: str | None
    stop_sequences: list[str] | None
    schema: dict[str, Any] | None
    fallbacks: bool
    tools: list[dict[str, Any]] | None = None  # a loop turn's tools, as the API takes them
    attachments: tuple[Attachment, ...] = ()  # llm.chat only, never a loop turn


def _whole_number(name: str, value: Any, minimum: int) -> int:
    """A whole number from a JSON number or a config string, or a 400 naming the field."""
    valid = (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and value.strip().isdigit()
    )
    if not valid or int(value) < minimum:
        raise AppException(400, f"{name} must be a whole number >= {minimum}")
    return int(value)


def _setting(params: dict[str, Any], key: str, config_key: str, default: str) -> Any:
    """Precedence: the call's param, then the config key, then the built-in default."""
    value = params.get(key)
    if value is None:
        value = _prop(config_key, default)
    return value


def _turns(body: dict[str, Any]) -> list[dict[str, str]]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return [{"role": "user", "content": str(body["prompt"])}]
    turns: list[dict[str, str]] = []
    for index, turn in enumerate(messages):
        if not isinstance(turn, dict) or turn.get("role") not in ROLES:
            raise AppException(400, f"messages[{index}].role must be one of: {', '.join(ROLES)}")
        content = turn.get("content")
        if not isinstance(content, str) or not content:
            raise AppException(400, f"messages[{index}].content must be a non-empty string")
        turns.append({"role": str(turn["role"]), "content": content})
    return turns


def _params(body: dict[str, Any]) -> dict[str, Any]:
    """The call's params: a map holding only keys this contract supports."""
    raw = body.get("params")
    if raw is not None and not isinstance(raw, dict):
        raise AppException(400, "params must be a map")
    params: dict[str, Any] = dict(raw or {})
    unknown = sorted(key for key in params if key not in PARAMS)
    if unknown:
        raise AppException(
            400, f"unsupported params: {', '.join(unknown)} - supported: {', '.join(PARAMS)}"
        )
    return params


def _check_provider(params: dict[str, Any]) -> None:
    provider = str(_setting(params, "provider", "llm.provider", DEFAULT_PROVIDER)).lower()
    if provider not in PROVIDERS:
        raise AppException(
            400, f"unknown LLM provider '{provider}' - this helper serves: {', '.join(PROVIDERS)}"
        )


def _system(body: dict[str, Any]) -> str | None:
    system = body.get("system")
    if system is not None and not isinstance(system, str):
        raise AppException(400, "system must be a string")
    return system or None


def _effort(params: dict[str, Any]) -> str | None:
    effort = params.get("effort") or _prop("llm.effort")
    if effort is None:
        return None
    effort = str(effort).lower()
    if effort not in EFFORTS:
        raise AppException(400, f"params.effort must be one of: {', '.join(EFFORTS)}")
    return effort


def _stop_sequences(params: dict[str, Any]) -> list[str] | None:
    stops = params.get("stop_sequences")
    if stops is None:
        return None
    if not (isinstance(stops, list) and all(isinstance(s, str) for s in stops)):
        raise AppException(400, "params.stop_sequences must be a list of strings")
    return stops or None


def _schema(body: dict[str, Any], streaming: bool) -> dict[str, Any] | None:
    schema = body.get("schema")
    if schema is None:
        return None
    if streaming:
        raise AppException(400, "schema is not part of the streaming contract - use llm.chat")
    if not isinstance(schema, dict):
        raise AppException(400, "schema must be a JSON schema map")
    # a closed schema is what a bounded verdict wants
    return {"additionalProperties": False, **schema}


def _pages(index: int, raw: Any) -> tuple[tuple[tuple[int, int], ...], str]:
    """The pages of a PDF to send, as ranges in the order named, with the text as written. A
    mapping may carry a page number as a number. Whether a page exists is the document's to say,
    when it is read."""
    text = "".join(raw.split()) if isinstance(raw, str) else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        text = str(raw)
    malformed = AppException(
        400, f"attachments[{index}].pages must name pages from 1, such as 13 or 1-3,7"
    )
    if text is None or not PAGES.fullmatch(text):
        raise malformed
    ranges: list[tuple[int, int]] = []
    for part in text.split(","):
        first, _, last = part.partition("-")
        low, high = int(first), int(last or first)
        if low < 1 or high < low:
            raise malformed
        if any(low <= held_high and held_low <= high for held_low, held_high in ranges):
            raise AppException(400, f"attachments[{index}].pages names a page twice")
        ranges.append((low, high))
    return tuple(ranges), text


def _attachment(index: int, entry: Any) -> Attachment:
    """One attachment: a path, or a map with a path and the pages of a PDF."""
    pages: Any = None
    if isinstance(entry, str):
        path: Any = entry
    elif isinstance(entry, dict):
        unknown = sorted(str(key) for key in entry if key not in ATTACHMENT_KEYS)
        if unknown:
            raise AppException(
                400,
                f"attachments[{index}]: unsupported keys: {', '.join(unknown)} - "
                f"supported: {', '.join(ATTACHMENT_KEYS)}",
            )
        path, pages = entry.get("path"), entry.get("pages")
    else:
        raise AppException(400, f"attachments[{index}] must be a path or {{path, pages}}")
    if not isinstance(path, str) or not path.strip():
        raise AppException(
            400, f"attachments[{index}].path must be a path inside {ATTACHMENTS_ROOT}"
        )
    if pages is None:
        return Attachment(path)
    ranges, text = _pages(index, pages)
    return Attachment(path, ranges, text)


def _attachments(body: dict[str, Any]) -> tuple[Attachment, ...]:
    """The files a single call names (RFC-0016): the shape only. The files themselves are read
    by attach(), after every other check of the request has passed."""
    raw = body.get("attachments")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise AppException(400, ATTACHMENTS_SHAPE)
    limit = _whole_number(
        ATTACHMENTS_MAX_COUNT,
        _prop(ATTACHMENTS_MAX_COUNT, str(DEFAULT_ATTACHMENTS_MAX_COUNT)),
        1,
    )
    if len(raw) > limit:
        raise AppException(
            400, f"attachments: {len(raw)} files exceed {ATTACHMENTS_MAX_COUNT} ({limit})"
        )
    return tuple(_attachment(index, entry) for index, entry in enumerate(raw))


def _fallbacks_enabled() -> bool:
    mode = str(_prop("llm.fallbacks", "default")).lower()
    if mode not in ("default", "off"):
        raise AppException(500, "llm.fallbacks must be 'default' or 'off'")
    return mode == "default"


def prepare(body: Body, *, streaming: bool, continuation: bool = False) -> LlmRequest:
    """Validate a request body into an LlmRequest, or raise AppException(400).

    The checks run in a fixed order, the same in the Node.js twin: the loop fields on the
    streaming route and then attachments on it, body, params and provider, the numbers, the
    turns, system, effort, stop sequences, schema, the attachments' shape, the fallbacks switch.
    A loop turn after the first (``continuation``) carries no turns of its own: the transcript
    holds them.
    """
    if streaming and isinstance(body, dict) and any(key in body for key in LOOP_KEYS):
        raise AppException(400, STREAM_LOOP)
    if streaming and isinstance(body, dict) and "attachments" in body:
        raise AppException(400, STREAM_ATTACHMENTS)
    turns = body.get("messages") if isinstance(body, dict) else None
    has_turns = isinstance(turns, list) and bool(turns)
    if not isinstance(body, dict) or not (continuation or body.get("prompt") or has_turns):
        raise AppException(400, "missing 'prompt' or 'messages'")
    params = _params(body)
    _check_provider(params)
    model = str(_setting(params, "model", "llm.model", DEFAULT_MODEL))
    max_tokens = _whole_number(
        "params.max_tokens",
        _setting(params, "max_tokens", "llm.max.tokens", str(DEFAULT_MAX_TOKENS)),
        1,
    )
    timeout_ms = _whole_number(
        "params.timeout_ms",
        _setting(params, "timeout_ms", "llm.timeout.ms", str(DEFAULT_TIMEOUT_MS)),
        1,
    )
    max_retries = _whole_number(
        "llm.max.retries", _prop("llm.max.retries", str(DEFAULT_MAX_RETRIES)), 0
    )
    return LlmRequest(
        model=model,
        max_tokens=max_tokens,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
        messages=[] if continuation else _turns(body),
        system=_system(body),
        effort=_effort(params),
        stop_sequences=_stop_sequences(params),
        schema=_schema(body, streaming),
        attachments=_attachments(body),
        fallbacks=_fallbacks_enabled(),
    )


# --- the loop request: one turn of an agent loop (RFC-0011) ------------------------------
#
# The agent loop (llm.run, in the engines) calls llm.chat once per model turn. The transcript stays
# in this app's store and the loop holds only a reference to it, so no provider block ever crosses
# an engine codec, and the record is appended to, never edited - the current models' thinking
# blocks are bound to the conversation that produced them.


@dataclass(frozen=True)
class LoopTurn:
    """The validated loop fields of a turn. The rest of the request is an LlmRequest."""

    tools: list[dict[str, Any]]
    conversation: str | None  # None on the first turn
    turn: int  # the turn count the loop expects the record to hold
    results: list[dict[str, Any]] | None
    ttl_s: int
    retention_s: int


def _is_loop(body: dict[str, Any]) -> bool:
    return any(key in body for key in LOOP_KEYS)


def _tools(raw: Any) -> list[dict[str, Any]]:
    """The tools as the API takes them; the loop's catalog keeps the routes and approval marks."""
    if not isinstance(raw, list) or not raw:
        raise AppException(
            400, "tools must be a non-empty list of {name, description, input_schema}"
        )
    names: set[str] = set()
    return [_tool(index, tool, names) for index, tool in enumerate(raw)]


def _tool(index: int, tool: Any, names: set[str]) -> dict[str, Any]:
    """One tool as the API takes it, its name added to ``names``; the checks run in the order
    of the Node.js twin and name the field that is wrong."""
    if not isinstance(tool, dict):
        raise AppException(400, f"tools[{index}] must be a map")
    unknown = sorted(str(key) for key in tool if key not in TOOL_KEYS)
    if unknown:
        raise AppException(
            400,
            f"tools[{index}]: unsupported keys: {', '.join(unknown)} - "
            f"supported: {', '.join(TOOL_KEYS)}",
        )
    name = tool.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        raise AppException(400, f"tools[{index}].name must match ^[a-zA-Z0-9_-]{{1,128}}$")
    if name in names:
        raise AppException(400, f"tools[{index}].name '{name}' is not unique")
    names.add(name)
    description = tool.get("description")
    if not isinstance(description, str) or not description:
        raise AppException(400, f"tools[{index}].description must be a non-empty string")
    input_schema = tool.get("input_schema")
    if not isinstance(input_schema, dict):
        raise AppException(400, f"tools[{index}].input_schema must be a JSON schema map")
    strict = tool.get("strict")
    if strict is not None and not isinstance(strict, bool):
        raise AppException(400, f"tools[{index}].strict must be true or false")
    entry: dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
    }
    if strict:
        entry["strict"] = True
    return entry


def _conversation(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not CONVERSATION_ID.fullmatch(raw):
        raise AppException(400, "conversation must be the reference a previous turn returned")
    return raw


def _results(raw: Any) -> list[dict[str, Any]] | None:
    """The answers to the previous turn's calls: {id, output} or {id, error}."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise AppException(400, "results must be a list of {id, output} or {id, error}")
    for index, result in enumerate(raw):
        if not isinstance(result, dict):
            raise AppException(400, f"results[{index}] must be a map")
        unknown = sorted(str(key) for key in result if key not in ("id", "output", "error"))
        if unknown:
            raise AppException(
                400,
                f"results[{index}]: unsupported keys: {', '.join(unknown)} - supported: "
                "id, output, error",
            )
        call_id = result.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise AppException(400, f"results[{index}].id must be a call id")
        if ("output" in result) == ("error" in result):
            raise AppException(400, f"results[{index}] must carry either output or error")
    return raw


def _duration(name: str, value: Any, minimum: int) -> int:
    """Seconds from a whole number or a duration such as 30m, 12h or 2d, or a 400 naming it."""
    text = value.strip() if isinstance(value, str) else None
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    match = DURATION.fullmatch(text) if text is not None else None
    seconds = int(match.group(1)) * DURATION_SECONDS[match.group(2)] if match else -1
    if seconds < minimum:
        raise AppException(
            400,
            f"{name} must be a duration such as 30m, 12h or 2d (seconds, s, m, h, d) >= {minimum}",
        )
    return seconds


def prepare_loop(body: dict[str, Any]) -> LoopTurn:
    """Validate the loop fields of a turn, or raise AppException(400).

    The checks run in a fixed order, the same in the Node.js twin: tools, conversation, turn,
    results, ttl, retention, then what a first turn and a later turn may carry.
    """
    if "tools" not in body:
        loop_field = next(key for key in LOOP_FIELDS if key in body)
        raise AppException(400, f"{loop_field} is a field of the loop contract - it needs tools")
    tools = _tools(body.get("tools"))
    conversation = _conversation(body.get("conversation"))
    raw_turn = body.get("turn")
    turn = None if raw_turn is None else _whole_number("turn", raw_turn, 0)
    results = _results(body.get("results"))
    ttl = body.get("ttl")
    if ttl is None:
        ttl = _prop("llm.transcript.ttl", DEFAULT_TRANSCRIPT_TTL)
    retention = body.get("retention")
    if retention is None:
        retention = _prop("llm.transcript.retention", DEFAULT_TRANSCRIPT_RETENTION)
    ttl_s = _duration("ttl", ttl, 1)
    retention_s = _duration("retention", retention, 0)
    _check_turn_shape(body, conversation, turn, results)
    return LoopTurn(tools, conversation, turn or 0, results, ttl_s, retention_s)


def _check_turn_shape(
    body: dict[str, Any], conversation: str | None, turn: int | None,
    results: list[dict[str, Any]] | None,
) -> None:
    """What a first turn and a later turn may carry, or raise AppException(400)."""
    if conversation is None:
        if turn:
            raise AppException(400, "turn must be 0 or absent on a first turn")
        if results is not None:
            raise AppException(400, "results answer a previous turn - a first turn carries none")
        return
    if turn is None:
        raise AppException(
            400, "turn is required with conversation - the turn count the record should hold"
        )
    if body.get("prompt") or body.get("messages"):
        raise AppException(
            400,
            "a later turn carries results, never prompt or messages - the transcript "
            "holds the conversation",
        )


# --- the backend seam ------------------------------------------------------------------


def _same_model(model: str) -> str:
    """The model id as the Claude API takes it; another route may spell it differently."""
    return model


def _bedrock_model(model: str) -> str:
    """The Anthropic-shaped surface: a bare model id gets the prefix it expects
    (``anthropic.claude-opus-5-5``); an id already spelled for Bedrock is sent as written. The
    classic runtime surface sends every id as written (``_same_model``)."""
    return BEDROCK_MODEL_PREFIX + model if model.startswith("claude-") else model


def _anthropic_credentials(client: Any) -> str | None:
    """The Claude API client: a key, a token, an OAuth profile or a custom auth, all local."""
    names = ("api_key", "auth_token", "credentials", "custom_auth")
    if any(getattr(client, name, None) for name in names):
        return None
    return MISSING_CREDENTIAL


def _bedrock_credentials(client: Any) -> str | None:
    """The Bedrock client: a Bedrock API key, else what the AWS credential chain resolves - the
    environment, a profile, a role; on an EC2 or ECS host that includes the instance metadata
    service, the one lookup the chain makes outside the process. No token is spent."""
    if getattr(client, "api_key", None):
        return None
    import boto3  # the [bedrock] extra; get_backend() checked that it is installed

    # only the options the client carries: boto3 fills the rest from its own chain
    options = {
        "profile_name": getattr(client, "aws_profile", None),
        "region_name": getattr(client, "aws_region", None),
        "aws_access_key_id": getattr(client, "aws_access_key", None),
        "aws_secret_access_key": getattr(client, "aws_secret_key", None),
        "aws_session_token": getattr(client, "aws_session_token", None),
    }
    session = boto3.Session(**{key: value for key, value in options.items() if value is not None})
    return None if session.get_credentials() else MISSING_AWS_CREDENTIAL


@dataclass(frozen=True)
class Backend:
    """One way to reach Claude: the client, and what this route to the model supports - how it
    spells a model id, whether server-side fallbacks exist, and what a missing credential looks
    like. Nothing else in this module knows which route answered, so the routes, the contract
    and the vector file are the same on every backend (see README.md, "Backends").
    """

    name: str
    client: Any
    supports_fallbacks: bool
    provider_model: Callable[[str], str] = _same_model
    credentials: Callable[[Any], str | None] = _anthropic_credentials
    api: str | None = None  # the Bedrock surface in use, reported by llm.health
    image_max_encoded: int = IMAGE_MAX_ENCODED  # an attached image's limit, base64-encoded

    def credential_problem(self) -> str | None:
        """What is missing for a call to be sent, or None. No token is spent."""
        return self.credentials(self.client)


def backend_for(name: str, client: Any, aws_api: str = AWS_API_MANTLE) -> Backend:
    """The Backend of a route over a client: get_backend() builds the client, tests inject one.
    For Bedrock, aws_api names the surface, which decides how a model id is spelled."""
    if name == BEDROCK_BACKEND:
        return Backend(
            BEDROCK_BACKEND,
            client,
            supports_fallbacks=False,  # server-side refusal fallbacks are not offered on Bedrock
            provider_model=_bedrock_model if aws_api == AWS_API_MANTLE else _same_model,
            credentials=_bedrock_credentials,
            api=aws_api,
            image_max_encoded=BEDROCK_IMAGE_MAX_ENCODED,
        )
    return Backend(DEFAULT_BACKEND, client, supports_fallbacks=True)


_backend: Backend | None = None  # built on first use - module-level so tests can inject


def _aws_api() -> str:
    """The Bedrock surface a call goes to: the Anthropic-shaped endpoint (mantle, the default)
    or the classic runtime - an account may be enabled for one and not the other."""
    api = str(_prop("llm.aws.api", AWS_API_MANTLE) or AWS_API_MANTLE).lower()
    if api not in AWS_APIS:
        raise AppException(500, f"llm.aws.api must be one of: {', '.join(AWS_APIS)}")
    return api


def _bedrock_client(aws_api: str) -> Any:
    """AWS Bedrock through the SDK's client for the chosen surface: the AWS credential chain (or
    a Bedrock API key) signs each call, and the region comes from llm.aws.region or the AWS
    environment."""
    if importlib.util.find_spec("boto3") is None:
        raise AppException(503, MISSING_BEDROCK_SDK)
    # the SDK reaches its Bedrock clients lazily at the top level; the package path is the typed one
    from anthropic.lib.bedrock import AsyncAnthropicBedrock, AsyncAnthropicBedrockMantle

    factory = AsyncAnthropicBedrockMantle if aws_api == AWS_API_MANTLE else AsyncAnthropicBedrock
    region = _prop("llm.aws.region")
    try:
        return factory(aws_region=str(region) if region else None)
    except (anthropic.AnthropicError, ValueError) as exc:  # no region, and no base URL for one
        raise AppException(503, MISSING_AWS_REGION) from exc


def get_backend() -> Backend:
    global _backend
    backend = _backend
    if backend is None:
        name = str(_prop("llm.backend", DEFAULT_BACKEND) or DEFAULT_BACKEND).lower()
        if name not in BACKENDS:
            raise AppException(
                501, f"unknown LLM backend '{name}' - this helper serves: {', '.join(BACKENDS)}"
            )
        # credentials resolve per call, so the app starts (and reports its health) without one
        aws_api = _aws_api() if name == BEDROCK_BACKEND else AWS_API_MANTLE
        client = _bedrock_client(aws_api) if name == BEDROCK_BACKEND else anthropic.AsyncAnthropic()
        backend = backend_for(name, client, aws_api)
        _backend = backend
    return backend


def _plan(request: LlmRequest, backend: Backend) -> tuple[Any, dict[str, Any]]:
    """The SDK call for a request: the messages namespace to use and its keyword arguments."""
    client = backend.client.with_options(
        timeout=request.timeout_ms / 1000, max_retries=request.max_retries
    )
    kwargs: dict[str, Any] = {
        "model": backend.provider_model(request.model),
        "max_tokens": request.max_tokens,
        "messages": request.messages,
    }
    if request.system:
        kwargs["system"] = request.system
    if request.tools is not None:
        # a loop turn: tools and system render first and stay the same across turns, so one
        # marker caches them and the top-level one caches the conversation as it grows. No
        # stored block is ever marked - the transcript is sent back exactly as it was returned
        kwargs["tools"] = request.tools
        kwargs["cache_control"] = dict(CACHE_CONTROL)
        if request.system:
            kwargs["system"] = [
                {"type": "text", "text": request.system, "cache_control": dict(CACHE_CONTROL)}
            ]
    output: dict[str, Any] = {}
    if request.schema is not None:
        output["format"] = {"type": "json_schema", "schema": request.schema}
    if request.effort:
        output["effort"] = request.effort
    if output:
        kwargs["output_config"] = output
    if request.stop_sequences:
        kwargs["stop_sequences"] = request.stop_sequences
    # never on a loop turn: the record pins the model at the first turn, and a fallback would move
    # the conversation to another model whose replay rules edit the history
    if (
        request.fallbacks
        and request.tools is None
        and backend.supports_fallbacks
        and request.model.startswith(FALLBACK_MODEL_PREFIXES)
    ):
        kwargs["betas"] = [FALLBACKS_BETA]
        kwargs["fallbacks"] = "default"
        return client.beta.messages, kwargs
    return client.messages, kwargs


# --- attachments: the files a single call names (RFC-0016) --------------------------------------
#
# The request names a file and never carries one, so an event stays small and a file's bytes reach
# no log, trace or stored record. One root, with the agent tools' workspace rules: resolved once,
# a relative path only, the resolved path inside it, no hidden entry, a regular file. Every caller
# that can reach this app can have a file under the root sent to the provider and read back in
# the reply: the root is deployment configuration, certified with the deployment.

# --- the document intake: llm.upload behind a REST automation entry with upload: true -----
# the files of a multipart upload saved under this folder of the root, each under a new name
UPLOAD_FOLDER = "uploads"
UPLOAD_NO_FILES = (
    "upload: no file in the request - send multipart/form-data to an entry with upload: true"
)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")

_attachments_root: Path | None = None  # resolved on first use - module-level so tests can inject


def attachments_root() -> Path:
    """The one folder a request may name files in. Unset, every attachment is refused."""
    global _attachments_root
    root = _attachments_root
    if root is None:
        configured = _prop(ATTACHMENTS_ROOT)
        if not configured:
            raise AppException(503, MISSING_ATTACHMENTS_ROOT)
        root = Path(str(configured)).expanduser().resolve()
        if not root.is_dir():
            raise AppException(500, f"{ATTACHMENTS_ROOT} is not a folder")
        _attachments_root = root
    return root


def _hidden(parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") and part not in (".", "..") for part in parts)


def _attachment_file(root: Path, index: int, path: str) -> Path:
    """The file a request names, or a 400 that names the rule it breaks - never the host's own
    path. The resolved path decides, so a link that leads out of the root is outside it."""
    where = f"attachments[{index}]"
    named = Path(path)
    if named.is_absolute() or path.startswith("~"):
        raise AppException(400, f"{where}: an absolute path - name a file inside the root")
    try:
        resolved = (root / named).resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # a loop of links, a null byte
        raise AppException(400, f"{where}: not found") from exc
    if not resolved.is_relative_to(root):
        raise AppException(400, f"{where}: outside {ATTACHMENTS_ROOT}")
    if _hidden(named.parts) or _hidden(resolved.relative_to(root).parts):
        raise AppException(400, f"{where}: a hidden entry, which the root does not expose")
    if not resolved.exists():
        raise AppException(400, f"{where}: not found")
    if not resolved.is_file():
        raise AppException(400, f"{where}: not a file")
    return resolved


def _media_type(head: bytes) -> str | None:
    """A file's kind by its first bytes, never by its name."""
    if head.startswith(b"%PDF-"):
        return PDF_MEDIA_TYPE
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pdf(index: int, data: bytes, pages: tuple[tuple[int, int], ...] | None) -> bytes:
    """The PDF to send: the file as it is, or a new one that holds only the pages named, in the
    order named, so a long document costs the pages asked for. The library (the documents extra)
    is needed to cut; with it a whole document is also checked, and without it a whole document
    goes as it is and the provider judges it."""
    where = f"attachments[{index}]"
    if importlib.util.find_spec("pypdf") is None:
        if pages is None:
            return data
        raise AppException(503, MISSING_PDF_SDK)
    import pypdf  # the [documents] extra; checked above

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AppException(400, f"{where}: an encrypted PDF - the provider takes none")
        if pages is None:
            return data
        count = len(reader.pages)
        beyond = next((high for _low, high in pages if high > count), None)
        if beyond is not None:
            raise AppException(
                400, f"{where}.pages names page {beyond} of a document of {count}"
            )
        writer = pypdf.PdfWriter()
        for low, high in pages:
            for number in range(low, high + 1):
                writer.add_page(reader.pages[number - 1])
        cut = io.BytesIO()
        writer.write(cut)
        return cut.getvalue()
    except AppException:
        raise
    except Exception as exc:  # the library's own errors for a damaged file, of several types
        raise AppException(400, f"{where}: not a readable PDF") from exc


def _load_attachments(
    specs: tuple[Attachment, ...], root: Path, max_bytes: int, image_max_encoded: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Read the files (in a worker thread): the blocks the model gets, the references the audit
    keeps - the path, the kind, the size and the SHA-256 of the source file as it was read,
    never its bytes - and the bytes read. A file's size is taken before it is read, so a file
    over the limit is refused unread."""
    blocks: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    total = 0
    over = "attachments: more than {} bytes of files - {} ({})"
    for index, spec in enumerate(specs):
        where = f"attachments[{index}]"
        file = _attachment_file(root, index, spec.path)
        if total + file.stat().st_size > max_bytes:
            raise AppException(400, over.format(max_bytes, ATTACHMENTS_MAX_BYTES, max_bytes))
        with file.open("rb") as handle:
            data = handle.read(max_bytes - total + 1)  # bounded: a file may grow under the read
        total += len(data)
        if total > max_bytes:
            raise AppException(400, over.format(max_bytes, ATTACHMENTS_MAX_BYTES, max_bytes))
        media_type = _media_type(data[:16])
        if media_type is None:
            raise AppException(400, f"{where}: not a PNG, JPEG, GIF, WebP or PDF file")
        reference: dict[str, Any] = {"type": "attachment", "path": spec.path}
        if spec.pages_text is not None:
            reference["pages"] = spec.pages_text
        reference.update(
            media_type=media_type, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
        )
        if media_type == PDF_MEDIA_TYPE:
            kind, sent = "document", _pdf(index, data, spec.pages)
        else:
            if spec.pages is not None:
                raise AppException(400, f"{where}.pages needs a PDF - the file is {media_type}")
            encoded = 4 * ((len(data) + 2) // 3)
            if encoded > image_max_encoded:
                raise AppException(
                    400,
                    f"{where}: an image of {encoded} bytes base64-encoded - the backend's "
                    f"limit is {image_max_encoded}",
                )
            kind, sent = "image", data
        source = {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(sent).decode("ascii"),
        }
        blocks.append({"type": kind, "source": source})
        references.append(reference)
    return blocks, references, total


@dataclass(frozen=True)
class Attached:
    """A request with its files read: what the model gets, and what the audit may keep."""

    request: LlmRequest  # the last turn holds the blocks, then the turn's text
    recorded: LlmRequest  # the same turn with a reference in place of each file
    count: int
    size: int


async def attach(request: LlmRequest, backend: Backend) -> Attached:
    """Read the files a call names and put them before the text of the last turn, which must be
    the user's. The files are read off the event loop; the caller's deadline covers the read."""
    last = request.messages[-1]
    if last["role"] != "user":
        raise AppException(400, "attachments go with the last turn, which must be the user's")
    root = attachments_root()
    max_bytes = _whole_number(
        ATTACHMENTS_MAX_BYTES,
        _prop(ATTACHMENTS_MAX_BYTES, str(DEFAULT_ATTACHMENTS_MAX_BYTES)),
        1,
    )
    blocks, references, size = await asyncio.to_thread(
        _load_attachments, request.attachments, root, max_bytes, backend.image_max_encoded
    )
    text = {"type": "text", "text": last["content"]}
    before = request.messages[:-1]
    return Attached(
        replace(request, messages=[*before, {"role": "user", "content": [*blocks, text]}]),
        replace(request, messages=[*before, {"role": "user", "content": [*references, text]}]),
        len(blocks),
        size,
    )


async def attachments_boot() -> None:
    """A root that is configured and is not a folder is a deployment error: stop by name at
    start, before a request finds it."""
    if not _prop(ATTACHMENTS_ROOT):
        return
    try:
        attachments_root()
    except AppException as exc:
        log.error("attachments cannot start - %s", exc.message)
        raise SystemExit(1) from exc
    log.info("attachments ready - %s is set", ATTACHMENTS_ROOT)


# --- one provider call, shared by both routes ------------------------------------------


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    request_id: str | None
    refusal: dict[str, Any] | None
    # every content block as the API returned it (thinking, text, tool_use), for a loop's record
    content: list[dict[str, Any]] = field(default_factory=list)
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def _block(block: Any) -> dict[str, Any]:
    """A content block as JSON, the way the SDK sends a returned block back: the fields the API
    set and nothing else. Stored, and later sent, exactly so."""
    to_dict: Any = getattr(block, "to_dict", None)
    if to_dict is not None:
        as_json: dict[str, Any] = to_dict(mode="json")
        return as_json
    if isinstance(block, dict):
        return dict(block)
    return dict(vars(block))


def _refusal(details: Any) -> dict[str, Any] | None:
    if details is None:
        return None
    return {
        "category": getattr(details, "category", None),
        "explanation": getattr(details, "explanation", None),
    }


def _request_id_of(stream: Any) -> str | None:
    """The API's request id, read from the response headers of an open stream."""
    headers: Any = getattr(getattr(stream, "response", None), "headers", None)
    if headers is None:
        return None
    return headers.get("request-id")


async def _complete(
    request: LlmRequest,
    backend: Backend,
    on_text: Callable[[str], None] | None = None,
) -> Completion:
    """Run the call on the SDK's streaming transport. The transport never trips the SDK's
    long-request guard, however large max_tokens is; llm.chat just takes the final message
    while llm.stream forwards the token batches as they arrive."""
    api, kwargs = _plan(request, backend)
    async with api.stream(**kwargs) as stream:
        request_id = _request_id_of(stream)
        if on_text is not None:
            async for text in stream.text_stream:
                if text:
                    on_text(text)
        message = await stream.get_final_message()
    usage = message.usage
    return Completion(
        text="".join(b.text for b in message.content if getattr(b, "type", "") == "text"),
        model=message.model,
        stop_reason=message.stop_reason or "",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        request_id=request_id or getattr(message, "_request_id", None),
        refusal=_refusal(getattr(message, "stop_details", None)),
        content=[_block(b) for b in message.content],
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
    )


# --- outcomes: a reply with nothing usable is an error, never an empty success ---------


def _judge(request: LlmRequest, done: Completion, has_content: bool) -> Any:
    """The parsed JSON for a schema request (None otherwise), or a 422 when the reply is
    refused, empty, or cut off before it could be used."""
    if done.stop_reason == "refusal" and (request.schema is not None or not has_content):
        category = (done.refusal or {}).get("category")
        raise AppException(
            422,
            "LLM refused the request - stop_reason=refusal"
            + (f", category={category}" if category else ""),
        )
    if not has_content:
        hint = (
            " (raise params.max_tokens or lower params.effort)"
            if done.stop_reason == "max_tokens"
            else ""
        )
        raise AppException(
            422,
            f"LLM reply is empty - stop_reason={done.stop_reason}, "
            f"output_tokens={done.output_tokens}{hint}",
        )
    if request.schema is None:
        return None
    try:
        return json.loads(done.text)
    except ValueError as exc:
        hint = (
            " (the reply was cut off - raise params.max_tokens)"
            if done.stop_reason == "max_tokens"
            else ""
        )
        raise AppException(
            422,
            "LLM reply is not valid JSON for the requested schema - "
            f"stop_reason={done.stop_reason}{hint}",
        ) from exc


# --- provider failures as the portable error contract ------------------------------------


def _detail(exc: anthropic.APIStatusError) -> str:
    """status, error type, message and request id - the same text on every runtime."""
    body = exc.body if isinstance(exc.body, dict) else {}
    raw = body.get("error")
    error: dict[str, Any] = raw if isinstance(raw, dict) else {}
    kind = error.get("type") or "error"
    message = error.get("message") or exc.message
    request_id = f" (request_id {exc.request_id})" if exc.request_id else ""
    return f"{exc.status_code} {kind}: {message}{request_id}"


def _status_failure(exc: anthropic.APIStatusError) -> AppException:
    """An HTTP error from the API, its status passed through (429 named for what it is)."""
    if isinstance(exc, anthropic.RateLimitError):
        return AppException(429, f"LLM provider rate limit - {_detail(exc)}")
    status = exc.status_code if 400 <= exc.status_code <= 599 else 502
    return AppException(status, f"LLM provider error - {_detail(exc)}")


def _aws_credential_error(exc: BaseException) -> bool:
    """The AWS credential chain resolved nothing: the SDK's bare RuntimeError while signing, or
    botocore's NoCredentialsError."""
    if type(exc).__name__ == "NoCredentialsError":
        return True
    text = str(exc).lower()
    return isinstance(exc, RuntimeError) and all(w in text for w in AWS_CREDENTIAL_RUNTIME_ERROR)


def _failure(exc: BaseException, timeout_ms: int) -> AppException | None:
    """Map a provider failure to AppException(status, message); None for anything else."""
    if isinstance(exc, AppException):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, anthropic.APITimeoutError)):
        return AppException(408, f"LLM request timed out after {timeout_ms} ms")
    if isinstance(exc, anthropic.APIStatusError):
        return _status_failure(exc)
    if isinstance(exc, anthropic.APIConnectionError):
        return AppException(503, f"LLM provider unreachable - {exc}")
    if isinstance(exc, TypeError) and CREDENTIAL_TYPE_ERROR in str(exc):
        return AppException(503, MISSING_CREDENTIAL)
    if _aws_credential_error(exc):
        return AppException(503, MISSING_AWS_CREDENTIAL)
    return None


def _annotate(done: Completion) -> None:
    """Usage rides the trace record, so telemetry shows what a call cost (never its text)."""
    annotate_trace("llm_model", done.model)
    annotate_trace("llm_stop_reason", done.stop_reason)
    annotate_trace("llm_input_tokens", str(done.input_tokens))
    annotate_trace("llm_output_tokens", str(done.output_tokens))
    if done.request_id:
        annotate_trace("llm_request_id", done.request_id)


def _summary(route: str, done: Completion, started: float) -> None:
    # model and usage only - a prompt or a completion never reaches a log
    log.info(
        "%s model=%s stop_reason=%s input_tokens=%d output_tokens=%d request_id=%s elapsed_ms=%d",
        route,
        done.model,
        done.stop_reason,
        done.input_tokens,
        done.output_tokens,
        done.request_id or "none",
        round((time.perf_counter() - started) * 1000),
    )


# --- the transcript store (RFC-0011) -------------------------------------------------------
#
# Only this app reads or writes a transcript. A record holds the model pinned at the first turn,
# the messages in the provider's format, the turn count and the ids of the calls the last turn
# made. A write is a compare-and-set on the turn count, so a duplicated turn can never fork a
# conversation, and every write sets the record's time to live. Since record version 2
# (RFC-0012) a record also carries the facts the audit rows need - saved inside the same
# compare-and-set, so a turn that lost the race leaves no facts behind - and the store keeps a
# third value beside the turn count and the record, the audited mark, owned by the audit function.


@dataclass(frozen=True)
class Transcript:
    model: str
    messages: list[dict[str, Any]]
    turn: int
    pending: list[str]  # the ids of the last turn's tool calls, in call order
    closed: bool = False  # the run ended; a retained record is kept for audit, never continued
    # the audit facts (record version 2): what the audit rows need and the transcript did not hold
    retention: int = 0  # the seconds the closing turn was given; the audit's check applies them
    correlation_id: str | None = None  # the business correlation id of the transaction
    graph_id: str | None = None  # the graph and the loop node, from the engines' two headers
    node: str | None = None
    system: str | None = None  # the first turn's system prompt, stored once
    tools: list[dict[str, Any]] | None = None  # the catalog as advertised at the first turn
    turns: list[dict[str, Any]] = field(default_factory=list)  # one entry of facts per turn

    def to_json(self) -> str:
        record = {
            "v": 2,
            "model": self.model,
            "turn": self.turn,
            "pending": self.pending,
            "closed": self.closed,
            "retention": self.retention,
            "correlation_id": self.correlation_id,
            "graph_id": self.graph_id,
            "node": self.node,
            "system": self.system,
            "tools": self.tools,
            "turns": self.turns,
            "messages": self.messages,
        }
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_json(text: str) -> Transcript:
        """A version 2 record, or a version 1 record (a conversation that was open when the helper
        was upgraded): its turns so far carry no facts, and the audit's check says so."""
        record = json.loads(text)
        return Transcript(
            model=record["model"],
            messages=record["messages"],
            turn=record["turn"],
            pending=record["pending"],
            closed=record["closed"],
            retention=int(record.get("retention") or 0),
            correlation_id=record.get("correlation_id"),
            graph_id=record.get("graph_id"),
            node=record.get("node"),
            system=record.get("system"),
            tools=record.get("tools"),
            turns=list(record.get("turns") or []),
        )


class TranscriptStore(Protocol):
    name: str

    async def load(self, conversation: str) -> Transcript | None:
        """The record, or None when it never existed, ended or expired."""
        ...

    async def save(
        self, conversation: str, expected_turn: int, record: Transcript, ttl_s: int
    ) -> bool:
        """Write the record if the stored one holds expected_turn (0: none may exist yet), with
        ttl_s to live; ttl_s 0 deletes it instead. False when another write came first."""
        ...

    # the audit half (RFC-0012): the audited mark is the audit function's, never the turn's

    async def audited(self, conversation: str) -> int:
        """The highest turn the audit store is known to hold for the record; 0 when none."""
        ...

    async def mark_audited(self, conversation: str, turn: int) -> None:
        """Set the audited mark; a record that is gone stays gone, and its time to live stays."""
        ...

    async def release(self, conversation: str, retention_s: int) -> None:
        """The record's time to live becomes retention_s; 0 deletes it at once."""
        ...

    async def unaudited(self) -> list[str]:
        """The conversations whose audited mark is below their turn count - the sweep's work."""
        ...

    async def sweep_lock(self, interval_ms: int, /) -> bool:
        """Claim this interval's sweep for this process; False when another replica holds it."""
        ...


class MemoryStore:
    """One process: for development and tests. A second replica cannot see these records."""

    name = STORE_MEMORY

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # expiry, turn, record, audited
        self._records: dict[str, tuple[float, int, str, int]] = {}

    def _live(self, conversation: str) -> tuple[float, int, str, int] | None:
        entry = self._records.get(conversation)
        if entry is not None and entry[0] <= self._clock():
            del self._records[conversation]
            return None
        return entry

    async def load(self, conversation: str) -> Transcript | None:
        entry = self._live(conversation)
        return None if entry is None else Transcript.from_json(entry[2])

    async def save(
        self, conversation: str, expected_turn: int, record: Transcript, ttl_s: int
    ) -> bool:
        # no await between the check and the write, so the event loop makes it atomic
        entry = self._live(conversation)
        held = 0 if entry is None else entry[1]
        if held != expected_turn or (entry is not None and expected_turn == 0):
            return False
        if ttl_s == 0:
            self._records.pop(conversation, None)
        else:
            audited = 0 if entry is None else entry[3]  # the compare-and-set never touches it
            self._records[conversation] = (
                self._clock() + ttl_s, record.turn, record.to_json(), audited
            )
        return True

    async def audited(self, conversation: str) -> int:
        entry = self._live(conversation)
        return 0 if entry is None else entry[3]

    async def mark_audited(self, conversation: str, turn: int) -> None:
        entry = self._live(conversation)
        if entry is not None:
            self._records[conversation] = (entry[0], entry[1], entry[2], turn)

    async def release(self, conversation: str, retention_s: int) -> None:
        entry = self._live(conversation)
        if entry is None:
            return
        if retention_s == 0:
            del self._records[conversation]
        else:
            self._records[conversation] = (self._clock() + retention_s, *entry[1:])

    async def unaudited(self) -> list[str]:
        lagging: list[str] = []
        for conversation in list(self._records):
            entry = self._live(conversation)
            if entry is not None and entry[3] < entry[1]:
                lagging.append(conversation)
        return lagging

    async def sweep_lock(self, _interval_ms: int) -> bool:
        return True  # one process sweeps its own records


# one script, one key: atomic on a standalone server and safe on a cluster. It writes the turn
# count and the record and sets the time to live - never the audited mark, which the audit
# function owns, so the two writers share a key and not a field
REDIS_SAVE = """
local held = redis.call('HGET', KEYS[1], 'turn')
if ARGV[1] == '0' then
  if held then return 0 end
elseif held ~= ARGV[1] then
  return 0
end
if ARGV[4] == '0' then
  redis.call('DEL', KEYS[1])
else
  redis.call('HSET', KEYS[1], 'turn', ARGV[2], 'record', ARGV[3])
  redis.call('EXPIRE', KEYS[1], ARGV[4])
end
return 1
"""

# the audited mark, set only on a record that still exists: a bare HSET would resurrect a released
# record as a hash with no time to live
REDIS_MARK_AUDITED = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], 'audited', ARGV[1])
end
return 1
"""


class RedisStore:
    """A hash per conversation under transcript:{conversation}: the turn count, the record and
    the audited mark."""

    name = STORE_REDIS

    def __init__(self, client: Any) -> None:
        self._client = client  # redis.asyncio.Redis with decode_responses=True

    async def load(self, conversation: str) -> Transcript | None:
        text = await self._client.hget(TRANSCRIPT_PREFIX + conversation, "record")
        return None if text is None else Transcript.from_json(text)

    async def save(
        self, conversation: str, expected_turn: int, record: Transcript, ttl_s: int
    ) -> bool:
        key = TRANSCRIPT_PREFIX + conversation
        args = (str(expected_turn), str(record.turn), record.to_json(), str(ttl_s))
        return bool(await self._client.eval(REDIS_SAVE, 1, key, *args))

    async def audited(self, conversation: str) -> int:
        value = await self._client.hget(TRANSCRIPT_PREFIX + conversation, "audited")
        return int(value) if value else 0

    async def mark_audited(self, conversation: str, turn: int) -> None:
        await self._client.eval(REDIS_MARK_AUDITED, 1, TRANSCRIPT_PREFIX + conversation, str(turn))

    async def release(self, conversation: str, retention_s: int) -> None:
        key = TRANSCRIPT_PREFIX + conversation
        if retention_s == 0:
            await self._client.delete(key)
        else:
            await self._client.expire(key, retention_s)

    async def unaudited(self) -> list[str]:
        """Two hash fields per live record and no database query; the sweep's lock and anything
        else under the prefix that is not a conversation key are skipped."""
        lagging: list[str] = []
        async for key in self._client.scan_iter(match=TRANSCRIPT_PREFIX + "*", count=100):
            conversation = str(key)[len(TRANSCRIPT_PREFIX):]
            if not CONVERSATION_ID.fullmatch(conversation):
                continue
            turn, audited = await self._client.hmget(key, "turn", "audited")
            if turn is not None and int(audited or 0) < int(turn):
                lagging.append(conversation)
        return lagging

    async def sweep_lock(self, interval_ms: int) -> bool:
        # SET NX PX: one sweep per interval across the replicas; the lock expires with the interval
        claimed = await self._client.set(
            AUDIT_SWEEP_LOCK, uuid.uuid4().hex, nx=True, px=max(1, interval_ms)
        )
        return bool(claimed)


def _redis_store() -> RedisStore:
    """The Redis client from the llm.transcript.redis.* keys - the engines' redis.* connection
    set under this store's own prefix. It connects on the first command."""
    if importlib.util.find_spec("redis") is None:
        raise AppException(503, MISSING_REDIS_SDK)
    import redis.asyncio as redis_async  # the [redis] extra; checked above

    timeout_s = (
        _whole_number(
            "llm.transcript.redis.timeout.ms", _prop("llm.transcript.redis.timeout.ms", "5000"), 1
        )
        / 1000
    )
    client = redis_async.Redis(
        host=str(_prop("llm.transcript.redis.host", "127.0.0.1")),
        port=_whole_number(
            "llm.transcript.redis.port", _prop("llm.transcript.redis.port", "6379"), 1
        ),
        username=_prop("llm.transcript.redis.username") or None,
        password=_prop("llm.transcript.redis.password") or None,
        ssl=str(_prop("llm.transcript.redis.ssl", "false")).lower() == "true",
        db=_whole_number(
            "llm.transcript.redis.database", _prop("llm.transcript.redis.database", "0"), 0
        ),
        socket_timeout=timeout_s,
        socket_connect_timeout=timeout_s,
        decode_responses=True,
    )
    return RedisStore(client)


_store: TranscriptStore | None = None  # built on first use - module-level so tests can inject


def get_store() -> TranscriptStore:
    global _store
    store = _store
    if store is None:
        name = str(_prop("llm.transcript.store", "") or "").lower()
        if not name:
            raise AppException(503, MISSING_STORE)
        if name not in STORES:
            raise AppException(500, f"llm.transcript.store must be one of: {', '.join(STORES)}")
        store = MemoryStore() if name == STORE_MEMORY else _redis_store()
        _store = store
    return store


def _store_failure(exc: Exception) -> AppException:
    """A store that cannot be reached is the deployment's problem, named as such."""
    return AppException(503, f"transcript store unreachable - {type(exc).__name__}: {exc}")


def _new_conversation_id() -> str:
    return uuid.uuid4().hex


# --- one loop turn ---------------------------------------------------------------------------


def _check_turn(
    conversation: str, record: Transcript | None, loop: LoopTurn, body: dict[str, Any]
) -> Transcript:
    """The append check: the record exists, is open, holds the turn the loop expects, and keeps
    its model."""
    if record is None:
        raise AppException(404, f"conversation {conversation} not found - it ended or expired")
    if record.closed:
        raise AppException(409, f"conversation {conversation} has ended - start a new one")
    if record.turn != loop.turn:
        raise AppException(
            409,
            f"conversation {conversation} holds turn {record.turn}, not {loop.turn} - "
            "a stale or duplicated turn",
        )
    params = body.get("params")
    model = params.get("model") if isinstance(params, dict) else None
    if model is not None and str(model) != record.model:
        raise AppException(400, f"conversation {conversation} is pinned to model {record.model}")
    return record


def _result_text(value: Any) -> str:
    """A tool's output or error as tool_result text; JSON is written compactly, keys in order."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tool_results(pending: list[str], results: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The one user message that answers the last turn's calls, in call order."""
    if results is None:
        raise AppException(
            400, f"results required - the conversation waits for {len(pending)} tool result(s)"
        )
    by_id: dict[str, dict[str, Any]] = {}
    for result in results:
        call_id = result["id"]
        if call_id in by_id:
            raise AppException(400, f"results: duplicate id '{call_id}'")
        if call_id not in pending:
            raise AppException(
                400,
                f"results: unknown id '{call_id}' - the conversation waits for: "
                f"{', '.join(pending)}",
            )
        by_id[call_id] = result
    missing = [call_id for call_id in pending if call_id not in by_id]
    if missing:
        raise AppException(400, f"results: missing id(s): {', '.join(missing)}")
    blocks: list[dict[str, Any]] = []
    for call_id in pending:
        result = by_id[call_id]
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id}
        if "error" in result:
            block["content"] = _result_text(result["error"])
            block["is_error"] = True
        else:
            block["content"] = _result_text(result["output"])
        blocks.append(block)
    return {"role": "user", "content": blocks}


def _loop_outcome(request: LlmRequest, done: Completion) -> dict[str, Any]:
    """The reply's outcome for the turn's stop reason: done, tool_use, refusal or cap. A cut-off
    turn returns no calls, so none of its truncated calls can run."""
    stop = done.stop_reason
    if stop == "tool_use":
        calls = [
            {"id": block["id"], "name": block["name"], "input": block["input"]}
            for block in done.content
            if block.get("type") == "tool_use"
        ]
        if not calls:
            raise AppException(502, "LLM stop_reason=tool_use carried no tool call")
        return {"status": "tool_use", "calls": calls}
    if stop in DONE_STOPS:
        data = _judge(request, done, has_content=bool(done.text))
        if request.schema is not None:
            return {"status": "done", "data": data}
        return {"status": "done", "text": done.text}
    if stop == "refusal":
        return {"status": "refusal", "refusal": {"category": (done.refusal or {}).get("category")}}
    if stop in CAPS:
        return {"status": "cap", "cap": CAPS[stop]}
    raise AppException(502, f"LLM stop_reason={stop} is not part of the loop contract")


def _correlation_id(headers: dict[str, str]) -> str | None:
    """The business correlation id of the transaction: the trace context's, which the host fills
    from the engine-managed tag at delivery, else the read-only header view of the same value."""
    info = get_trace()
    if info is not None and info.my_correlation_id:
        return info.my_correlation_id
    return headers.get("my_correlation_id") or None


def _first_record(request: LlmRequest, loop: LoopTurn, headers: dict[str, str]) -> Transcript:
    """A new conversation's record: the model pinned, the first turn's messages, and what the
    audit rows need (record version 2) - the correlation id, the graph and the node the engines'
    loop sends as headers (null when a caller sends none), the system prompt and the catalog once."""
    return Transcript(
        request.model,
        list(request.messages),
        0,
        [],
        correlation_id=_correlation_id(headers),
        graph_id=headers.get(GRAPH_ID_HEADER) or None,
        node=headers.get(NODE_HEADER) or None,
        system=request.system,
        tools=loop.tools,
    )


def _turn_facts(
    turn: int,
    status: str,
    done: Completion,
    bounds: tuple[str, str],
    headers: dict[str, str],
    indexes: list[int],
) -> dict[str, Any]:
    """The facts of one turn, saved in the record inside the compare-and-set: the identity from
    the trace context, the model call's UTC bounds, its outcome and usage, and where the turn's
    request and response sit in the messages (``[first request message, response]``)."""
    info = get_trace()
    return {
        "turn": turn,
        "trace_id": info.trace_id if info else None,
        "trace_path": info.trace_path if info else None,
        "correlation_id": _correlation_id(headers),
        "started_at": bounds[0],
        "ended_at": bounds[1],
        "status": status,
        "stop_reason": done.stop_reason,
        "request_id": done.request_id,
        "usage": {
            "input_tokens": done.input_tokens,
            "output_tokens": done.output_tokens,
            "cache_creation_input_tokens": done.cache_creation_input_tokens,
            "cache_read_input_tokens": done.cache_read_input_tokens,
        },
        "messages": indexes,
    }


def _record_ttl(loop: LoopTurn, outcome: dict[str, Any], closed: bool, audited: bool) -> int:
    """How long the saved record lives. A run the model ended takes the retention; a run the loop
    stops (a cut-off turn, or the loop's own limits) keeps its time to live and expires with it.
    With an audit store, a closed record waits for its integrity check instead, which releases it
    to the retention once the audit holds every turn (RFC-0012, section 4)."""
    if audited and closed:
        return _audit_grace_s()
    model_ended = outcome["status"] in ("done", "refusal") or outcome.get("cap") == "context"
    return loop.retention_s if model_ended else loop.ttl_s


async def _loop_turn(headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    """One model turn of an agent loop: load and check the record, append the tool results, call
    the model, append its turn and its facts, save under the compare-and-set, and tell the audit
    function when one is configured - off the turn's path."""
    if "attachments" in body:
        # a transcript keeps every turn, so it would have to keep the files or references whose
        # files may change between turns: a single call only (RFC-0016)
        raise AppException(400, LOOP_ATTACHMENTS)
    loop = prepare_loop(body)
    request = prepare(body, streaming=False, continuation=loop.conversation is not None)
    store = get_store()
    audited = audit_configured()
    try:
        if loop.conversation is None:
            conversation = _new_conversation_id()
            record = _first_record(request, loop, headers)
            messages = list(record.messages)
            first = 0  # a first turn's request is every message it sent
        else:
            conversation = loop.conversation
            record = _check_turn(conversation, await store.load(conversation), loop, body)
            messages = [*record.messages, _tool_results(record.pending, loop.results)]
            first = len(record.messages)  # the one message of tool results
    except AppException:
        raise
    except Exception as exc:  # the store's own failure - a connection, a timeout
        raise _store_failure(exc) from exc
    request = replace(request, model=record.model, messages=messages, tools=loop.tools)
    started = time.perf_counter()
    started_at = iso_utc()
    try:
        done = await asyncio.wait_for(
            _complete(request, get_backend()), timeout=request.timeout_ms / 1000
        )
    except Exception as exc:  # anything unmapped is re-raised below
        failure = _failure(exc, request.timeout_ms)
        if failure is None:
            raise
        log.warning("llm.chat failed - status=%d turn=%d", failure.status, record.turn + 1)
        raise failure from exc
    ended_at = iso_utc()
    outcome = _loop_outcome(request, done)
    status = outcome["status"]
    calls = outcome.get("calls", [])
    turn = record.turn + 1
    closed = status != "tool_use"
    facts = _turn_facts(turn, status, done, (started_at, ended_at), headers, [first, len(messages)])
    saved = replace(
        record,
        messages=[*messages, {"role": "assistant", "content": done.content}],
        turn=turn,
        pending=[call["id"] for call in calls],
        closed=closed,
        retention=loop.retention_s,
        turns=[*record.turns, facts],
    )
    try:
        written = await store.save(
            conversation, record.turn, saved, _record_ttl(loop, outcome, closed, audited)
        )
    except Exception as exc:  # the store's own failure - a connection, a timeout
        raise _store_failure(exc) from exc
    if not written:
        raise AppException(
            409,
            f"conversation {conversation} moved on before turn {turn} was saved - "
            "a duplicated turn",
        )
    if audited:
        await _notify_audit(conversation, turn, closed)
    _annotate(done)
    annotate_trace("llm_status", status)
    annotate_trace("llm_turn", str(turn))
    annotate_trace("llm_cache_creation_input_tokens", str(done.cache_creation_input_tokens))
    annotate_trace("llm_cache_read_input_tokens", str(done.cache_read_input_tokens))
    # status, turn and usage only - no prompt, completion, tool argument or result reaches a log
    log.info(
        "llm.chat model=%s status=%s turn=%d stop_reason=%s input_tokens=%d output_tokens=%d "
        "cache_read_input_tokens=%d request_id=%s elapsed_ms=%d",
        done.model,
        status,
        turn,
        done.stop_reason,
        done.input_tokens,
        done.output_tokens,
        done.cache_read_input_tokens,
        done.request_id or "none",
        round((time.perf_counter() - started) * 1000),
    )
    reply: dict[str, Any] = {
        **outcome,
        "conversation": conversation,
        "turn": turn,
        "model": done.model,
        "stop_reason": done.stop_reason,
        "usage": {
            "input_tokens": done.input_tokens,
            "output_tokens": done.output_tokens,
            "cache_creation_input_tokens": done.cache_creation_input_tokens,
            "cache_read_input_tokens": done.cache_read_input_tokens,
        },
    }
    if done.request_id:
        reply["request_id"] = done.request_id
    return reply


# --- the transcript audit store (RFC-0012) --------------------------------------------------
#
# The working store's expiry covers the transaction; the audit store covers the record. Every turn
# is recorded, asynchronously: after the compare-and-set accepts a turn, llm.chat sends one local
# event to transcript.audit, a composable function of this process with ONE instance, so its
# mailbox orders the writes and the check of a conversation without a lock. The function reads the
# working record - the one place the content lives - and writes the rows the audit store lacks.
# The closing turn sends a second event behind its write, the integrity check, which repairs
# whatever a failed write left behind and only then releases the working record. A sweep sends a
# check for every record whose audited mark lags its turn count: the runs the loop stopped and
# the writes a dying process lost. A failure is logged by type and never refuses a turn.


class AuditStore(Protocol):
    name: str

    async def open(self) -> None:
        """Connect, at boot. A store that does not answer raises; the sweep repairs later."""
        ...

    async def close(self) -> None: ...

    async def turns(self, conversation: str) -> set[int]:
        """The turn numbers the store holds for a conversation."""
        ...

    async def write(self, conversation: dict[str, Any], turns: list[dict[str, Any]]) -> None:
        """Upsert the conversation row and insert the turn rows in one transaction. Idempotent: a
        turn row that exists is left as it is, the conversation row's status, turn count, times
        and totals are brought up to date, and the totals are recomputed from the turn rows."""
        ...


# the conversation row's token totals, recomputed from the turn rows on every write
TOKEN_COLUMNS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
)  # fmt: skip


class MemoryAuditStore:
    """The reference semantics in memory, for the tests and the twins' parity - never a configured
    store: an audit needs a durable, governed database."""

    name = "memory"

    def __init__(self) -> None:
        self.conversations: dict[str, dict[str, Any]] = {}
        self.turn_rows: dict[str, dict[int, dict[str, Any]]] = {}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def turns(self, conversation: str) -> set[int]:
        return set(self.turn_rows.get(conversation, {}))

    async def write(self, conversation: dict[str, Any], turns: list[dict[str, Any]]) -> None:
        key = str(conversation["conversation"])
        held = self.conversations.get(key)
        if held is None:
            held = dict(conversation)  # INSERT
            self.conversations[key] = held
        else:  # ON CONFLICT DO UPDATE of what a turn changes; the first-turn snapshot stays
            for column in ("status", "turns", "updated_at", "ended_at"):
                held[column] = conversation[column]
        rows = self.turn_rows.setdefault(key, {})
        for row in turns:
            rows.setdefault(int(row["turn"]), dict(row))  # ON CONFLICT DO NOTHING
        for column in TOKEN_COLUMNS:
            held[column] = sum(int(row[column]) for row in rows.values())


# the statements the reference store runs, with psycopg's placeholders. JSON travels as text cast
# to jsonb and a UTC stamp as ISO-8601 text cast to timestamptz, so the Node twin runs the same
# statements with its own placeholders and no driver-specific wrapper
SQL_TURNS = "SELECT turn FROM transcript_turn WHERE conversation = %s"
SQL_UPSERT_CONVERSATION = """
INSERT INTO transcript (conversation, correlation_id, graph_id, node, model, status, turns,
  started_at, updated_at, ended_at, expires_at, system, tools)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s::timestamptz,
  %s::timestamptz, %s, %s::jsonb)
ON CONFLICT (conversation) DO UPDATE SET status = EXCLUDED.status, turns = EXCLUDED.turns,
  updated_at = EXCLUDED.updated_at, ended_at = EXCLUDED.ended_at
""".strip()
SQL_INSERT_TURN = """
INSERT INTO transcript_turn (conversation, turn, trace_id, trace_path, correlation_id,
  started_at, ended_at, stop_reason, status, request_id, input_tokens, output_tokens,
  cache_creation_input_tokens, cache_read_input_tokens, request, response, calls)
VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s,
  %s::jsonb, %s::jsonb, %s::jsonb)
ON CONFLICT (conversation, turn) DO NOTHING
""".strip()
SQL_TOTALS = """
UPDATE transcript SET input_tokens = totals.input_tokens, output_tokens = totals.output_tokens,
  cache_creation_input_tokens = totals.cache_creation_input_tokens,
  cache_read_input_tokens = totals.cache_read_input_tokens
FROM (SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
  COALESCE(SUM(output_tokens), 0) AS output_tokens,
  COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
  COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens
  FROM transcript_turn WHERE conversation = %s) AS totals
WHERE transcript.conversation = %s
""".strip()


def _json_text(value: Any) -> str | None:
    """JSON for a jsonb column, compact and as the record holds it; None stays NULL."""
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _conversation_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["conversation"], row["correlation_id"], row["graph_id"], row["node"], row["model"],
        row["status"], row["turns"], row["started_at"], row["updated_at"], row["ended_at"],
        row["expires_at"], row["system"], _json_text(row["tools"]),
    )  # fmt: skip


def _turn_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["conversation"], row["turn"], row["trace_id"], row["trace_path"],
        row["correlation_id"], row["started_at"], row["ended_at"], row["stop_reason"],
        row["status"], row["request_id"], row["input_tokens"], row["output_tokens"],
        row["cache_creation_input_tokens"], row["cache_read_input_tokens"],
        _json_text(row["request"]), _json_text(row["response"]), _json_text(row["calls"]),
    )  # fmt: skip


class PostgresAuditStore:
    """The reference store: psycopg 3 over an asynchronous connection pool (the ``postgres``
    extra). The function connects as a writer that may insert, select and update the two tables
    and nothing else (schema/postgres.sql); people and the SQL browser read."""

    name = AUDIT_POSTGRES

    def __init__(self, pool: Any, timeout_s: float = 5.0) -> None:
        self._pool = pool  # psycopg_pool.AsyncConnectionPool, or a recording fake in the tests
        self._timeout_s = timeout_s

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=self._timeout_s)

    async def close(self) -> None:
        await self._pool.close()

    async def turns(self, conversation: str) -> set[int]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(SQL_TURNS, (conversation,))
            return {int(row[0]) for row in await cursor.fetchall()}

    async def write(self, conversation: dict[str, Any], turns: list[dict[str, Any]]) -> None:
        key = str(conversation["conversation"])
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(SQL_UPSERT_CONVERSATION, _conversation_params(conversation))
            for row in turns:
                await connection.execute(SQL_INSERT_TURN, _turn_params(row))
            await connection.execute(SQL_TOTALS, (key, key))


def _postgres_audit_store() -> PostgresAuditStore:
    """The pool from the llm.transcript.audit.postgres.* keys - the working store's connection set
    under the audit's own prefix. Nothing connects until open(); the password comes from the
    environment, never from a file."""
    if any(importlib.util.find_spec(name) is None for name in ("psycopg", "psycopg_pool")):
        raise AppException(503, MISSING_POSTGRES_SDK)
    from psycopg_pool import AsyncConnectionPool  # the [postgres] extra; checked above

    prefix = "llm.transcript.audit.postgres."
    timeout_ms = _whole_number(prefix + "timeout.ms", _prop(prefix + "timeout.ms", "5000"), 1)
    timeout_s = timeout_ms / 1000
    ssl = str(_prop(prefix + "ssl", "true")).lower() == "true"
    settings: dict[str, Any] = {
        "host": str(_prop(prefix + "host", "127.0.0.1")),
        "port": _whole_number(prefix + "port", _prop(prefix + "port", "5432"), 1),
        "dbname": str(_prop(prefix + "database", "mercury")),
        "user": _prop(prefix + "username") or None,
        "password": _prop(prefix + "password") or None,
        "sslmode": "require" if ssl else "disable",
        "connect_timeout": max(1, round(timeout_s)),
        # the server-side bound on a statement: a database that stalls (a paused container, a lock
        # held) frees the function's mailbox after the timeout instead of holding it for the
        # outage's length, as the live drive of 2026-11-17 showed a stopped one does by itself
        "options": f"-c statement_timeout={timeout_ms}",
    }
    pool = AsyncConnectionPool(
        kwargs={key: value for key, value in settings.items() if value is not None},
        min_size=1,
        max_size=4,
        open=False,
        timeout=timeout_s,
    )
    return PostgresAuditStore(pool, timeout_s)


_post_office = PostOffice()  # local sends to transcript.audit: no endpoint, the in-process bus
_audit: AuditStore | None = None  # built at boot - module-level so tests can inject
_sweep_task: asyncio.Task[None] | None = None


def audit_configured() -> bool:
    return bool(_prop("llm.transcript.audit"))


def get_audit_store() -> AuditStore:
    global _audit
    audit = _audit
    if audit is None:
        name = str(_prop("llm.transcript.audit", "") or "").lower()
        if not name:
            raise AppException(503, MISSING_AUDIT)
        if name not in AUDITS:
            raise AppException(500, f"llm.transcript.audit must be one of: {', '.join(AUDITS)}")
        audit = _postgres_audit_store()
        _audit = audit
    return audit


def _audit_grace_s() -> int:
    return _duration(
        "llm.transcript.audit.grace", _prop("llm.transcript.audit.grace", DEFAULT_AUDIT_GRACE), 1
    )


def _audit_retention_s() -> int:
    return _duration(
        "llm.transcript.audit.retention",
        _prop("llm.transcript.audit.retention", DEFAULT_AUDIT_RETENTION),
        0,
    )


def _audit_sweep_ms() -> int:
    return _whole_number(
        "llm.transcript.audit.sweep.ms",
        _prop("llm.transcript.audit.sweep.ms", str(DEFAULT_AUDIT_SWEEP_MS)),
        0,
    )


async def _audit_send(event: dict[str, Any]) -> None:
    """A local drop-n-forget event to transcript.audit over the in-process bus, the turn's trace
    inherited so the write shows as the function's own execution. A failure to send is logged by
    type and never refuses the turn: the sweep repairs what a lost event leaves behind."""
    try:
        ack = await _post_office.send(AUDIT_ROUTE, event)
    except Exception as exc:  # noqa: BLE001 - in-process; nothing here may fail the turn
        log.warning("%s event not sent - %s", AUDIT_ROUTE, type(exc).__name__)
        return
    if ack.has_error():
        log.warning("%s event not accepted - status=%d", AUDIT_ROUTE, ack.get_status())


async def _notify_audit(conversation: str, turn: int, closed: bool) -> None:
    """Tell transcript.audit, off the turn's path: the write of this turn and, behind it, the
    integrity check when the turn closed the run - one mailbox, one worker, so the check can
    never overtake the write it verifies."""
    await _audit_send({"conversation": conversation, "turn": turn})
    if closed:
        await _audit_send({"conversation": conversation, "check": True})


# --- a single call in the audit store (RFC-0015) ------------------------------------------------
#
# A call without tools keeps no working record, so the audit had nothing to read. With
# llm.transcript.audit.single on, the call becomes a conversation of ONE turn carried the way a
# loop's turns are: a closed version 2 record saved under a new reference with the audit's grace,
# and the one event a closed record needs, the check. transcript.audit is unchanged - the check
# writes the two rows from the record and releases it, and the sweep re-sends a check that failed.
# No content crosses the bus, and the reply never waits for the save.


def audit_single_configured() -> bool:
    return audit_configured() and str(_prop(AUDIT_SINGLE, "false")).lower() == "true"


def _single_status(stop_reason: str) -> str:
    """A single call's status by the loop's table: a refusal, a reply cut off at a limit, else
    done - the model's outcome, whatever the caller then received; the provider's own word stays
    in stop_reason."""
    if stop_reason == "refusal":
        return "refusal"
    return "cap" if stop_reason in CAPS else "done"


def _single_record(
    headers: dict[str, str], request: LlmRequest, done: Completion, bounds: tuple[str, str]
) -> Transcript:
    """The closed record of a single call: the messages as sent and the assistant's content
    blocks as returned, the model that answered (a server-side fallback may have), no tools -
    which a loop's record never lacks, so the audit row tells the two apart - and the one turn's
    facts. Retention zero: the check deletes the record once the audit holds the turn."""
    messages: list[dict[str, Any]] = [dict(turn) for turn in request.messages]
    facts = _turn_facts(
        1, _single_status(done.stop_reason), done, bounds, headers, [0, len(messages)]
    )
    return Transcript(
        done.model,
        [*messages, {"role": "assistant", "content": done.content}],
        1,
        [],
        closed=True,
        correlation_id=_correlation_id(headers),
        graph_id=headers.get(GRAPH_ID_HEADER) or None,
        node=headers.get(NODE_HEADER) or None,
        system=request.system,
        turns=[facts],
    )


# the saves in flight: a task is held here until it ends, so it cannot be collected mid-save
_single_saves: set[asyncio.Task[None]] = set()


async def _save_single(record: Transcript) -> None:
    """Save the record under a new reference and send its check. Nothing here may fail the call:
    a store that is missing or unreachable is logged by type, and that call is not audited."""
    conversation = _new_conversation_id()
    try:
        saved = await get_store().save(conversation, 0, record, _audit_grace_s())
    except Exception as exc:  # noqa: BLE001 - off the call's path: logged, never raised
        log.warning("%s single call not saved - %s", AUDIT_ROUTE, _audit_failure(exc))
        return
    if not saved:  # a new reference never meets a record; named for the day one does
        log.warning("%s single call not saved - conversation %s exists", AUDIT_ROUTE, conversation)
        return
    await _audit_send({"conversation": conversation, "check": True})


def _audit_single(
    headers: dict[str, str], request: LlmRequest, done: Completion, bounds: tuple[str, str]
) -> None:
    """Record a call the model answered, off the call's path: the record is built here, where the
    call's trace is, and saved by a task the reply does not wait for."""
    try:
        record = _single_record(headers, request, done, bounds)
        task = asyncio.get_running_loop().create_task(
            _save_single(record), name="transcript-audit-single"
        )
    except Exception as exc:  # noqa: BLE001 - the audit is a side effect; the call answers
        log.warning("%s single call not recorded - %s", AUDIT_ROUTE, type(exc).__name__)
        return
    _single_saves.add(task)
    task.add_done_callback(_single_saves.discard)


async def audit_single_idle() -> None:
    """Wait for the saves in flight - the shutdown's drain, and the tests' seam."""
    while _single_saves:
        await asyncio.gather(*list(_single_saves), return_exceptions=True)


def _plus_seconds(stamp: str, seconds: int) -> str:
    """An ISO-8601 UTC stamp moved by seconds (the conversation row's expires_at)."""
    moment = datetime.fromisoformat(stamp)
    return iso_utc(moment + timedelta(seconds=seconds))


def _calls_of(blocks: Any) -> list[dict[str, Any]] | None:
    """The normalized calls of a tool_use response, for the person who queries."""
    calls = [
        {"id": block["id"], "name": block["name"], "input": block["input"]}
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    return calls or None


def _turn_row(conversation: str, record: Transcript, fact: dict[str, Any]) -> dict[str, Any]:
    """A turn row from the record: the turn's facts, the messages it sent before the model
    answered (its request: the prompt, or the one message of tool results) and the assistant
    content blocks it got back (its response), as the model exchanged them."""
    first, answer = fact["messages"]
    usage: dict[str, Any] = fact.get("usage") or {}
    content = record.messages[answer]["content"]
    return {
        "conversation": conversation,
        "turn": int(fact["turn"]),
        "trace_id": fact.get("trace_id"),
        "trace_path": fact.get("trace_path"),
        "correlation_id": fact.get("correlation_id"),
        "started_at": fact["started_at"],
        "ended_at": fact["ended_at"],
        "stop_reason": fact.get("stop_reason"),
        "status": fact["status"],
        "request_id": fact.get("request_id"),
        **{column: int(usage.get(column) or 0) for column in TOKEN_COLUMNS},
        "request": record.messages[first:answer],
        "response": content,
        "calls": _calls_of(content) if fact["status"] == "tool_use" else None,
    }


def _conversation_row(conversation: str, record: Transcript, now: str) -> dict[str, Any]:
    """The conversation row from the record: the identity, the first-turn snapshot, the status
    and the bounds. A record from before version 2 carries no facts: its row opens now."""
    first = record.turns[0] if record.turns else None
    last = record.turns[-1] if record.turns else None
    ended = last if record.closed and last is not None and last["turn"] == record.turn else None
    started_at = str(first["started_at"]) if first else now
    return {
        "conversation": conversation,
        "correlation_id": record.correlation_id,
        "graph_id": record.graph_id,
        "node": record.node,
        "model": record.model,
        "status": ended["status"] if ended else "open",
        "turns": record.turn,
        "started_at": started_at,
        "updated_at": now,
        "ended_at": ended["ended_at"] if ended else None,
        "expires_at": _plus_seconds(started_at, _audit_retention_s()),
        "system": record.system,
        "tools": record.tools,
    }


def _audit_failure(exc: BaseException) -> str:
    """A failure as a log may carry it: the type and the database's SQLSTATE, never the message of
    an error that could quote a row (a constraint violation details the failing row); the first
    line of a connection failure or a configuration error, which quotes none."""
    name = type(exc).__name__
    state = getattr(exc, "sqlstate", None)
    detail = f"{name} sqlstate={state}" if state else name
    quotes_no_row = isinstance(exc, (OSError, TimeoutError, AppException)) or name in (
        "OperationalError", "PoolTimeout",
    )  # fmt: skip
    text = str(getattr(exc, "message", None) or exc).strip()
    if quotes_no_row and text:
        return f"{detail}: {text.splitlines()[0]}"
    return detail


async def _audit_apply(conversation: str, check: bool) -> None:
    """One event: write what the audit store lacks of the conversation's record, then mark the
    record audited. A check on a closed record releases it first, so the mark says the check is
    done; a write of a closing turn leaves both to the check right behind it; an open record (a
    run waiting for a tool result, a person or the loop's next turn) is only filled."""
    store = get_store()
    audit = get_audit_store()
    record = await store.load(conversation)
    if record is None:
        log.warning(
            "%s conversation=%s no working record - expired or released", AUDIT_ROUTE, conversation
        )
        return
    held = await audit.turns(conversation)
    facts = {int(fact["turn"]): fact for fact in record.turns}
    missing = [turn for turn in range(1, record.turn + 1) if turn not in held]
    rows = [_turn_row(conversation, record, facts[turn]) for turn in missing if turn in facts]
    await audit.write(_conversation_row(conversation, record, iso_utc()), rows)
    unauditable = [turn for turn in missing if turn not in facts]
    if unauditable:
        # a record from before version 2: its earlier turns carry no facts (the one time the
        # audit is knowingly incomplete)
        log.warning(
            "%s conversation=%s turn(s) %s carry no facts - unaudited",
            AUDIT_ROUTE, conversation, ",".join(str(turn) for turn in unauditable),
        )  # fmt: skip
    released = False
    if record.closed and check:
        await store.release(conversation, record.retention)
        released = True
    if check or not record.closed:
        await store.mark_audited(conversation, record.turn)
    log.info(
        "%s conversation=%s turn=%d written=%d check=%s released=%s",
        AUDIT_ROUTE, conversation, record.turn, len(rows), check, released,
    )  # fmt: skip


async def transcript_audit(_headers: dict[str, str], body: Body) -> None:
    """The audit function (RFC-0012): registered at boot as a private route with ONE instance when
    llm.transcript.audit is configured. Two events, both local and drop-n-forget from llm.chat -
    ``{conversation, turn}`` after a compare-and-set accepted the turn, and ``{conversation,
    check: true}`` behind the write of a closing turn (and from the sweep). A failure is logged by
    type - never a prompt, a completion, an argument or a result - and the sweep repairs it."""
    event: dict[str, Any] = body if isinstance(body, dict) else {}
    conversation = event.get("conversation")
    if not isinstance(conversation, str) or not CONVERSATION_ID.fullmatch(conversation):
        log.warning("%s event without a conversation reference ignored", AUDIT_ROUTE)
        return
    check = bool(event.get("check"))
    try:
        await _audit_apply(conversation, check)
    except Exception as exc:  # noqa: BLE001 - off the turn's path: logged, the sweep repairs
        log.warning(
            "%s conversation=%s %s failed - %s",
            AUDIT_ROUTE, conversation, "check" if check else "write", _audit_failure(exc),
        )  # fmt: skip


async def audit_sweep() -> int:
    """One sweep: a check event for every live record whose audited mark lags its turn count -
    the runs the loop stopped, and the writes a dying process lost. On Redis one replica per
    interval does it; the memory store sweeps its own process. The number of checks sent."""
    store = get_store()
    if not await store.sweep_lock(_audit_sweep_ms()):
        return 0
    lagging = await store.unaudited()
    for conversation in lagging:
        await _audit_send({"conversation": conversation, "check": True})
    if lagging:
        log.info("%s sweep: %d record(s) behind their audit", AUDIT_ROUTE, len(lagging))
    return len(lagging)


async def _sweep_forever(interval_ms: int) -> None:
    while True:
        await asyncio.sleep(interval_ms / 1000)
        try:
            await audit_sweep()
        except Exception as exc:  # noqa: BLE001 - the store may be down; the next interval retries
            log.warning("%s sweep failed - %s", AUDIT_ROUTE, _audit_failure(exc))


async def audit_boot() -> None:
    """Start the audit when llm.transcript.audit is configured: register transcript.audit with one
    instance, open the store and start the sweep. A driver that is not installed stops the
    application by name (a deployment error); a database that does not answer is logged, and the
    sweep repairs what the outage defers."""
    global _sweep_task
    if not audit_configured():
        return
    try:
        audit = get_audit_store()
    except AppException as exc:
        log.error("transcript audit store cannot start - %s", exc.message)
        raise SystemExit(1) from exc
    if audit_single_configured() and not _prop("llm.transcript.store"):
        # a single call's record waits in the working store for its check: a deployment error
        log.error("transcript audit store cannot start - %s", MISSING_SINGLE_STORE)
        raise SystemExit(1)
    if not default_registry.exists(AUDIT_ROUTE):
        default_registry.register(AUDIT_ROUTE, transcript_audit, instances=1, private=True)
        log.info("Loaded PRIVATE %s, instances=1 - audit store %s", AUDIT_ROUTE, audit.name)
    try:
        await audit.open()
        log.info("transcript audit store %s ready", audit.name)
    except Exception as exc:  # noqa: BLE001 - an outage, not a deployment error
        log.warning(
            "transcript audit store %s not answering at start - %s; the sweep repairs what the "
            "outage defers",
            audit.name, _audit_failure(exc),
        )  # fmt: skip
    sweep_ms = _audit_sweep_ms()
    if sweep_ms > 0 and _sweep_task is None:
        _sweep_task = asyncio.get_running_loop().create_task(
            _sweep_forever(sweep_ms), name="transcript-audit-sweep"
        )


async def audit_shutdown() -> None:
    global _sweep_task
    task = _sweep_task
    _sweep_task = None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await audit_single_idle()  # a single call's save in flight reaches the store before it closes
    audit = _audit
    if audit is not None:
        await audit.close()


# --- the functions -----------------------------------------------------------------------


async def _answer(request: LlmRequest, backend: Backend) -> tuple[Attached | None, Completion]:
    """A single call's work under its one deadline: the files it names, then the model. A file
    that is refused is refused here, before any token is spent."""
    attached = await attach(request, backend) if request.attachments else None
    return attached, await _complete(attached.request if attached else request, backend)


@preload(route="llm.chat", instances=50)
async def llm_chat(headers: dict[str, str], body: Body) -> dict[str, Any]:
    """Single-shot completion - the AI node a graph's ``graph.task`` or a flow's task calls.

    Input (map):
      prompt | messages   single-turn text, or conversation turns [{role, content}]
      system              optional system prompt
      schema              optional JSON schema -> structured output (the graph needs parseable
                          verdicts for decision routing; additionalProperties defaults to false)
      params              model, max_tokens, timeout_ms, effort, stop_sequences, provider
      attachments         optional files for the model to look at, named by reference inside
                          llm.attachments.root: a path, or {path, pages} for pages of a PDF
                          (RFC-0016; README.md, "Attachments"). Never on a loop turn.

    Output (map): text | data, model, stop_reason, usage {input_tokens, output_tokens},
    request_id; stop_details when the model refused.

    ``params.timeout_ms`` bounds the whole call, SDK retries included - the x-ttl pattern -
    and the reading of the attachments. A reply that carries nothing usable (refused, empty, or
    a schema reply cut off) is a 422.

    A request that carries ``tools`` is one turn of an agent loop (RFC-0011): ``conversation``,
    ``turn``, ``results``, ``ttl`` and ``retention`` come with it, and the reply carries
    ``status`` (done, tool_use, refusal or cap), ``calls``, ``conversation`` and ``turn``. The
    transcript stays in this app's store; see README.md, "The loop contract". The loop's
    ``x-graph-id`` and ``x-node`` headers are recorded for the audit store, when one is
    configured (README.md, "The audit store").
    """
    if isinstance(body, dict) and _is_loop(body):
        return await _loop_turn(headers, body)
    request = prepare(body, streaming=False)
    started = time.perf_counter()
    started_at = iso_utc()
    try:
        attached, done = await asyncio.wait_for(
            _answer(request, get_backend()), timeout=request.timeout_ms / 1000
        )
    except Exception as exc:  # anything unmapped is re-raised below
        failure = _failure(exc, request.timeout_ms)
        if failure is None:
            raise
        log.warning("llm.chat failed - status=%d", failure.status)
        raise failure from exc
    if audit_single_configured():
        # before the reply is judged: a refusal, a cut-off reply and a reply that is a 422 below
        # were answered by the model and cost tokens, so they are recorded too. With attachments
        # the record keeps a reference to each file, never its bytes
        recorded = attached.recorded if attached else request
        _audit_single(headers, recorded, done, (started_at, iso_utc()))
    data = _judge(request, done, has_content=bool(done.text))
    _annotate(done)
    if attached:
        annotate_trace("llm_attachments", str(attached.count))
        annotate_trace("llm_attachment_bytes", str(attached.size))
        # the count and the bytes only - a path or a file's content never reaches a log
        log.info("llm.chat attachments=%d bytes=%d", attached.count, attached.size)
    _summary("llm.chat", done, started)
    result: dict[str, Any] = {
        "model": done.model,
        "stop_reason": done.stop_reason,
        "usage": {"input_tokens": done.input_tokens, "output_tokens": done.output_tokens},
    }
    if request.schema is not None:
        result["data"] = data
    else:
        result["text"] = done.text
    if done.request_id:
        result["request_id"] = done.request_id
    if done.stop_reason == "refusal" and done.refusal:
        result["stop_details"] = done.refusal
    return result


@preload(route="llm.stream", instances=50, interceptor=True)
async def llm_stream(headers: dict[str, str], event: EventEnvelope) -> None:
    """Streaming completion: the model's real token batches over the multi-shot reply
    contract - a calling engine renders them progressively out its own HTTP edge (SSE).

    Same request surface as llm.chat minus ``schema`` (a verdict is a single-shot reply).
    ``params.timeout_ms`` is the idle allowance between events, not a total deadline - a
    stream runs as long as tokens keep flowing. The terminal event's trailing metadata
    carries model, stop_reason, usage, request_id and the trace and business correlation
    ids. A stream that ends with no token at all fails in-band with a 422.
    """
    out = EventStreamWriter.from_request(event)
    started = time.perf_counter()
    try:
        request = prepare(event.body, streaming=True)
        backend = get_backend()
    except AppException as exc:
        out.fail(exc)
        return
    info = get_trace()
    meta: dict[str, Any] = {
        "language": "python",
        "trace_id": info.trace_id if info else None,
        "my_correlation_id": headers.get("my_correlation_id"),
    }
    frames = 0
    batch_log = str(_prop("llm.log.batches", "false")).lower() == "true"

    def forward(text: str) -> None:
        nonlocal frames
        if frames == 0:
            # the head rides the first token; a stream that never gets one fails cleanly
            out.first(200, TEXT_EVENT_STREAM)
        out.write(text)
        frames += 1
        if batch_log:
            # the diagnostics switch: a batch's number, size and arrival time - never its text
            elapsed = round((time.perf_counter() - started) * 1000)
            log.info("llm.stream batch=%d chars=%d t_ms=%d", frames, len(text), elapsed)

    started_at = iso_utc()
    try:
        done = await _complete(request, backend, on_text=forward)
        if audit_single_configured():
            # a stream the model completed is a single call like any other (RFC-0015)
            _audit_single(headers, request, done, (started_at, iso_utc()))
        _judge(request, done, has_content=frames > 0)
    except Exception as exc:  # anything unmapped is re-raised below
        failure = _failure(exc, request.timeout_ms)
        if failure is None:
            raise
        log.warning("llm.stream failed - status=%d frames=%d", failure.status, frames)
        out.fail(failure)
        return
    _annotate(done)
    _summary("llm.stream", done, started)
    trailing: dict[str, Any] = {
        "model": done.model,
        "stop_reason": done.stop_reason,
        "usage": {"input_tokens": done.input_tokens, "output_tokens": done.output_tokens},
        **meta,
    }
    if done.request_id:
        trailing["request_id"] = done.request_id
    if done.stop_reason == "refusal" and done.refusal:
        trailing["stop_details"] = done.refusal
    out.close(trailing)


def _safe_name(filename: str) -> str:
    """The uploaded name as a safe suffix of the saved name: the base name only, hidden-entry
    dots stripped (the attachments rules would refuse them), every other character an
    underscore, at most 80 characters."""
    base = Path(filename).name if filename else ""
    return _SAFE_NAME.sub("_", base).lstrip(".")[:80]


def _save_uploads(root: Path, files: list[UploadedFile]) -> list[dict[str, Any]]:
    """Write each file under a new name in the uploads folder of the root (in a worker thread)
    and return the references a later call names: the path inside the root, the original name,
    the kind by the first bytes (else the declared type), the size, the SHA-256 the audit keeps,
    and whether llm.chat can attach the file as it is."""
    folder = root / UPLOAD_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for uploaded in files:
        suffix = _safe_name(uploaded.name)
        name = f"{uuid.uuid4().hex}-{suffix}" if suffix else uuid.uuid4().hex
        with open(folder / name, "xb") as handle:  # a new name - never an overwrite
            handle.write(uploaded.content)
        detected = _media_type(uploaded.content[:16])
        saved.append({
            "path": f"{UPLOAD_FOLDER}/{name}",
            "filename": uploaded.name,
            "media_type": detected or uploaded.content_type,
            "bytes": len(uploaded.content),
            "sha256": hashlib.sha256(uploaded.content).hexdigest(),
            "attachable": detected is not None,
        })
    return saved


@preload(route="llm.upload", instances=10)
async def llm_upload(_headers: dict[str, str], body: Body) -> Any:
    """The document intake for the attachments folder (RFC-0016): behind a REST automation entry
    with ``upload: true`` (``POST /api/llm/upload`` in resources/rest.yaml), the files of a
    multipart upload are saved under ``uploads/`` inside ``llm.attachments.root``, each under a
    new name, and their references returned - the ``path`` a later ``llm.chat`` call names in
    ``attachments``, the kind by the first bytes, the size and the SHA-256 the audit keeps, and
    ``attachable``: whether the model takes the file as it is (a PDF or an image) or a conversion
    comes first (a word-processor file, a spreadsheet). The folder is readable through the model:
    put an authentication function or the gateway in front of the entry."""
    request = AsyncHttpRequest(body)
    files = request.files()
    if not files:
        raise AppException(400, UPLOAD_NO_FILES)
    root = attachments_root()
    saved = await asyncio.get_running_loop().run_in_executor(None, _save_uploads, root, files)
    annotate_trace("llm_uploads", len(saved))
    log.info("Uploaded %d file(s), %d bytes", len(saved), sum(item["bytes"] for item in saved))
    return {"files": saved}


@preload(route="llm.health", instances=5, private=True)
async def llm_health(headers: dict[str, str], _body: Body) -> Any:
    """Health check speaking the engines' interface contract (type=info / type=health).

    Activated for the /health actuator endpoint by mandatory.health.dependencies in
    resources/application.yml. It reports whether a call could be sent (a credential is
    present) without reaching the model, so a probe never spends a token.
    """
    backend = get_backend()
    if headers.get("type") == "info":
        info: dict[str, Any] = {
            "service": "llm.helper",
            "href": "http://127.0.0.1",
            "backend": backend.name,
            "model": _prop("llm.model", DEFAULT_MODEL),
        }
        region = getattr(backend.client, "aws_region", None)
        if region:
            info["region"] = region  # which region a Bedrock call is signed for
        if backend.api:
            info["api"] = backend.api  # which Bedrock surface answers
        store = _prop("llm.transcript.store")
        if store:
            info["transcript_store"] = store  # where a loop's transcripts are kept
        audit = _prop("llm.transcript.audit")
        if audit:
            info["transcript_audit"] = audit  # where every model turn is recorded for audit
        return info
    problem = backend.credential_problem()
    if problem:
        raise AppException(503, problem)
    return "llm.helper is running fine"


# the audit starts inside the serving loop, before the first request, when it is configured
platform.on_startup(audit_boot)
platform.on_startup(attachments_boot)  # a configured root that is not a folder stops the start
platform.on_cleanup(audit_shutdown)

if __name__ == "__main__":
    platform.run()
