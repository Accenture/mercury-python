"""
Demo polyglot functions.

Run:  mercury-serve examples/demo_app.py

Configuration comes from examples/resources/application.yml (the engines'
"resources" convention - port 8086, the demo.health dependency, log format);
override any key with -Dkey=value, e.g. -Drest.server.port=8090.

Then map a route from a Mercury engine application (event-over-http.yaml):

    event.http:
      - route: 'hello.python'
        target: 'http://127.0.0.1:8086/api/event'
"""

import asyncio
import json
from typing import Any

from mercury_composable import (
    AppException,
    Body,
    EventEnvelope,
    EventStreamWriter,
    PostOffice,
    annotate_trace,
    app_config,
    get_logger,
    get_trace,
    platform,
    preload,
)

log = get_logger(__name__)

# the AI node's provider surface: llm.provider / llm.model in the app config
# (or -D overrides), params.provider / params.model per call
LLM_DEFAULT_PROVIDER = "anthropic"
LLM_DEFAULT_MODELS = {"anthropic": "claude-opus-5", "gemini": "gemini-3.6-flash"}
LLM_DEFAULT_MAX_TOKENS = 16000
LLM_DEFAULT_TIMEOUT_MS = 60000

# the streaming head's content type (every streaming demo renders as SSE)
TEXT_EVENT_STREAM = "text/event-stream"

# lazily built provider clients - module-level so tests can inject fakes
_llm_client: Any = None
_gemini_client: Any = None


@preload(route="hello.python", instances=10)
def handle_event(_headers: dict[str, str], body: Body):
    """Uppercase transform - the polyglot hello world.

    The (headers, body) two-part signature is the function contract (the
    TypedLambdaFunction mirror) - a handler that does not need headers keeps
    the parameter, underscore-prefixed per Python convention.
    """
    if not isinstance(body, dict) or "text" not in body:
        raise AppException(400, "missing 'text'")
    annotate_trace("language", "python")
    log.info("Transforming text of length %d", len(str(body["text"])))
    return {"text": str(body["text"]).upper(), "language": "python"}


@preload(route="hello.declarative", instances=10)
async def declarative_echo(headers: dict[str, str], body: Body):
    """Echo for the composable-example declarative Event-over-HTTP demo."""
    return {"body": body, "headers": headers, "language": "python"}


@preload(route="demo.suffix.helper", instances=10, private=True)
async def suffix_helper(_headers: dict[str, str], body: Body):
    """Private helper - callable in-app only (the HTTP host answers 403 for it)."""
    assert isinstance(body, dict)
    return {"text": f"{body.get('text', '')}!", "language": "python"}


@preload(route="hello.chain", instances=10)
async def chain(_headers: dict[str, str], body: Body):
    """Local composition: a public function calls a private sibling through the bus."""
    reply = await PostOffice().request("demo.suffix.helper", body=body, timeout_ms=5000)
    return reply.body


@preload(route="hello.sync.chain", instances=10)
def sync_chain(_headers: dict[str, str], body: Body):
    """Sync composition: a plain-def handler (the requests/NumPy world) calls a
    sibling through the sync bridge - blocking its own worker thread only,
    never the event loop."""
    reply = PostOffice().request_sync("demo.suffix.helper", body=body, timeout_ms=5000)
    return reply.body


@preload(route="hello.tokens", instances=10, interceptor=True)
async def stream_tokens(headers: dict[str, str], event: EventEnvelope):
    """Streaming demo: paced test messages over the multi-shot reply contract.

    A calling engine consumes this progressively through Event-over-HTTP
    (accept: text/event-stream on the outbound event) and can render it out
    its own HTTP edge - engine-to-wrapper token streaming. Optional headers:
    "delay" ms between messages (default 500, clamped 50-5000) and "count"
    messages (default 5, clamped 1-100).
    """
    delay = min(5000, max(50, int(headers.get("delay", "500") or 500))) / 1000
    count = min(100, max(1, int(headers.get("count", "5") or 5)))
    # with log.format=json/compact, this line carries the application log
    # "context" block (trace ids, business cid) - see the streaming guide
    log.info("Streaming %d messages", count)
    out = EventStreamWriter.from_request(event)
    out.first(200, TEXT_EVENT_STREAM)
    out.write("The following messages are rendered slowly to demonstrate streaming:")
    for n in range(1, count + 1):
        await asyncio.sleep(delay)
        out.write(f"test message {n} (python)")
    # the trailing metadata echoes the distributed trace id and the business
    # correlation-id, so a calling engine's edge shows both continuity
    # dimensions end to end
    info = get_trace()
    out.close({"count": count, "language": "python",
               "trace_id": info.trace_id if info else None,
               "my_correlation_id": headers.get("my_correlation_id")})


@preload(route="llm.chat", instances=50)
async def llm_chat(_headers: dict[str, str], body: Body):
    """The AI node (agent-orchestration experiment E0): a provider-neutral LLM
    adapter as a plain wrapper-side function. The engine and this host stay
    LLM-free - a graph or flow reaches this route like any other function, so
    the certified graph decides control flow while the model advises within it.

    Input (map):
      prompt | messages   single-turn text, or conversation turns [{role, content}]
      system              optional system prompt
      schema              optional JSON schema -> structured output (the graph
                          needs parseable verdicts for decision routing;
                          additionalProperties defaults to false)
      params              provider, model, max_tokens, timeout_ms + provider
                          pass-through

    Provider selection: params.provider, else the llm.provider config key
    (e.g. mercury-serve ... -Dllm.provider=gemini), else anthropic. The default
    model per provider comes from params.model, the llm.model config key, or
    LLM_DEFAULT_MODELS.

    Output (map): text | data, model, stop_reason (check for "refusal"),
    usage {input_tokens, output_tokens}.

    Provider errors ride the envelope status - portable to a graph's error
    context (error.code / error.message). The remaining time budget maps onto
    the SDK timeout (params.timeout_ms), the x-ttl pattern.
    """
    provider, model, max_tokens, timeout_ms, messages, system, params = _llm_request_prep(body)
    assert isinstance(body, dict)  # narrowed by _llm_request_prep
    schema = body.get("schema")
    if isinstance(schema, dict):
        # structured output: a closed schema is what a bounded verdict wants, so
        # default additionalProperties to false when the caller omits it
        schema = {"additionalProperties": False, **schema}
    else:
        schema = None
    if provider == "gemini":
        result = await _call_gemini(model, messages, system, schema, max_tokens, timeout_ms, params)
    else:
        result = await _call_anthropic(model, messages, system, schema, max_tokens, timeout_ms, params)
    annotate_trace("llm_model", str(result.get("model", "")))
    text = str(result.pop("text", ""))
    if schema is not None and text:
        # both providers guarantee schema-constrained output as one JSON text
        result["data"] = json.loads(text)
    else:
        result["text"] = text
    return result


def _llm_request_prep(
    body: Body,
) -> tuple[str, str, int, int, Any, Any, dict[str, Any]]:
    """The shared request surface of the AI nodes (llm.chat and llm.stream):
    provider and model resolution (params -> llm.provider/llm.model config ->
    defaults), token/time budgets and message shaping."""
    if not isinstance(body, dict) or not (body.get("prompt") or body.get("messages")):
        raise AppException(400, "missing 'prompt' or 'messages'")
    raw_params = body.get("params")
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
    provider = str(
        params.pop("provider", None)
        or app_config().get_property("llm.provider", LLM_DEFAULT_PROVIDER)
        or LLM_DEFAULT_PROVIDER
    ).lower()
    if provider not in LLM_DEFAULT_MODELS:
        raise AppException(400, f"unknown LLM provider '{provider}' - use one of "
                                f"{sorted(LLM_DEFAULT_MODELS)}")
    model = str(
        params.pop("model", None)
        or app_config().get_property("llm.model", None)
        or LLM_DEFAULT_MODELS[provider]
    )
    max_tokens = int(params.pop("max_tokens", LLM_DEFAULT_MAX_TOKENS))
    timeout_ms = int(params.pop("timeout_ms", LLM_DEFAULT_TIMEOUT_MS))
    messages = body.get("messages") or [{"role": "user", "content": str(body["prompt"])}]
    return provider, model, max_tokens, timeout_ms, messages, body.get("system"), params


async def _call_anthropic(model: str, messages: Any, system: Any, schema: dict[str, Any] | None,
                          max_tokens: int, timeout_ms: int, extra: dict[str, Any]) -> dict[str, Any]:
    """Anthropic SDK edition of the llm.chat contract (lazy optional import)."""
    try:
        import anthropic
    except ImportError as exc:  # the SDK is an optional extra - teach, don't crash the app
        raise AppException(501, "llm.chat requires the Anthropic SDK - pip install anthropic") from exc
    request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        request["system"] = system
    if schema is not None:
        request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    request.update(extra)  # provider pass-through (e.g. output_config.effort) wins verbatim
    global _llm_client
    if _llm_client is None:
        _llm_client = anthropic.AsyncAnthropic()
    try:
        response = await _llm_client.with_options(timeout=max(1.0, timeout_ms / 1000)) \
            .messages.create(**request)
    except anthropic.RateLimitError as exc:
        raise AppException(429, f"LLM provider rate limit - {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise AppException(int(exc.status_code), f"LLM provider error - {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise AppException(503, f"LLM provider unreachable - {exc}") from exc
    text = ""
    for block in response.content:
        if getattr(block, "type", "") == "text" and block.text:
            text = block.text
            break
    return {
        "text": text,
        "model": response.model,
        "stop_reason": str(response.stop_reason),
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


def _gemini_request(system: Any, messages: Any, max_tokens: int, timeout_ms: int,
                    extra: dict[str, Any]) -> tuple[Any, list[Any]]:
    """Config and role-mapped contents shared by the single-shot and streaming
    Gemini editions. HttpOptions.timeout is in milliseconds - params.timeout_ms
    passes through. Conversation turns map onto Gemini roles (assistant -> model).
    """
    from google.genai import types as genai_types
    config = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        http_options=genai_types.HttpOptions(timeout=timeout_ms),
        **extra,  # provider pass-through (e.g. temperature) wins verbatim
    )
    if config.automatic_function_calling is None:
        # the AI nodes expose no tool surface (the graph decides, the model
        # advises), so the SDK's automatic-function-calling loop is opted out -
        # which also silences its AFC advisory warning on direct
        # generate_content(_stream) calls
        config.automatic_function_calling = genai_types.AutomaticFunctionCallingConfig(disable=True)
    if system:
        config.system_instruction = str(system)
    contents = [
        genai_types.Content(
            role="model" if turn.get("role") == "assistant" else "user",
            parts=[genai_types.Part(text=str(turn.get("content", "")))],
        )
        for turn in messages
        if isinstance(turn, dict)
    ]
    return config, contents


def _gemini_finish(candidates: Any) -> str:
    """Finish-reason name from a response/chunk's candidates, or empty."""
    if candidates:
        reason = candidates[0].finish_reason
        if reason is not None:
            return getattr(reason, "name", str(reason))
    return ""


def _gemini_usage(usage: Any) -> dict[str, int]:
    """Usage metadata (absent until the final stream chunk) in the contract shape."""
    return {
        "input_tokens": (usage.prompt_token_count or 0) if usage else 0,
        "output_tokens": (usage.candidates_token_count or 0) if usage else 0,
    }


async def _call_gemini(model: str, messages: Any, system: Any, schema: dict[str, Any] | None,
                       max_tokens: int, timeout_ms: int, extra: dict[str, Any]) -> dict[str, Any]:
    """Gemini SDK edition of the llm.chat contract (lazy optional import).

    The client reads GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment.
    """
    try:
        from google import genai
        from google.genai import errors as genai_errors
    except ImportError as exc:
        raise AppException(501, "llm.chat requires the Gemini SDK - pip install google-genai") from exc
    config, contents = _gemini_request(system, messages, max_tokens, timeout_ms, extra)
    if schema is not None:
        config.response_mime_type = "application/json"
        config.response_json_schema = schema
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client()
    try:
        response = await _gemini_client.aio.models.generate_content(
            model=model, contents=contents, config=config)
    except genai_errors.APIError as exc:
        raise AppException(int(exc.code) if exc.code else 500, f"LLM provider error - {exc}") from exc
    except OSError as exc:  # connection-level failures (DNS, refused, timeout)
        raise AppException(503, f"LLM provider unreachable - {exc}") from exc
    return {
        "text": response.text or "",
        "model": getattr(response, "model_version", None) or model,
        "stop_reason": _gemini_finish(response.candidates),
        "usage": _gemini_usage(response.usage_metadata),
    }


@preload(route="llm.stream", instances=50, interceptor=True)
async def llm_stream(headers: dict[str, str], event: EventEnvelope):
    """The streaming AI node (agent-orchestration follow-up to E0): pulls the
    provider's REAL token stream and relays each token batch over the multi-shot
    reply contract - a calling engine renders it progressively out its own HTTP
    edge (SSE), with the same provider neutrality as llm.chat.

    Body: prompt | messages, optional system, params (provider, model,
    max_tokens, timeout_ms + provider pass-through). Structured output (schema)
    is deliberately not part of the streaming contract - a schema verdict is a
    single-shot reply (use llm.chat).

    The terminal event's trailing metadata carries model, stop_reason, usage
    and the trace/business correlation ids.
    """
    out = EventStreamWriter.from_request(event)
    try:
        provider, model, max_tokens, timeout_ms, messages, system, params = \
            _llm_request_prep(event.body)
    except AppException as exc:
        out.fail(exc)
        return
    info = get_trace()
    meta: dict[str, Any] = {
        "language": "python",
        "trace_id": info.trace_id if info else None,
        "my_correlation_id": headers.get("my_correlation_id"),
    }
    log.info("Streaming tokens from %s via %s", model, provider)
    if provider == "gemini":
        await _stream_gemini(out, model, messages, system, max_tokens, timeout_ms, params, meta)
    else:
        await _stream_anthropic(out, model, messages, system, max_tokens, timeout_ms, params, meta)


async def _stream_anthropic(out: EventStreamWriter, model: str, messages: Any, system: Any,
                            max_tokens: int, timeout_ms: int, extra: dict[str, Any],
                            meta: dict[str, Any]) -> None:
    """Anthropic SDK edition of the streaming contract (lazy optional import)."""
    try:
        import anthropic
    except ImportError:
        out.fail(AppException(501, "llm.stream requires the Anthropic SDK - pip install anthropic"))
        return
    request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        request["system"] = system
    request.update(extra)  # provider pass-through wins verbatim
    global _llm_client
    if _llm_client is None:
        _llm_client = anthropic.AsyncAnthropic()
    try:
        async with _llm_client.with_options(timeout=max(1.0, timeout_ms / 1000)) \
                .messages.stream(**request) as stream:
            out.first(200, TEXT_EVENT_STREAM)
            async for text in stream.text_stream:
                if text:
                    out.write(text)
            message = await stream.get_final_message()
    except anthropic.RateLimitError as exc:
        out.fail(AppException(429, f"LLM provider rate limit - {exc}"))
        return
    except anthropic.APIStatusError as exc:
        out.fail(AppException(int(exc.status_code), f"LLM provider error - {exc}"))
        return
    except anthropic.APIConnectionError as exc:
        out.fail(AppException(503, f"LLM provider unreachable - {exc}"))
        return
    out.close({
        "model": message.model,
        "stop_reason": str(message.stop_reason),
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
        **meta,
    })


async def _stream_gemini(out: EventStreamWriter, model: str, messages: Any, system: Any,
                         max_tokens: int, timeout_ms: int, extra: dict[str, Any],
                         meta: dict[str, Any]) -> None:
    """Gemini SDK edition of the streaming contract: each chunk is a token batch."""
    try:
        from google import genai
        from google.genai import errors as genai_errors
    except ImportError:
        out.fail(AppException(501, "llm.stream requires the Gemini SDK - pip install google-genai"))
        return
    config, contents = _gemini_request(system, messages, max_tokens, timeout_ms, extra)
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client()
    usage = None
    finish = ""
    version = model
    try:
        stream = await _gemini_client.aio.models.generate_content_stream(
            model=model, contents=contents, config=config)
        out.first(200, TEXT_EVENT_STREAM)
        async for chunk in stream:
            if chunk.text:
                out.write(chunk.text)
            # usage/finish arrive on the final chunk; the model version on any
            usage = chunk.usage_metadata or usage
            finish = _gemini_finish(chunk.candidates) or finish
            version = getattr(chunk, "model_version", None) or version
    except genai_errors.APIError as exc:
        out.fail(AppException(int(exc.code) if exc.code else 500, f"LLM provider error - {exc}"))
        return
    except OSError as exc:
        out.fail(AppException(503, f"LLM provider unreachable - {exc}"))
        return
    out.close({"model": version, "stop_reason": finish, "usage": _gemini_usage(usage), **meta})


@preload(route="demo.health", instances=5, private=True)
async def health_check(headers: dict[str, str], _body: Body):
    """Health check speaking the engines' interface contract (type=info / type=health).

    Activated for the /health actuator endpoint by mandatory.health.dependencies
    in examples/resources/application.yml (or a -D override).
    """
    if headers.get("type") == "info":
        return {"service": "demo.service", "href": "http://127.0.0.1"}
    return "demo.service is running fine"


if __name__ == "__main__":
    platform.run()
