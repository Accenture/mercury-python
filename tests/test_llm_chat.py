"""The llm.chat demo function (agent-orchestration experiment E0) - token-free:
the provider client is a fake, so these tests pin the adapter contract (request
shaping, structured output, usage surfacing, provider-error mapping) without
spending tokens or needing credentials.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from mercury_composable import AppException

_DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo_app.py"
_spec = importlib.util.spec_from_file_location("demo_app_under_test", _DEMO)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes
# are unknowable statically - Any tells every analyzer to trust the runtime
demo_app: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("demo_app_under_test", demo_app)
_spec.loader.exec_module(demo_app)


def _provider_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=42, output_tokens=7),
        stop_reason="end_turn",
        model="claude-opus-5",
    )


class _FakeMessages:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeClient:
    def __init__(self, outcome: Any):
        self.messages = _FakeMessages(outcome)
        self.timeouts: list[float] = []

    def with_options(self, timeout: float) -> "_FakeClient":
        self.timeouts.append(timeout)
        return self


def _patch_demo(monkeypatch: pytest.MonkeyPatch, attr: str, value: Any) -> None:
    """monkeypatch.setattr on the dynamically loaded demo module. The attribute
    name rides through a parameter because no static analyzer can verify names
    on a module loaded from a file path; monkeypatch still validates the name
    at run time and restores the original on teardown."""
    monkeypatch.setattr(demo_app, attr, value)


def _install(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> _FakeClient:
    fake = _FakeClient(outcome)
    _patch_demo(monkeypatch, "_llm_client", fake)
    return fake


async def test_prompt_mode_returns_text_usage_and_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _provider_response("Paris"))
    result = await demo_app.llm_chat({}, {"prompt": "Capital of France?"})
    assert result == {
        "model": "claude-opus-5",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 42, "output_tokens": 7},
        "text": "Paris",
    }
    request = fake.messages.requests[0]
    # provider defaults: the documented model and max_tokens, single-turn message shaping
    assert request["model"] == demo_app.LLM_DEFAULT_MODELS["anthropic"]
    assert request["max_tokens"] == demo_app.LLM_DEFAULT_MAX_TOKENS
    assert request["messages"] == [{"role": "user", "content": "Capital of France?"}]
    assert "output_config" not in request
    # the default time budget maps onto the SDK timeout (seconds)
    assert fake.timeouts == [demo_app.LLM_DEFAULT_TIMEOUT_MS / 1000]


async def test_schema_mode_requests_structured_output_and_parses_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {
        "type": "object",
        "properties": {"label": {"type": "string", "enum": ["question", "bug", "feature"]}},
        "required": ["label"],
        "additionalProperties": False,
    }
    fake = _install(monkeypatch, _provider_response(json.dumps({"label": "bug"})))
    result = await demo_app.llm_chat(
        {},
        {
            "prompt": "Classify: the app crashes on save",
            "system": "You are a support triage assistant.",
            "schema": schema,
            "params": {"model": "claude-opus-5", "max_tokens": 512, "timeout_ms": 5000},
        },
    )
    assert result["data"] == {"label": "bug"}
    assert "text" not in result
    request = fake.messages.requests[0]
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert request["system"] == "You are a support triage assistant."
    assert request["max_tokens"] == 512
    assert fake.timeouts == [5.0]


async def test_schema_defaults_to_a_closed_object(monkeypatch: pytest.MonkeyPatch) -> None:
    # a bounded verdict wants a closed schema - additionalProperties defaults to
    # false when the caller omits it (callers that set it keep their value)
    fake = _install(monkeypatch, _provider_response(json.dumps({"label": "bug"})))
    await demo_app.llm_chat({}, {"prompt": "x", "schema": {"type": "object"}})
    sent = fake.messages.requests[0]["output_config"]["format"]["schema"]
    assert sent == {"additionalProperties": False, "type": "object"}


async def test_conversation_turns_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _provider_response("hi"))
    turns = [{"role": "user", "content": "hello"}]
    await demo_app.llm_chat({}, {"messages": turns})
    assert fake.messages.requests[0]["messages"] == turns


async def test_missing_prompt_and_messages_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _provider_response("unused"))
    with pytest.raises(AppException) as error:
        await demo_app.llm_chat({}, {"schema": {}})
    assert error.value.status == 400


def _gemini_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        model_version="gemini-3.6-flash",
        usage_metadata=SimpleNamespace(prompt_token_count=11, candidates_token_count=3),
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
    )


class _FakeGeminiModels:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.requests: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeGemini:
    def __init__(self, outcome: Any):
        self.aio = SimpleNamespace(models=_FakeGeminiModels(outcome))


def _install_gemini(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> _FakeGemini:
    fake = _FakeGemini(outcome)
    _patch_demo(monkeypatch, "_gemini_client", fake)
    return fake


async def test_gemini_provider_speaks_the_same_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # provider swapped per call (or by the llm.provider config key) - the caller's
    # contract and the graph above it do not change
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    fake = _install_gemini(monkeypatch, _gemini_response(json.dumps({"label": "bug"})))
    result = await demo_app.llm_chat(
        {},
        {
            "prompt": "Classify: the app crashes on save",
            "system": "You are a support triage assistant.",
            "schema": schema,
            "params": {"provider": "gemini", "timeout_ms": 5000},
        },
    )
    assert result == {
        "model": "gemini-3.6-flash",
        "stop_reason": "STOP",
        "usage": {"input_tokens": 11, "output_tokens": 3},
        "data": {"label": "bug"},
    }
    request = fake.aio.models.requests[0]
    assert request["model"] == demo_app.LLM_DEFAULT_MODELS["gemini"]
    config = request["config"]
    assert config.system_instruction == "You are a support triage assistant."
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == {"additionalProperties": False, **schema}
    # HttpOptions.timeout is milliseconds - timeout_ms passes through unchanged
    assert config.http_options is not None
    assert config.http_options.timeout == 5000
    contents = request["contents"]
    assert len(contents) == 1
    assert contents[0].role == "user"


async def test_gemini_errors_map_to_envelope_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.genai import errors as genai_errors

    _install_gemini(monkeypatch, genai_errors.APIError(429, {"error": {"message": "quota"}}))
    with pytest.raises(AppException) as error:
        await demo_app.llm_chat({}, {"prompt": "x", "params": {"provider": "gemini"}})
    assert error.value.status == 429


async def test_unknown_provider_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _provider_response("unused"))
    with pytest.raises(AppException) as error:
        await demo_app.llm_chat({}, {"prompt": "x", "params": {"provider": "openai"}})
    assert error.value.status == 400


async def test_provider_errors_map_to_envelope_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # subclass the SDK exceptions so no httpx plumbing is needed - isinstance is
    # what the mapping chain dispatches on
    class _RateLimited(anthropic.RateLimitError):
        def __init__(self) -> None:
            Exception.__init__(self, "rate limited")
            self.status_code = 429

    class _Invalid(anthropic.APIStatusError):
        def __init__(self) -> None:
            Exception.__init__(self, "bad request")
            self.status_code = 400

    class _Unreachable(anthropic.APIConnectionError):
        def __init__(self) -> None:
            Exception.__init__(self, "connect timeout")

    for boom, expected in ((_RateLimited(), 429), (_Invalid(), 400), (_Unreachable(), 503)):
        _install(monkeypatch, boom)
        with pytest.raises(AppException) as error:
            await demo_app.llm_chat({}, {"prompt": "x"})
        assert error.value.status == expected
