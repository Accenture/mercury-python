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
llm.transcript.redis.* connection keys. The contract, the keys and the backends are documented in
README.md next to this file. The Node.js twin (mercury-nodejs extensions/llm-helper) speaks
the same contract, and both are pinned by shared vector files.

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
import importlib.util
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

try:
    import anthropic
except ImportError as missing_sdk:  # the SDK is this app's one dependency - teach the fix
    raise SystemExit(
        "The LLM helper needs the Anthropic SDK - pip install 'mercury-composable[llm]'"
    ) from missing_sdk

from mercury_composable import (
    AppException,
    Body,
    EventEnvelope,
    EventStreamWriter,
    annotate_trace,
    app_config,
    get_logger,
    get_trace,
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
DURATION = re.compile(r"([0-9]+)([smhd]?)")
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


def _prop(key: str, default: str | None = None) -> str | None:
    return app_config().get_property(key, default)


# --- the request ---------------------------------------------------------------------


@dataclass(frozen=True)
class LlmRequest:
    """A validated request. The contract's one request surface, shared by both routes."""

    model: str
    max_tokens: int
    timeout_ms: int
    max_retries: int
    messages: list[dict[str, str]]
    system: str | None
    effort: str | None
    stop_sequences: list[str] | None
    schema: dict[str, Any] | None
    fallbacks: bool
    tools: list[dict[str, Any]] | None = None  # a loop turn's tools, as the API takes them


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


def _fallbacks_enabled() -> bool:
    mode = str(_prop("llm.fallbacks", "default")).lower()
    if mode not in ("default", "off"):
        raise AppException(500, "llm.fallbacks must be 'default' or 'off'")
    return mode == "default"


def prepare(body: Body, *, streaming: bool, continuation: bool = False) -> LlmRequest:
    """Validate a request body into an LlmRequest, or raise AppException(400).

    The checks run in a fixed order, the same in the Node.js twin: the loop fields on the
    streaming route, body, params and provider, the numbers, the turns, system, effort, stop
    sequences, schema, the fallbacks switch. A loop turn after the first (``continuation``)
    carries no turns of its own: the transcript holds them.
    """
    if streaming and isinstance(body, dict) and any(key in body for key in LOOP_KEYS):
        raise AppException(400, STREAM_LOOP)
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
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, tool in enumerate(raw):
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
        tools.append(entry)
    return tools


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
    if conversation is None:
        if turn:
            raise AppException(400, "turn must be 0 or absent on a first turn")
        if results is not None:
            raise AppException(400, "results answer a previous turn - a first turn carries none")
    else:
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
    return LoopTurn(tools, conversation, turn or 0, results, ttl_s, retention_s)


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

    session = boto3.Session(
        profile_name=getattr(client, "aws_profile", None),
        region_name=getattr(client, "aws_region", None),
        aws_access_key_id=getattr(client, "aws_access_key", None),
        aws_secret_access_key=getattr(client, "aws_secret_key", None),
        aws_session_token=getattr(client, "aws_session_token", None),
    )
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
# conversation, and every write sets the record's time to live.


@dataclass(frozen=True)
class Transcript:
    model: str
    messages: list[dict[str, Any]]
    turn: int
    pending: list[str]  # the ids of the last turn's tool calls, in call order
    closed: bool = False  # the run ended; a retained record is kept for audit, never continued

    def to_json(self) -> str:
        record = {
            "v": 1,
            "model": self.model,
            "turn": self.turn,
            "pending": self.pending,
            "closed": self.closed,
            "messages": self.messages,
        }
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_json(text: str) -> Transcript:
        record = json.loads(text)
        return Transcript(
            model=record["model"],
            messages=record["messages"],
            turn=record["turn"],
            pending=record["pending"],
            closed=record["closed"],
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


class MemoryStore:
    """One process: for development and tests. A second replica cannot see these records."""

    name = STORE_MEMORY

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._records: dict[str, tuple[float, int, str]] = {}  # expiry, turn, record

    def _live(self, conversation: str) -> tuple[float, int, str] | None:
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
            self._records[conversation] = (self._clock() + ttl_s, record.turn, record.to_json())
        return True


# one script, one key: atomic on a standalone server and safe on a cluster
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


class RedisStore:
    """A hash per conversation under transcript:{conversation}: the turn count and the record."""

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


async def _loop_turn(body: dict[str, Any]) -> dict[str, Any]:
    """One model turn of an agent loop: load and check the record, append the tool results, call
    the model, append its turn, and save under the compare-and-set."""
    loop = prepare_loop(body)
    request = prepare(body, streaming=False, continuation=loop.conversation is not None)
    store = get_store()
    try:
        if loop.conversation is None:
            conversation = _new_conversation_id()
            record = Transcript(request.model, list(request.messages), 0, [])
            messages = list(record.messages)
        else:
            conversation = loop.conversation
            record = _check_turn(conversation, await store.load(conversation), loop, body)
            messages = [*record.messages, _tool_results(record.pending, loop.results)]
    except AppException:
        raise
    except Exception as exc:  # the store's own failure - a connection, a timeout
        raise _store_failure(exc) from exc
    request = replace(request, model=record.model, messages=messages, tools=loop.tools)
    started = time.perf_counter()
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
    outcome = _loop_outcome(request, done)
    status = outcome["status"]
    calls = outcome.get("calls", [])
    turn = record.turn + 1
    saved = Transcript(
        model=record.model,
        messages=[*messages, {"role": "assistant", "content": done.content}],
        turn=turn,
        pending=[call["id"] for call in calls],
        closed=status != "tool_use",
    )
    # a run the model ended takes the retention; a run the loop stops (a cut-off turn, or the
    # loop's own limits) keeps its time to live and expires with it
    model_ended = status in ("done", "refusal") or outcome.get("cap") == "context"
    ttl_s = loop.retention_s if model_ended else loop.ttl_s
    try:
        written = await store.save(conversation, record.turn, saved, ttl_s)
    except Exception as exc:  # the store's own failure - a connection, a timeout
        raise _store_failure(exc) from exc
    if not written:
        raise AppException(
            409,
            f"conversation {conversation} moved on before turn {turn} was saved - "
            "a duplicated turn",
        )
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


# --- the functions -----------------------------------------------------------------------


@preload(route="llm.chat", instances=50)
async def llm_chat(_headers: dict[str, str], body: Body) -> dict[str, Any]:
    """Single-shot completion - the AI node a graph's ``graph.task`` or a flow's task calls.

    Input (map):
      prompt | messages   single-turn text, or conversation turns [{role, content}]
      system              optional system prompt
      schema              optional JSON schema -> structured output (the graph needs parseable
                          verdicts for decision routing; additionalProperties defaults to false)
      params              model, max_tokens, timeout_ms, effort, stop_sequences, provider

    Output (map): text | data, model, stop_reason, usage {input_tokens, output_tokens},
    request_id; stop_details when the model refused.

    ``params.timeout_ms`` bounds the whole call, SDK retries included - the x-ttl pattern.
    A reply that carries nothing usable (refused, empty, or a schema reply cut off) is a 422.

    A request that carries ``tools`` is one turn of an agent loop (RFC-0011): ``conversation``,
    ``turn``, ``results``, ``ttl`` and ``retention`` come with it, and the reply carries
    ``status`` (done, tool_use, refusal or cap), ``calls``, ``conversation`` and ``turn``. The
    transcript stays in this app's store; see README.md, "The loop contract".
    """
    if isinstance(body, dict) and _is_loop(body):
        return await _loop_turn(body)
    request = prepare(body, streaming=False)
    started = time.perf_counter()
    try:
        done = await asyncio.wait_for(
            _complete(request, get_backend()), timeout=request.timeout_ms / 1000
        )
    except Exception as exc:  # anything unmapped is re-raised below
        failure = _failure(exc, request.timeout_ms)
        if failure is None:
            raise
        log.warning("llm.chat failed - status=%d", failure.status)
        raise failure from exc
    data = _judge(request, done, has_content=bool(done.text))
    _annotate(done)
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

    try:
        done = await _complete(request, backend, on_text=forward)
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
        return info
    problem = backend.credential_problem()
    if problem:
        raise AppException(503, problem)
    return "llm.helper is running fine"


if __name__ == "__main__":
    platform.run()
