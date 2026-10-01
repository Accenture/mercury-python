"""
The LLM helper - a dedicated polyglot function host for the AI nodes of the
agent-orchestration experiment: ``llm.chat``, ``llm.stream`` and ``llm.health``, on the
official Anthropic SDK. The engines stay LLM-free: a graph or a flow reaches these routes
through declarative Event-over-HTTP like any other function, so the certified model decides
control flow while the LLM advises within it.

Run:  pip install 'mercury-composable[llm]'
      mercury-serve examples/llm-helper/llm_helper.py

The credential comes from the environment (ANTHROPIC_API_KEY), never from a config file.
Settings live in resources/application.yml (or -Dkey=value): llm.backend, llm.model,
llm.max.tokens, llm.timeout.ms, llm.max.retries, llm.fallbacks, llm.effort. The contract,
the keys and the backend seam are documented in README.md next to this file. The Node.js
twin (mercury-nodejs examples/llm-helper) speaks the same contract, and both are pinned by
one shared vector file.

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
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT_MS = 60000
DEFAULT_MAX_RETRIES = 2
PROVIDERS = (DEFAULT_PROVIDER,)
BACKENDS = (DEFAULT_BACKEND,)
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
# the SDK raises a bare TypeError, before sending anything, when no credential resolves
CREDENTIAL_TYPE_ERROR = "Could not resolve authentication method"


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


def prepare(body: Body, *, streaming: bool) -> LlmRequest:
    """Validate a request body into an LlmRequest, or raise AppException(400).

    The checks run in a fixed order, the same in the Node.js twin: body, params and provider,
    the numbers, the turns, system, effort, stop sequences, schema, the fallbacks switch.
    """
    turns = body.get("messages") if isinstance(body, dict) else None
    has_turns = isinstance(turns, list) and bool(turns)
    if not isinstance(body, dict) or not (body.get("prompt") or has_turns):
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
        messages=_turns(body),
        system=_system(body),
        effort=_effort(params),
        stop_sequences=_stop_sequences(params),
        schema=_schema(body, streaming),
        fallbacks=_fallbacks_enabled(),
    )


# --- the backend seam ------------------------------------------------------------------


def _same_model(model: str) -> str:
    """The model id as the Claude API takes it; another route may spell it differently."""
    return model


@dataclass(frozen=True)
class Backend:
    """One way to reach Claude: the client, and what this route to the model supports.

    This is the seam for a second route to the same models - AWS Bedrock through IAM is the
    planned one (see README.md, "Backends"): build its client in get_backend(), give it a
    provider_model that prefixes the model id, report no server-side fallbacks, and describe
    a missing credential. Nothing else in this module knows which route answered.
    """

    name: str
    client: Any
    supports_fallbacks: bool
    provider_model: Callable[[str], str] = _same_model

    def credential_problem(self) -> str | None:
        """What is missing for a call to be sent, or None. No network traffic."""
        names = ("api_key", "auth_token", "credentials", "custom_auth")
        if any(getattr(self.client, name, None) for name in names):
            return None
        return MISSING_CREDENTIAL


_backend: Backend | None = None  # built on first use - module-level so tests can inject


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
        backend = Backend(name, anthropic.AsyncAnthropic(), supports_fallbacks=True)
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
    output: dict[str, Any] = {}
    if request.schema is not None:
        output["format"] = {"type": "json_schema", "schema": request.schema}
    if request.effort:
        output["effort"] = request.effort
    if output:
        kwargs["output_config"] = output
    if request.stop_sequences:
        kwargs["stop_sequences"] = request.stop_sequences
    if (
        request.fallbacks
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
    return Completion(
        text="".join(b.text for b in message.content if getattr(b, "type", "") == "text"),
        model=message.model,
        stop_reason=message.stop_reason or "",
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        request_id=request_id or getattr(message, "_request_id", None),
        refusal=_refusal(getattr(message, "stop_details", None)),
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
    """
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
    present) without any network traffic, so a probe never spends a token.
    """
    backend = get_backend()
    if headers.get("type") == "info":
        return {
            "service": "llm.helper",
            "href": "http://127.0.0.1",
            "backend": backend.name,
            "model": _prop("llm.model", DEFAULT_MODEL),
        }
    problem = backend.credential_problem()
    if problem:
        raise AppException(503, problem)
    return "llm.helper is running fine"


if __name__ == "__main__":
    platform.run()
