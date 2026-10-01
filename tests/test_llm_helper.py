"""The LLM helper (examples/llm-helper/llm_helper.py) - token-free.

One shared contract file, tests/vectors/llm-helper-vectors.json (byte-identical in
mercury-nodejs), drives every case below against a fake of the Anthropic SDK: the exact SDK
call, the reply map, the error contract and the streaming segments are pinned without
spending a token or needing a credential, and the Node.js twin runs the same file, so the two
helpers cannot drift apart. The tests after the vector runs pin what a vector cannot: token
batches are never held back, no prompt text reaches a log, the trace annotations, the health
route and the backend seam.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from mercury_composable import AppException

_TESTS = Path(__file__).resolve().parent
_HELPER = _TESTS.parent / "examples" / "llm-helper" / "llm_helper.py"
_VECTORS = _TESTS / "vectors" / "llm-helper-vectors.json"
# the Node.js twin pins the same digest: change the file in both packs or in neither
_VECTORS_SHA256 = "1f4823d9259ed6ff26e96d044b5f6b3c7bc9b342cc8ad76047434c2d3a2b8fa0"

_spec = importlib.util.spec_from_file_location("llm_helper_under_test", _HELPER)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes are
# unknowable statically - Any tells every analyzer to trust the runtime
helper: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("llm_helper_under_test", helper)
_spec.loader.exec_module(helper)

VECTORS: dict[str, Any] = json.loads(_VECTORS.read_text(encoding="utf-8"))
CHAT_CASES: list[dict[str, Any]] = [c for c in VECTORS["cases"] if c["route"] == "llm.chat"]
STREAM_CASES: list[dict[str, Any]] = [c for c in VECTORS["cases"] if c["route"] == "llm.stream"]

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
_STATUS_CLASSES: dict[int, type[anthropic.APIStatusError]] = {
    400: anthropic.BadRequestError,
    401: anthropic.AuthenticationError,
    403: anthropic.PermissionDeniedError,
    404: anthropic.NotFoundError,
    422: anthropic.UnprocessableEntityError,
    429: anthropic.RateLimitError,
}
_NO_CREDENTIAL = (
    '"Could not resolve authentication method. Expected one of api_key, auth_token, or '
    "credentials to be set. Or for one of the `X-Api-Key` or `Authorization` headers to be "
    'explicitly omitted"'
)


def _status_error(spec: dict[str, Any]) -> anthropic.APIStatusError:
    """A real SDK error object, built the way the SDK builds one from a response."""
    status = int(spec["status"])
    headers = {"request-id": spec["request_id"]} if spec.get("request_id") else {}
    body = {"type": "error", "error": {"type": spec["type"], "message": spec["message"]}}
    response = httpx2.Response(status, request=_REQUEST, headers=headers, json=body)
    error_class = _STATUS_CLASSES.get(status)
    if error_class is None:
        error_class = anthropic.InternalServerError if status >= 500 else anthropic.APIStatusError
    return error_class(spec["message"], response=response, body=body)


class _Ledger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.options: list[dict[str, int]] = []
        self.cancelled = False


class _FakeStream:
    """What ``client.messages.stream(...)`` returns: an async context manager."""

    def __init__(self, provider: dict[str, Any], ledger: _Ledger) -> None:
        self._provider = provider
        self._ledger = ledger
        self.response: Any = None

    async def __aenter__(self) -> Any:
        kind = self._provider["kind"]
        if kind == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._ledger.cancelled = True
                raise
        if kind == "credential":
            raise TypeError(_NO_CREDENTIAL)
        if kind == "timeout":
            raise anthropic.APITimeoutError(request=_REQUEST)
        if kind == "connection":
            raise anthropic.APIConnectionError(message="Connection error.", request=_REQUEST)
        if kind == "status" and not self._provider.get("after"):
            raise _status_error(self._provider)
        request_id = self._provider.get("request_id")
        self.response = SimpleNamespace(headers={"request-id": request_id} if request_id else {})
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    @property
    def text_stream(self) -> Any:
        provider = self._provider

        async def batches() -> Any:
            deltas: list[str] = provider.get("deltas", [])
            after = int(provider.get("after", 0))
            for index, text in enumerate(deltas):
                if provider["kind"] == "status" and index == after:
                    raise _status_error(provider)
                yield text
            if provider["kind"] == "status" and after >= len(deltas):
                raise _status_error(provider)

        return batches()

    async def get_final_message(self) -> Any:
        provider = self._provider
        details: Any = provider.get("stop_details")
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text=provider.get("text", "".join(provider["deltas"])))
            ],
            model=provider["model"],
            stop_reason=provider["stop_reason"],
            usage=SimpleNamespace(**provider["usage"]),
            stop_details=SimpleNamespace(**details) if details else None,
        )


class _Namespace:
    def __init__(self, name: str, client: _FakeClient) -> None:
        self._name = name
        self._client = client

    def stream(self, **kwargs: Any) -> _FakeStream:
        self._client.ledger.calls.append((self._name, kwargs))
        return self._client.make_stream()


class _FakeClient:
    """The SDK client as the helper uses it: with_options(), then a message's namespace."""

    def __init__(self, provider: dict[str, Any] | None) -> None:
        self.provider = provider or {}
        self.ledger = _Ledger()
        self.api_key = "test-credential"
        self.messages = _Namespace("messages", self)
        self.beta = SimpleNamespace(messages=_Namespace("beta.messages", self))

    def with_options(self, *, timeout: float, max_retries: int) -> _FakeClient:
        self.ledger.options.append(
            {"timeout_ms": round(timeout * 1000), "max_retries": max_retries}
        )
        return self

    def make_stream(self) -> _FakeStream:
        return _FakeStream(self.provider, self.ledger)


class _FakeWriter:
    def __init__(self) -> None:
        self.head: tuple[int, str] | None = None
        self.segments: list[Any] = []
        self.trailing: Any = None
        self.error: AppException | None = None

    def first(self, status: int, content_type: str, _ttl_seconds: int | None = None) -> None:
        self.head = (status, content_type)

    def write(self, segment: Any) -> None:
        self.segments.append(segment)

    def close(self, trailing_metadata: Any = None) -> None:
        self.trailing = trailing_metadata

    def fail(self, error: Exception) -> None:
        assert isinstance(error, AppException)
        self.error = error


def _install_backend(
    monkeypatch: pytest.MonkeyPatch, client: Any, config: dict[str, str] | None = None
) -> None:
    settings = dict(config or {})
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(
        helper, "_backend", helper.Backend("anthropic", client, supports_fallbacks=True)
    )


def _install_writer(monkeypatch: pytest.MonkeyPatch) -> _FakeWriter:
    out = _FakeWriter()
    monkeypatch.setattr(helper, "EventStreamWriter", SimpleNamespace(from_request=lambda _e: out))
    return out


def _event(body: Any) -> SimpleNamespace:
    return SimpleNamespace(body=body)


def _assert_sdk(client: _FakeClient, case: dict[str, Any]) -> None:
    wanted = case["expect"].get("sdk")
    if wanted is not None:
        assert client.ledger.calls == [(wanted["namespace"], wanted["params"])]
        assert client.ledger.options == [wanted["options"]]
    elif case.get("provider") is None:
        assert client.ledger.calls == [], "a rejected request must not reach the provider"


def _assert_error(error: AppException | None, wanted: dict[str, Any]) -> None:
    assert error is not None, "an error was expected"
    assert error.status == wanted["status"], error.message
    for fragment in wanted["message_contains"]:
        assert fragment in error.message, f"{fragment!r} not in {error.message!r}"


# --- the shared contract: llm.chat -------------------------------------------------------


def test_the_vector_file_is_the_one_the_node_twin_runs() -> None:
    digest = hashlib.sha256(_VECTORS.read_bytes()).hexdigest()
    assert digest == _VECTORS_SHA256, "the shared vector file changed - update both packs"
    assert VECTORS["contract"] == "llm-helper"
    assert VECTORS["version"] == 1
    assert len(CHAT_CASES) + len(STREAM_CASES) == len(VECTORS["cases"])


@pytest.mark.parametrize("case", CHAT_CASES, ids=[c["name"] for c in CHAT_CASES])
async def test_chat_contract(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(case.get("provider"))
    _install_backend(monkeypatch, client, case.get("config"))
    result: Any = None
    error: AppException | None = None
    try:
        result = await helper.llm_chat({}, case["body"])
    except AppException as exc:
        error = exc
    _assert_sdk(client, case)
    if "error" in case["expect"]:
        _assert_error(error, case["expect"]["error"])
    else:
        assert error is None, error
        assert result == case["expect"]["result"]


# --- the shared contract: llm.stream -----------------------------------------------------


@pytest.mark.parametrize("case", STREAM_CASES, ids=[c["name"] for c in STREAM_CASES])
async def test_stream_contract(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(case.get("provider"))
    _install_backend(monkeypatch, client, case.get("config"))
    out = _install_writer(monkeypatch)
    await helper.llm_stream(case.get("headers", {}), _event(case["body"]))
    expect = case["expect"]
    _assert_sdk(client, case)
    wanted_head = expect.get("head")
    assert out.head == (tuple(wanted_head) if wanted_head else None)
    assert out.segments == expect.get("frames", [])
    terminal = expect.get("terminal")
    if terminal is None:
        assert out.trailing is None
    else:
        assert out.trailing is not None
        for key, value in terminal.items():
            assert out.trailing[key] == value, key
        assert out.trailing["language"] == "python"
    if "error" in expect:
        _assert_error(out.error, expect["error"])
    else:
        assert out.error is None, out.error


# --- progressive rendering: a token batch is never held back -----------------------------


async def test_each_token_batch_reaches_the_caller_before_the_next_is_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the streaming route is to deliver batches continuously. The fake model
    produces batch k only after asserting that the caller already holds batches 0 to k-1 -
    a helper that gathered the batches and sent them once would fail this test."""
    out = _install_writer(monkeypatch)
    batches = ["Event-", "driven ", "architecture ", "decouples ", "producers."]

    class _Paced(_FakeStream):
        @property
        def text_stream(self) -> Any:
            async def paced() -> Any:
                for index, text in enumerate(batches):
                    assert out.segments == batches[:index], f"batch {index} was produced early"
                    assert (out.head is not None) == (index > 0), "the head rides the first batch"
                    await asyncio.sleep(0)  # a network read yields to the loop between batches
                    yield text

            return paced()

    class _PacedClient(_FakeClient):
        def make_stream(self) -> _FakeStream:
            return _Paced(self.provider, self.ledger)

    provider = {
        "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "deltas": batches, "usage": {"input_tokens": 9, "output_tokens": 11},
    }  # fmt: skip
    _install_backend(monkeypatch, _PacedClient(provider))
    await helper.llm_stream({}, _event({"prompt": "describe it"}))
    assert out.error is None
    assert out.segments == batches, "every batch is its own segment"
    assert out.head == (200, "text/event-stream")
    assert out.trailing is not None
    assert out.trailing["usage"] == {"input_tokens": 9, "output_tokens": 11}


async def test_the_chat_route_sends_nothing_to_a_stream_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """llm.chat is the single-shot route: one reply map, never segments."""
    out = _install_writer(monkeypatch)
    provider = {
        "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "deltas": ["a", "b"], "usage": {"input_tokens": 1, "output_tokens": 2},
    }  # fmt: skip
    _install_backend(monkeypatch, _FakeClient(provider))
    result = await helper.llm_chat({}, {"prompt": "x"})
    assert result["text"] == "ab"
    assert out.segments == []
    assert out.head is None


# --- what a vector cannot say -------------------------------------------------------------


async def test_the_deadline_cancels_the_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"kind": "hang"})
    _install_backend(monkeypatch, client)
    with pytest.raises(AppException) as raised:
        await helper.llm_chat({}, {"prompt": "x", "params": {"timeout_ms": 30}})
    assert raised.value.status == 408
    assert client.ledger.cancelled, "the abandoned call must be cancelled, not left running"


async def test_neither_the_prompt_nor_the_reply_reaches_a_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret_prompt, secret_reply = "PROMPT-MARKER-4471", "REPLY-MARKER-9023"
    provider = {
        "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "deltas": [secret_reply], "usage": {"input_tokens": 7, "output_tokens": 5},
        "request_id": "req_log_001",
    }  # fmt: skip
    _install_backend(monkeypatch, _FakeClient(provider))
    _install_writer(monkeypatch)
    caplog.set_level(logging.DEBUG)
    await helper.llm_chat({}, {"prompt": secret_prompt, "system": "SYSTEM-MARKER-1188"})
    await helper.llm_stream({}, _event({"prompt": secret_prompt}))
    assert "llm.chat model=claude-opus-5-5" in caplog.text
    assert "llm.stream model=claude-opus-5-5" in caplog.text
    assert "request_id=req_log_001" in caplog.text
    for marker in (secret_prompt, secret_reply, "SYSTEM-MARKER-1188"):
        assert marker not in caplog.text


async def test_batch_timing_is_logged_when_asked_and_never_the_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    provider = {
        "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "deltas": ["alpha-MARKER-1", "beta-MARKER-2", "gamma"],
        "usage": {"input_tokens": 3, "output_tokens": 9},
    }  # fmt: skip
    _install_backend(monkeypatch, _FakeClient(provider), {"llm.log.batches": "true"})
    _install_writer(monkeypatch)
    caplog.set_level(logging.INFO)
    await helper.llm_stream({}, _event({"prompt": "x"}))
    lines = [r.getMessage() for r in caplog.records if "llm.stream batch=" in r.getMessage()]
    assert len(lines) == 3, lines
    assert lines[0].startswith("llm.stream batch=1 chars=14 t_ms=")
    assert lines[2].startswith("llm.stream batch=3 chars=5 t_ms=")
    assert "MARKER" not in caplog.text


async def test_batch_timing_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    provider = {
        "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "deltas": ["a", "b"], "usage": {"input_tokens": 1, "output_tokens": 2},
    }  # fmt: skip
    _install_backend(monkeypatch, _FakeClient(provider))
    _install_writer(monkeypatch)
    caplog.set_level(logging.DEBUG)
    await helper.llm_stream({}, _event({"prompt": "x"}))
    assert "llm.stream batch=" not in caplog.text


async def test_usage_rides_the_trace_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(helper, "annotate_trace", lambda key, value: seen.__setitem__(key, value))
    provider = {
        "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "deltas": ["ok"], "usage": {"input_tokens": 20, "output_tokens": 4},
        "request_id": "req_trace_001",
    }  # fmt: skip
    _install_backend(monkeypatch, _FakeClient(provider))
    await helper.llm_chat({}, {"prompt": "x"})
    assert seen == {
        "llm_model": "claude-opus-5-5",
        "llm_stop_reason": "end_turn",
        "llm_input_tokens": "20",
        "llm_output_tokens": "4",
        "llm_request_id": "req_trace_001",
    }


@pytest.mark.parametrize(
    ("model", "gets_fallbacks"),
    [
        ("claude-opus-5-5", True),
        ("claude-opus-5", True),
        ("claude-fable-5-1", True),
        ("claude-sonnet-5-5", True),
        ("claude-sonnet-5", False),
        ("claude-opus-4-8", False),
        ("claude-haiku-4-5", False),
    ],
)
def test_refusal_fallbacks_only_for_the_models_that_support_them(
    model: str, gets_fallbacks: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(None)
    _install_backend(monkeypatch, client)
    request = helper.prepare({"prompt": "x", "params": {"model": model}}, streaming=False)
    api, kwargs = helper._plan(request, helper.get_backend())
    assert (api is client.beta.messages) == gets_fallbacks
    assert ("fallbacks" in kwargs) == gets_fallbacks
    assert ("betas" in kwargs) == gets_fallbacks


def test_a_backend_can_spell_the_model_id_its_own_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam for a second route to the models: its model ids may carry a prefix."""
    client = _FakeClient(None)
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: default)
    backend = helper.Backend(
        "anthropic", client, True, provider_model=lambda model: f"anthropic.{model}"
    )
    request = helper.prepare({"prompt": "x"}, streaming=False)
    _api, kwargs = helper._plan(request, backend)
    assert kwargs["model"] == "anthropic.claude-opus-5-5"


def test_a_route_without_server_side_fallbacks_never_sends_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(None)
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: default)
    backend = helper.Backend("anthropic", client, supports_fallbacks=False)
    request = helper.prepare({"prompt": "x"}, streaming=False)
    api, kwargs = helper._plan(request, backend)
    assert api is client.messages
    assert "fallbacks" not in kwargs


# --- the health route and the backend seam -----------------------------------------------


async def test_health_reports_the_backend_and_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_backend(monkeypatch, _FakeClient(None))
    info = await helper.llm_health({"type": "info"}, None)
    assert info["service"] == "llm.helper"
    assert info["backend"] == "anthropic"
    assert info["model"] == "claude-opus-5-5"


async def test_health_is_up_when_a_credential_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_backend(monkeypatch, _FakeClient(None))
    assert await helper.llm_health({"type": "health"}, None) == "llm.helper is running fine"


async def test_health_is_down_without_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(None)
    client.api_key = None  # type: ignore[assignment]
    _install_backend(monkeypatch, client)
    with pytest.raises(AppException) as raised:
        await helper.llm_health({"type": "health"}, None)
    assert raised.value.status == 503
    assert "credential missing" in raised.value.message


def test_the_real_client_reports_a_credential_it_was_given() -> None:
    backend = helper.Backend("anthropic", anthropic.AsyncAnthropic(api_key="test-credential"), True)
    assert backend.credential_problem() is None


def test_an_unknown_backend_is_a_501_that_names_what_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper, "_backend", None)
    monkeypatch.setattr(
        helper, "_prop", lambda key, default=None: "bedrock" if key == "llm.backend" else default
    )
    with pytest.raises(AppException) as raised:
        helper.get_backend()
    assert raised.value.status == 501
    assert "unknown LLM backend 'bedrock'" in raised.value.message
    assert "this helper serves: anthropic" in raised.value.message


def test_the_default_backend_is_the_anthropic_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper, "_backend", None)
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: default)
    backend = helper.get_backend()
    assert backend.name == "anthropic"
    assert backend.supports_fallbacks is True
    assert isinstance(backend.client, anthropic.AsyncAnthropic)
    assert helper.get_backend() is backend, "the client is built once"
