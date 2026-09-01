"""The llm.stream demo function (progressive token rendering, E0 follow-up) -
token-free: fake provider streams and a recording writer pin the relay contract
(head, ordered token batches, terminal metadata, provider-error fail) without
spending tokens or needing credentials.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from mercury_composable import AppException

_DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo_app.py"
_spec = importlib.util.spec_from_file_location("demo_app_stream_under_test", _DEMO)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes
# are unknowable statically - Any tells every analyzer to trust the runtime
demo_app: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("demo_app_stream_under_test", demo_app)
_spec.loader.exec_module(demo_app)


class _FakeWriter:
    def __init__(self) -> None:
        self.head: tuple[int, str] | None = None
        self.segments: list[Any] = []
        self.trailing: Any = None
        self.error: Exception | None = None

    def first(self, status: int, content_type: str) -> None:
        self.head = (status, content_type)

    def write(self, segment: Any) -> None:
        self.segments.append(segment)

    def close(self, trailing_metadata: Any = None) -> None:
        self.trailing = trailing_metadata

    def fail(self, error: Exception) -> None:
        self.error = error


class _FakeWriterFactory:
    instance = _FakeWriter()

    @classmethod
    def from_request(cls, _event: Any) -> _FakeWriter:
        return cls.instance


def _patch_demo(monkeypatch: pytest.MonkeyPatch, attr: str, value: Any) -> None:
    """monkeypatch.setattr on the dynamically loaded demo module. The attribute
    name rides through a parameter because no static analyzer can verify names
    on a module loaded from a file path; monkeypatch still validates the name
    at run time and restores the original on teardown."""
    monkeypatch.setattr(demo_app, attr, value)


def _install_writer(monkeypatch: pytest.MonkeyPatch) -> _FakeWriter:
    fake = _FakeWriter()
    _FakeWriterFactory.instance = fake
    _patch_demo(monkeypatch, "EventStreamWriter", _FakeWriterFactory)
    return fake


def _event(body: Any) -> SimpleNamespace:
    return SimpleNamespace(body=body)


# --- gemini streaming path ---------------------------------------------------


def _gemini_chunk(text: str | None, usage: Any = None, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        usage_metadata=usage,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))] if finish else [],
        model_version="gemini-3.6-flash",
    )


class _FakeGeminiStreamModels:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.requests: list[dict[str, Any]] = []

    async def generate_content_stream(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        chunks = list(self.outcome)

        async def gen():
            for chunk in chunks:
                yield chunk

        return gen()


def _install_gemini_stream(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> _FakeGeminiStreamModels:
    models = _FakeGeminiStreamModels(outcome)
    fake = SimpleNamespace(aio=SimpleNamespace(models=models))
    _patch_demo(monkeypatch, "_gemini_client", fake)
    return models


async def test_gemini_token_batches_relay_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _install_writer(monkeypatch)
    final_usage = SimpleNamespace(prompt_token_count=9, candidates_token_count=17)
    models = _install_gemini_stream(monkeypatch, [
        _gemini_chunk("Event-"),
        _gemini_chunk("driven "),
        _gemini_chunk("haiku", usage=final_usage, finish="STOP"),
    ])
    await demo_app.llm_stream(
        {"my_correlation_id": "biz-777"},
        _event({"prompt": "haiku please", "params": {"provider": "gemini", "timeout_ms": 5000}}),
    )
    assert out.error is None
    assert out.head == (200, "text/event-stream")
    assert out.segments == ["Event-", "driven ", "haiku"]
    assert out.trailing["usage"] == {"input_tokens": 9, "output_tokens": 17}
    assert out.trailing["stop_reason"] == "STOP"
    assert out.trailing["model"] == "gemini-3.6-flash"
    assert out.trailing["my_correlation_id"] == "biz-777"
    # HttpOptions.timeout is milliseconds - timeout_ms passes through unchanged
    config = models.requests[0]["config"]
    assert config.http_options is not None
    assert config.http_options.timeout == 5000


async def test_gemini_provider_error_fails_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.genai import errors as genai_errors

    out = _install_writer(monkeypatch)
    _install_gemini_stream(monkeypatch, genai_errors.APIError(429, {"error": {"message": "quota"}}))
    await demo_app.llm_stream({}, _event({"prompt": "x", "params": {"provider": "gemini"}}))
    assert isinstance(out.error, AppException)
    assert out.error.status == 429
    assert out.trailing is None


async def test_missing_prompt_fails_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _install_writer(monkeypatch)
    await demo_app.llm_stream({}, _event({"params": {"provider": "gemini"}}))
    assert isinstance(out.error, AppException)
    assert out.error.status == 400
    assert out.head is None
    assert out.segments == []


# --- anthropic streaming path ------------------------------------------------


class _FakeAnthropicStream:
    def __init__(self, texts: list[str], final: Any):
        self._texts = texts
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    @property
    def text_stream(self) -> Any:
        async def gen():
            for text in self._texts:
                yield text

        return gen()

    async def get_final_message(self) -> Any:
        return self._final


class _FakeAnthropicStreamClient:
    def __init__(self, outcome: Any, final: Any):
        self.outcome = outcome
        self.final = final
        self.requests: list[dict[str, Any]] = []
        self.messages = self
        self.timeout: float | None = None

    def with_options(self, timeout: float) -> "_FakeAnthropicStreamClient":
        self.timeout = timeout
        return self

    def stream(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _FakeAnthropicStream(self.outcome, self.final)


async def test_anthropic_token_batches_relay_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _install_writer(monkeypatch)
    final = SimpleNamespace(
        model="claude-opus-5",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=12, output_tokens=34),
    )
    client = _FakeAnthropicStreamClient(["Hello ", "world"], final)
    _patch_demo(monkeypatch, "_llm_client", client)
    await demo_app.llm_stream({}, _event({"prompt": "greet me"}))
    assert out.error is None
    assert out.head == (200, "text/event-stream")
    assert out.segments == ["Hello ", "world"]
    assert out.trailing["usage"] == {"input_tokens": 12, "output_tokens": 34}
    assert out.trailing["model"] == "claude-opus-5"
    assert client.requests[0]["model"] == demo_app.LLM_DEFAULT_MODELS["anthropic"]


async def test_anthropic_rate_limit_fails_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RateLimited(anthropic.RateLimitError):
        def __init__(self) -> None:
            Exception.__init__(self, "rate limited")
            self.status_code = 429

    out = _install_writer(monkeypatch)
    client = _FakeAnthropicStreamClient(_RateLimited(), None)
    _patch_demo(monkeypatch, "_llm_client", client)
    await demo_app.llm_stream({}, _event({"prompt": "x"}))
    assert isinstance(out.error, AppException)
    assert out.error.status == 429
