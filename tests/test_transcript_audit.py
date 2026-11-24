"""The transcript audit store (RFC-0012) in the LLM helper - token-free.

Record version 2 and the facts a turn saves inside its compare-and-set, the two events a turn sends
to ``transcript.audit`` and the grace a closed record waits with, the function driven through its
mailbox (a write that fails and the check that repairs it before the release, an open record only
filled, the mark left to the check), the sweep, the audited mark in the memory and Redis working
stores, and the PostgreSQL store on a recording fake of the connection pool that pins every statement
and its parameters. The live test runs on a real PostgreSQL when LLM_TRANSCRIPT_AUDIT_TEST_URL is set.
The shared loop vector file gains no case: the audit is a side effect of a turn, not part of the loop
contract the twins repeat; the twins are held to one schema file and to the version 2 record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from test_llm_helper import (
    _TOOL,
    _TURN,
    _assert_error,
    _BrokenStore,
    _Clock,
    _event,
    _fake_redis,
    _FakeClient,
    _install_backend,
    _install_writer,
    helper,
)

from mercury_composable import AppException, FunctionRegistry, PostOffice, trace_context

_SCHEMA = Path(__file__).resolve().parent.parent / "extensions" / "llm-helper" / "schema"
_CONFIG = {"llm.transcript.store": "memory", "llm.transcript.audit": "postgres"}
_HEADERS = {"x-graph-id": "order-assistant", "x-node": "assistant", "my_correlation_id": "corr-1"}
_TRACE_ID = "3e9a" * 8
_TRACE_PATH = "POST /api/graph/order-assistant"
# the model's answer to the tool result: the closing turn of a two-turn conversation
_DONE = {
    **_TURN,
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "The limit is 500."}],
    "usage": {"input_tokens": 60, "output_tokens": 12, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 30},
    "request_id": "req_done",
}  # fmt: skip
_PROMPT = {"role": "user", "content": "What is the limit of C-1?"}
_RESULTS = [{"id": "toolu_01", "output": {"limit": 500}}]
_RESULT_MESSAGE = {
    "role": "user",
    "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": '{"limit":500}'}],
}
_CALLS = [{"id": "toolu_01", "name": "credit_limit", "input": {"id": "INPUT-MARKER-1"}}]


class _Sent:
    """The events llm.chat sends to transcript.audit, captured instead of delivered."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _capture(monkeypatch: pytest.MonkeyPatch) -> _Sent:
    sent = _Sent()
    monkeypatch.setattr(helper, "_audit_send", sent)
    return sent


def _configure(monkeypatch: pytest.MonkeyPatch, **settings: str) -> dict[str, str]:
    config = {**_CONFIG, **settings}
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: config.get(key, default))
    return config


async def _first_turn(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str], headers: dict[str, str] = _HEADERS
) -> str:
    """The first turn: the model asks for the tool. The conversation reference."""
    _install_backend(monkeypatch, _FakeClient(_TURN), config)
    reply = await helper.llm_chat(headers, {**_PROMPT_BODY})
    return str(reply["conversation"])


_PROMPT_BODY = {"prompt": _PROMPT["content"], "system": "Be brief.", "tools": [_TOOL]}


async def _closing_turn(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str], conversation: str, **fields: Any
) -> dict[str, Any]:
    """The second turn: the tool's result goes back and the model answers, closing the run."""
    _install_backend(monkeypatch, _FakeClient(_DONE), config)
    body = {"tools": [_TOOL], "conversation": conversation, "turn": 1, "results": _RESULTS, **fields}
    return await helper.llm_chat(_HEADERS, body)


def _facts(record: Any, turn: int) -> dict[str, Any]:
    return dict(record.turns[turn - 1])


# --- the record: version 2 and its facts ---------------------------------------------------


def test_a_version_2_record_round_trips_and_a_version_1_record_still_loads() -> None:
    record = helper.Transcript(
        "claude-haiku-4-5", [_PROMPT], 1, [], True, retention=60, correlation_id="c",
        graph_id="g", node="n", system="s", tools=[_TOOL], turns=[{"turn": 1}],
    )  # fmt: skip
    data = json.loads(record.to_json())
    assert data["v"] == 2
    assert list(data) == [
        "v", "model", "turn", "pending", "closed", "retention", "correlation_id", "graph_id",
        "node", "system", "tools", "turns", "messages",
    ]  # fmt: skip
    assert helper.Transcript.from_json(record.to_json()) == record
    # a conversation that was open when the helper was upgraded: no facts, the defaults
    version_1 = json.dumps({"v": 1, "model": "m", "turn": 1, "pending": [], "closed": False,
                            "messages": [_PROMPT]})  # fmt: skip
    loaded = helper.Transcript.from_json(version_1)
    assert loaded == helper.Transcript("m", [_PROMPT], 1, [])
    assert (loaded.retention, loaded.turns, loaded.tools, loaded.system) == (0, [], None, None)
    assert (loaded.correlation_id, loaded.graph_id, loaded.node) == (None, None, None)


async def test_a_turn_saves_its_facts_inside_the_compare_and_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every fact an audit row needs is in the record as the turn happens: identity from the trace
    context and the loop's headers, the call's UTC bounds, the outcome, the usage, and where the
    turn's request and response sit in the messages."""
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_store", store)
    config = {"llm.transcript.store": "memory"}
    with trace_context(_TRACE_ID, _TRACE_PATH, my_correlation_id="corr-1"):
        conversation = await _first_turn(monkeypatch, config, {"x-graph-id": "order-assistant",
                                                                "x-node": "assistant"})
        await _closing_turn(monkeypatch, config, conversation, retention="1h")
    record = await store.load(conversation)
    assert record is not None and record.closed and record.retention == 3600
    assert (record.correlation_id, record.graph_id, record.node) == (
        "corr-1", "order-assistant", "assistant",
    )  # fmt: skip
    assert (record.system, record.tools) == ("Be brief.", [_TOOL])
    first, second = record.turns
    assert first == {
        "turn": 1,
        "trace_id": _TRACE_ID,
        "trace_path": _TRACE_PATH,
        "correlation_id": "corr-1",
        "started_at": first["started_at"],
        "ended_at": first["ended_at"],
        "status": "tool_use",
        "stop_reason": "tool_use",
        "request_id": "req_loop_trace",
        "usage": {"input_tokens": 40, "output_tokens": 9, "cache_creation_input_tokens": 30,
                  "cache_read_input_tokens": 0},
        "messages": [0, 1],
    }  # fmt: skip
    assert (second["turn"], second["status"], second["stop_reason"]) == (2, "done", "end_turn")
    assert (second["request_id"], second["messages"]) == ("req_done", [2, 3])
    assert second["usage"]["cache_read_input_tokens"] == 30
    for stamp in (first["started_at"], first["ended_at"], second["started_at"]):
        assert len(stamp) == 24 and stamp.endswith("Z"), stamp  # ISO-8601 UTC, milliseconds
    assert first["started_at"] <= first["ended_at"] <= second["started_at"] <= second["ended_at"]
    # the messages the indexes point at: the prompt and the answer, the results and the answer
    assert record.messages[0] == _PROMPT and record.messages[1]["role"] == "assistant"
    assert record.messages[2] == _RESULT_MESSAGE and record.messages[3]["role"] == "assistant"


async def test_a_direct_call_records_the_header_view_and_no_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a trace context the correlation id comes from the read-only header view, the trace
    fields stay null, and a first turn that carried a conversation history points at all of it."""
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_store", store)
    config = {"llm.transcript.store": "memory"}
    _install_backend(monkeypatch, _FakeClient(_TURN), config)
    history = [_PROMPT, {"role": "assistant", "content": "Which customer?"},
               {"role": "user", "content": "C-1"}]  # fmt: skip
    reply = await helper.llm_chat({"my_correlation_id": "corr-2"},
                                  {"messages": history, "tools": [_TOOL]})  # fmt: skip
    record = await store.load(reply["conversation"])
    assert record is not None
    assert (record.correlation_id, record.graph_id, record.node, record.system) == (
        "corr-2", None, None, None,
    )  # fmt: skip
    fact = _facts(record, 1)
    assert (fact["trace_id"], fact["trace_path"], fact["correlation_id"]) == (None, None, "corr-2")
    assert fact["messages"] == [0, 3], "the request of the first turn is every message it sent"


# --- the events behind a turn, and the grace -------------------------------------------------


async def test_a_turn_sends_its_write_and_a_closing_turn_the_check_behind_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the compare-and-set accepted the turn: one write event; a closing turn adds the
    check. The closed record waits for the check with the audit's grace, not the retention."""
    sent = _capture(monkeypatch)
    clock = _Clock()
    store = helper.MemoryStore(clock)
    monkeypatch.setattr(helper, "_store", store)
    conversation = await _first_turn(monkeypatch, _CONFIG)
    assert sent.events == [{"conversation": conversation, "turn": 1}]
    assert store._records[conversation][0] - clock.now == 604800, "an open record keeps its ttl"
    await _closing_turn(monkeypatch, _CONFIG, conversation)
    assert sent.events[1:] == [
        {"conversation": conversation, "turn": 2},
        {"conversation": conversation, "check": True},
    ]
    assert store._records[conversation][0] - clock.now == 86400, "the grace (1d), not retention 0"
    record = await store.load(conversation)
    assert record is not None and record.closed and record.retention == 0


async def test_the_grace_is_configurable_and_a_cut_off_turn_waits_for_its_check_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _capture(monkeypatch)
    clock = _Clock()
    store = helper.MemoryStore(clock)
    monkeypatch.setattr(helper, "_store", store)
    config = {**_CONFIG, "llm.transcript.audit.grace": "2h"}
    cut_off = {**_TURN, "stop_reason": "max_tokens", "content": [{"type": "text", "text": "Th"}]}
    _install_backend(monkeypatch, _FakeClient(cut_off), config)
    reply = await helper.llm_chat(_HEADERS, {**_PROMPT_BODY})
    assert reply["status"] == "cap" and reply["cap"] == "max_tokens"
    conversation = reply["conversation"]
    assert sent.events == [{"conversation": conversation, "turn": 1},
                           {"conversation": conversation, "check": True}]  # fmt: skip
    assert store._records[conversation][0] - clock.now == 7200


async def test_without_an_audit_store_a_turn_sends_nothing_and_the_retention_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _capture(monkeypatch)
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_store", store)
    config = {"llm.transcript.store": "memory"}
    conversation = await _first_turn(monkeypatch, config)
    await _closing_turn(monkeypatch, config, conversation)
    assert sent.events == []
    assert await store.load(conversation) is None, "retention 0 deletes the record at the close"


async def test_a_send_that_fails_is_logged_and_never_refuses_the_turn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The route is not registered (the boot did not run, or the function is gone): the bus
    answers 404, the turn is still answered, and the sweep's job is noted in the log."""
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_store", store)
    monkeypatch.setattr(helper, "_post_office", PostOffice(registry=FunctionRegistry()))
    caplog.set_level(logging.WARNING)
    conversation = await _first_turn(monkeypatch, _CONFIG)
    assert await store.load(conversation) is not None
    assert "transcript.audit event not accepted - status=404" in caplog.text


# --- the function, driven through its mailbox ------------------------------------------------


class _RowError(Exception):
    """A database error whose message details the failing row, as a constraint violation does."""

    sqlstate = "23514"

    def __init__(self) -> None:
        super().__init__(
            'new row for relation "transcript_turn" violates check constraint\n'
            "DETAIL: Failing row contains (SECRET-MARKER-ROW)."
        )


class _FlakyAudit(helper.MemoryAuditStore):
    """An audit store whose first writes fail."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def write(self, conversation: dict[str, Any], turns: list[dict[str, Any]]) -> None:
        if self.failures:
            self.failures -= 1
            raise _RowError()
        await super().write(conversation, turns)


def _expires(started_at: str) -> str:
    return helper._plus_seconds(started_at, 365 * 86400)


async def test_a_write_event_persists_the_turn_row_and_the_conversation_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write of turn N builds its row from the record - the facts, the messages the turn sent
    and the blocks it got back - upserts the conversation row and marks the record audited."""
    audit = helper.MemoryAuditStore()
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    _capture(monkeypatch)
    with trace_context(_TRACE_ID, _TRACE_PATH, my_correlation_id="corr-1"):
        conversation = await _first_turn(monkeypatch, _CONFIG)
    record = await store.load(conversation)
    assert record is not None
    fact = _facts(record, 1)
    await helper.transcript_audit({}, {"conversation": conversation, "turn": 1})
    row = audit.conversations[conversation]
    assert row == {
        "conversation": conversation,
        "correlation_id": "corr-1",
        "graph_id": "order-assistant",
        "node": "assistant",
        "model": "claude-opus-5-5",
        "status": "open",
        "turns": 1,
        "started_at": fact["started_at"],
        "updated_at": row["updated_at"],
        "ended_at": None,
        "expires_at": _expires(fact["started_at"]),
        "system": "Be brief.",
        "tools": [_TOOL],
        "input_tokens": 40,
        "output_tokens": 9,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 0,
    }
    assert row["updated_at"] >= fact["ended_at"]
    assert audit.turn_rows[conversation] == {
        1: {
            "conversation": conversation,
            "turn": 1,
            "trace_id": _TRACE_ID,
            "trace_path": _TRACE_PATH,
            "correlation_id": "corr-1",
            "started_at": fact["started_at"],
            "ended_at": fact["ended_at"],
            "stop_reason": "tool_use",
            "status": "tool_use",
            "request_id": "req_loop_trace",
            "input_tokens": 40,
            "output_tokens": 9,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 0,
            "request": [_PROMPT],
            "response": _TURN["content"],
            "calls": _CALLS,
        }
    }
    assert await store.audited(conversation) == 1
    # the closing turn: its write leaves the mark to the check behind it, and sets the status
    await _closing_turn(monkeypatch, _CONFIG, conversation)
    record = await store.load(conversation)
    assert record is not None
    second = _facts(record, 2)
    await helper.transcript_audit({}, {"conversation": conversation, "turn": 2})
    row = audit.conversations[conversation]
    assert (row["status"], row["turns"], row["ended_at"]) == ("done", 2, second["ended_at"])
    assert (row["input_tokens"], row["output_tokens"], row["cache_creation_input_tokens"],
            row["cache_read_input_tokens"]) == (100, 21, 30, 30)  # fmt: skip
    assert (row["started_at"], row["expires_at"]) == (fact["started_at"], _expires(fact["started_at"]))
    turn_2 = audit.turn_rows[conversation][2]
    assert (turn_2["status"], turn_2["stop_reason"], turn_2["request_id"]) == (
        "done", "end_turn", "req_done",
    )  # fmt: skip
    assert turn_2["request"] == [_RESULT_MESSAGE]
    assert turn_2["response"] == [{"type": "text", "text": "The limit is 500."}]
    assert turn_2["calls"] is None
    assert await store.audited(conversation) == 1, "a closing write leaves the mark to the check"
    assert await store.load(conversation) is not None


async def test_a_failed_write_is_logged_by_type_and_the_check_repairs_it_before_the_release(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Eric's integrity step: the check counts the turns the record holds against the rows the
    store holds, writes the missing ones, and only then releases the working record. A failure
    reaches the log as a type and a SQLSTATE, never as the row the database quotes."""
    audit = _FlakyAudit(failures=1)
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    _capture(monkeypatch)
    caplog.set_level(logging.INFO)
    conversation = await _first_turn(monkeypatch, _CONFIG)
    await helper.transcript_audit({}, {"conversation": conversation, "turn": 1})
    assert audit.turn_rows == {}, "the first write failed"
    assert f"transcript.audit conversation={conversation} write failed - _RowError sqlstate=23514" in (
        caplog.text
    )
    assert "SECRET-MARKER" not in caplog.text and "Failing row" not in caplog.text
    assert await store.audited(conversation) == 0
    await _closing_turn(monkeypatch, _CONFIG, conversation)
    await helper.transcript_audit({}, {"conversation": conversation, "turn": 2})
    assert set(audit.turn_rows[conversation]) == {1, 2}, "a write fills every turn the store lacks"
    assert await store.load(conversation) is not None, "a write never releases"
    await helper.transcript_audit({}, {"conversation": conversation, "check": True})
    assert set(audit.turn_rows[conversation]) == {1, 2}
    assert audit.conversations[conversation]["status"] == "done"
    assert await store.load(conversation) is None, "released: retention 0 deletes the record"
    assert f"conversation={conversation} turn=2 written=0 check=True released=True" in caplog.text
    for marker in ("PROMPT", "INPUT-MARKER", "Be brief", "The limit is", "limit"):
        assert marker not in caplog.text, marker


async def test_the_check_releases_a_closed_record_to_its_retention_and_marks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = helper.MemoryAuditStore()
    clock = _Clock()
    store = helper.MemoryStore(clock)
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    _capture(monkeypatch)
    conversation = await _first_turn(monkeypatch, _CONFIG)
    await _closing_turn(monkeypatch, _CONFIG, conversation, retention="30m")
    assert store._records[conversation][0] - clock.now == 86400
    await helper.transcript_audit({}, {"conversation": conversation, "check": True})
    assert set(audit.turn_rows[conversation]) == {1, 2}
    assert store._records[conversation][0] - clock.now == 1800, "the retention the turn carried"
    assert await store.audited(conversation) == 2, "the mark says the check is done"


async def test_a_check_never_releases_an_open_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run waiting for a tool result, a person or the loop's next turn is filled, never released
    - it may resume; the sweep sends such checks for records whose audit lags."""
    audit = helper.MemoryAuditStore()
    clock = _Clock()
    store = helper.MemoryStore(clock)
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    _capture(monkeypatch)
    conversation = await _first_turn(monkeypatch, _CONFIG)
    await helper.transcript_audit({}, {"conversation": conversation, "check": True})
    assert set(audit.turn_rows[conversation]) == {1}
    assert audit.conversations[conversation]["status"] == "open"
    assert store._records[conversation][0] - clock.now == 604800, "its time to live stays"
    assert await store.audited(conversation) == 1
    # the run goes on: the later turn is accepted over the audited record
    await _closing_turn(monkeypatch, _CONFIG, conversation)
    record = await store.load(conversation)
    assert record is not None and record.turn == 2


async def test_a_record_that_is_gone_or_from_before_version_2_is_handled_by_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    audit = helper.MemoryAuditStore()
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    _configure(monkeypatch)
    caplog.set_level(logging.INFO)
    gone = "e" * 32
    await helper.transcript_audit({}, {"conversation": gone, "turn": 1})
    assert f"conversation={gone} no working record" in caplog.text
    await helper.transcript_audit({}, {"conversation": "not-a-reference", "turn": 1})
    assert "event without a conversation reference ignored" in caplog.text
    # a version 1 record, open when the helper was upgraded: its turns carry no facts
    old = "f" * 32
    version_1 = helper.Transcript("claude-opus-5-5", [_PROMPT, {"role": "assistant",
                                  "content": _TURN["content"]}], 1, ["toolu_01"])  # fmt: skip
    assert await store.save(old, 0, version_1, 600)
    await helper.transcript_audit({}, {"conversation": old, "check": True})
    assert f"conversation={old} turn(s) 1 carry no facts - unaudited" in caplog.text
    assert audit.conversations[old]["status"] == "open" and audit.turn_rows[old] == {}
    assert await store.audited(old) == 1, "nothing more can be done for it"
    assert "INPUT-MARKER" not in caplog.text


async def test_the_mailbox_runs_the_write_and_the_check_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real path: llm.chat sends the events over the in-process bus to the function with one
    instance; the check behind the closing write finds every turn and releases the record."""
    registry = FunctionRegistry()
    registry.register(helper.AUDIT_ROUTE, helper.transcript_audit, instances=1, private=True)
    monkeypatch.setattr(helper, "_post_office", PostOffice(registry=registry))
    audit = helper.MemoryAuditStore()
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    try:
        conversation = await _first_turn(monkeypatch, _CONFIG)
        await _closing_turn(monkeypatch, _CONFIG, conversation)
        for _ in range(200):
            if conversation not in store._records:
                break
            await asyncio.sleep(0.005)
        assert conversation not in store._records, "the check released the record"
        assert set(audit.turn_rows[conversation]) == {1, 2}
        assert audit.conversations[conversation]["status"] == "done"
        assert audit.conversations[conversation]["turns"] == 2
    finally:
        await registry.bus.close()


# --- the sweep -------------------------------------------------------------------------------


async def test_the_sweep_sends_a_check_for_every_record_whose_audit_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_store", store)
    sent = _capture(monkeypatch)
    _configure(monkeypatch)
    behind, current, gone = "a" * 32, "b" * 32, "c" * 32
    record = helper.Transcript("claude-opus-5-5", [_PROMPT], 1, ["toolu_01"])
    for conversation in (behind, current):
        assert await store.save(conversation, 0, record, 600)
    await store.mark_audited(current, 1)
    assert await helper.audit_sweep() == 1
    assert sent.events == [{"conversation": behind, "check": True}]
    assert gone not in store._records
    await store.mark_audited(behind, 1)
    assert await helper.audit_sweep() == 0 and len(sent.events) == 1


# --- the audited mark in the Redis working store ---------------------------------------------


async def test_the_redis_store_keeps_the_audited_mark_beside_the_turn_and_the_record() -> None:
    """The compare-and-set never touches the mark, the mark never resurrects a released record,
    the release applies the retention, and the sweep reads two fields per live record."""
    client = _fake_redis()
    store = helper.RedisStore(client)
    conversation = "a" * 32
    key = "transcript:" + conversation
    record = helper.Transcript("claude-opus-5-5", [_PROMPT], 1, ["toolu_01"])
    assert await store.save(conversation, 0, record, 600)
    assert await store.audited(conversation) == 0
    await store.mark_audited(conversation, 1)
    assert await client.hget(key, "audited") == "1"
    assert 599 <= await client.ttl(key) <= 600, "the mark leaves the time to live alone"
    later = helper.Transcript("claude-opus-5-5", [*record.messages, _RESULT_MESSAGE], 2, [])
    assert await store.save(conversation, 1, later, 600)
    assert await store.audited(conversation) == 1, "the compare-and-set leaves the mark alone"
    assert await client.set(helper.AUDIT_SWEEP_LOCK, "x")  # the lock under the prefix is skipped
    assert await store.unaudited() == [conversation]
    await store.mark_audited(conversation, 2)
    assert await store.unaudited() == []
    await store.mark_audited("b" * 32, 1)
    assert await client.exists("transcript:" + "b" * 32) == 0, "no record, no hash"
    await store.release(conversation, 60)
    assert 59 <= await client.ttl(key) <= 60
    await store.release(conversation, 0)
    assert await client.exists(key) == 0
    await store.release(conversation, 0)  # gone already: nothing to do


async def test_the_redis_sweep_lock_is_one_per_interval() -> None:
    client = _fake_redis()
    store = helper.RedisStore(client)
    assert await store.sweep_lock(60000) is True
    assert await store.sweep_lock(60000) is False, "another replica holds this interval"
    assert 59 <= await client.ttl(helper.AUDIT_SWEEP_LOCK) <= 60
    assert await helper.MemoryStore().sweep_lock(60000) is True, "one process sweeps its own"


# --- the PostgreSQL store on a recording fake of the pool ------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _FakeCursor:
        self._pool.log.append((sql, params))
        return _FakeCursor(self._pool.rows if sql == helper.SQL_TURNS else [])

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self._pool.log.append(("BEGIN", None))
        yield
        self._pool.log.append(("COMMIT", None))


class _FakePool:
    """psycopg_pool.AsyncConnectionPool as the store uses it, recording every statement."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None,
                 open_error: Exception | None = None) -> None:  # fmt: skip
        self.log: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows = rows or []
        self.opened: tuple[bool, float] | None = None
        self.closed = False
        self._open_error = open_error

    async def open(self, wait: bool, timeout: float) -> None:
        self.opened = (wait, timeout)
        if self._open_error is not None:
            raise self._open_error

    async def close(self) -> None:
        self.closed = True

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_FakeConnection]:
        yield _FakeConnection(self)


def _conversation_row(conversation: str) -> dict[str, Any]:
    return {
        "conversation": conversation, "correlation_id": "corr-1", "graph_id": "order-assistant",
        "node": "assistant", "model": "claude-opus-5-5", "status": "done", "turns": 2,
        "started_at": "2026-11-17T01:02:03.456Z", "updated_at": "2026-11-17T01:02:06.000Z",
        "ended_at": "2026-11-17T01:02:05.012Z", "expires_at": "2027-11-17T01:02:03.456Z",
        "system": "Be brief.", "tools": [_TOOL],
    }  # fmt: skip


def _turn_row(conversation: str) -> dict[str, Any]:
    return {
        "conversation": conversation, "turn": 1, "trace_id": _TRACE_ID, "trace_path": _TRACE_PATH,
        "correlation_id": "corr-1", "started_at": "2026-11-17T01:02:03.456Z",
        "ended_at": "2026-11-17T01:02:05.012Z", "stop_reason": "tool_use", "status": "tool_use",
        "request_id": "req_loop_trace", "input_tokens": 40, "output_tokens": 9,
        "cache_creation_input_tokens": 30, "cache_read_input_tokens": 0, "request": [_PROMPT],
        "response": _TURN["content"], "calls": _CALLS,
    }  # fmt: skip


async def test_the_postgres_store_runs_the_statements_in_one_transaction() -> None:
    """The exact statements and parameters: JSON as compact text cast to jsonb, the stamps as text
    cast to timestamptz, the totals recomputed from the turn rows - the Node twin runs the same."""
    conversation = "d" * 32
    pool = _FakePool(rows=[(1,), (2,)])
    store = helper.PostgresAuditStore(pool, 2.5)
    await store.open()
    assert pool.opened == (True, 2.5)
    assert await store.turns(conversation) == {1, 2}
    assert pool.log == [(helper.SQL_TURNS, (conversation,))]
    pool.log.clear()
    await store.write(_conversation_row(conversation), [_turn_row(conversation)])
    tools = json.dumps([_TOOL], separators=(",", ":"))
    assert pool.log == [
        ("BEGIN", None),
        (
            helper.SQL_UPSERT_CONVERSATION,
            (conversation, "corr-1", "order-assistant", "assistant", "claude-opus-5-5", "done", 2,
             "2026-11-17T01:02:03.456Z", "2026-11-17T01:02:06.000Z", "2026-11-17T01:02:05.012Z",
             "2027-11-17T01:02:03.456Z", "Be brief.", tools),
        ),
        (
            helper.SQL_INSERT_TURN,
            (conversation, 1, _TRACE_ID, _TRACE_PATH, "corr-1", "2026-11-17T01:02:03.456Z",
             "2026-11-17T01:02:05.012Z", "tool_use", "tool_use", "req_loop_trace", 40, 9, 30, 0,
             json.dumps([_PROMPT], separators=(",", ":")),
             json.dumps(_TURN["content"], separators=(",", ":")),
             json.dumps(_CALLS, separators=(",", ":"))),
        ),
        (helper.SQL_TOTALS, (conversation, conversation)),
        ("COMMIT", None),
    ]  # fmt: skip
    assert "ON CONFLICT (conversation) DO UPDATE" in helper.SQL_UPSERT_CONVERSATION
    assert "ON CONFLICT (conversation, turn) DO NOTHING" in helper.SQL_INSERT_TURN
    await store.close()
    assert pool.closed


async def test_a_null_json_column_stays_null() -> None:
    pool = _FakePool()
    store = helper.PostgresAuditStore(pool)
    row = {**_turn_row("d" * 32), "calls": None}
    await store.write({**_conversation_row("d" * 32), "tools": None}, [row])
    assert pool.log[1][1] is not None and pool.log[1][1][-1] is None, "tools NULL"
    assert pool.log[2][1] is not None and pool.log[2][1][-1] is None, "calls NULL"


def test_the_postgres_store_is_built_from_its_connection_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {
        "llm.transcript.audit": "postgres",
        "llm.transcript.audit.postgres.host": "postgres.internal",
        "llm.transcript.audit.postgres.port": "5433",
        "llm.transcript.audit.postgres.database": "audit",
        "llm.transcript.audit.postgres.username": "transcript_writer",
        "llm.transcript.audit.postgres.password": "test-only-password",
        "llm.transcript.audit.postgres.ssl": "false",
        "llm.transcript.audit.postgres.timeout.ms": "2500",
    }
    monkeypatch.setattr(helper, "_audit", None)
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: settings.get(key, default))
    store = helper.get_audit_store()
    assert isinstance(store, helper.PostgresAuditStore)
    assert helper.get_audit_store() is store, "built once"
    pool = store._pool
    assert pool.kwargs == {
        "host": "postgres.internal",
        "port": 5433,
        "dbname": "audit",
        "user": "transcript_writer",
        "password": "test-only-password",
        "sslmode": "disable",
        "connect_timeout": 2,
        "options": "-c statement_timeout=2500",
    }
    assert (pool.min_size, pool.max_size, pool.timeout) == (1, 4, 2.5)
    assert store._timeout_s == 2.5
    # the defaults: localhost, the mercury database, TLS required, no credential in a file
    monkeypatch.setattr(helper, "_audit", None)
    monkeypatch.setattr(
        helper, "_prop",
        lambda key, default=None: "postgres" if key == "llm.transcript.audit" else default,
    )  # fmt: skip
    pool = helper.get_audit_store()._pool
    assert pool.kwargs == {
        "host": "127.0.0.1", "port": 5432, "dbname": "mercury", "sslmode": "require",
        "connect_timeout": 5, "options": "-c statement_timeout=5000",
    }  # fmt: skip


def test_the_postgres_store_without_psycopg_is_a_503_that_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper, "_audit", None)
    _configure(monkeypatch)
    monkeypatch.setattr(helper.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(AppException) as raised:
        helper.get_audit_store()
    assert raised.value.status == 503
    assert "mercury-composable[postgres]" in raised.value.message


def test_an_audit_store_that_is_not_configured_or_unknown_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper, "_audit", None)
    monkeypatch.setattr(helper, "_prop", lambda _key, default=None: default)
    assert helper.audit_configured() is False
    with pytest.raises(AppException) as raised:
        helper.get_audit_store()
    assert raised.value.status == 503 and "llm.transcript.audit" in raised.value.message
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: "mongo" if "audit" in key else default)
    with pytest.raises(AppException) as raised:
        helper.get_audit_store()
    assert raised.value.status == 500
    assert raised.value.message == "llm.transcript.audit must be one of: postgres"


# --- the boot ----------------------------------------------------------------------------------


async def test_the_boot_registers_the_function_once_opens_the_store_and_starts_the_sweep(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _FakePool()
    monkeypatch.setattr(helper, "_audit", helper.PostgresAuditStore(pool))
    monkeypatch.setattr(helper, "_sweep_task", None)
    _configure(monkeypatch, **{"llm.transcript.audit.sweep.ms": "50"})
    caplog.set_level(logging.INFO)
    await helper.audit_boot()
    service = helper.default_registry.get(helper.AUDIT_ROUTE)
    assert service is not None and service.instances == 1 and service.private
    assert service.handler is helper.transcript_audit
    assert pool.opened == (True, 5.0)
    assert "transcript audit store postgres ready" in caplog.text
    task = helper._sweep_task
    assert task is not None and not task.done()
    await helper.audit_boot()  # again: one registration, one sweep
    assert helper._sweep_task is task
    await helper.audit_shutdown()
    assert helper._sweep_task is None and task.cancelled()
    assert pool.closed


async def test_a_store_that_does_not_answer_at_boot_is_logged_and_the_application_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _FakePool(open_error=TimeoutError("no server at 127.0.0.1:5432"))
    monkeypatch.setattr(helper, "_audit", helper.PostgresAuditStore(pool))
    monkeypatch.setattr(helper, "_sweep_task", None)
    _configure(monkeypatch, **{"llm.transcript.audit.sweep.ms": "0"})
    caplog.set_level(logging.WARNING)
    await helper.audit_boot()
    assert ("transcript audit store postgres not answering at start - TimeoutError: no server at "
            "127.0.0.1:5432; the sweep repairs what the outage defers") in caplog.text
    assert helper._sweep_task is None, "sweep.ms 0 disables the sweep"


async def test_a_configured_driver_that_is_not_installed_stops_the_application_by_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(helper, "_audit", None)
    _configure(monkeypatch)
    monkeypatch.setattr(helper.importlib.util, "find_spec", lambda _name: None)
    caplog.set_level(logging.ERROR)
    with pytest.raises(SystemExit):
        await helper.audit_boot()
    assert "transcript audit store cannot start" in caplog.text
    assert "mercury-composable[postgres]" in caplog.text


async def test_the_boot_does_nothing_without_an_audit_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper, "_sweep_task", None)
    monkeypatch.setattr(helper, "_prop", lambda _key, default=None: default)
    await helper.audit_boot()
    assert helper._sweep_task is None


async def test_health_reports_the_audit_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_backend(monkeypatch, _FakeClient(None), _CONFIG)
    info = await helper.llm_health({"type": "info"}, None)
    assert (info["transcript_store"], info["transcript_audit"]) == ("memory", "postgres")
    _install_backend(monkeypatch, _FakeClient(None), {"llm.transcript.store": "memory"})
    assert "transcript_audit" not in await helper.llm_health({"type": "info"}, None)


def test_the_failure_text_never_quotes_a_row() -> None:
    assert helper._audit_failure(_RowError()) == "_RowError sqlstate=23514"
    assert helper._audit_failure(ConnectionRefusedError("Connection refused")) == (
        "ConnectionRefusedError: Connection refused"
    )
    assert helper._audit_failure(AppException(503, MISSING := helper.MISSING_AUDIT)) == (
        f"AppException: {MISSING}"
    )
    assert helper._audit_failure(ValueError("x\nDETAIL: a row")) == "ValueError"


# --- the schema files --------------------------------------------------------------------------


def test_the_schema_files_are_the_published_ones() -> None:
    """The reference schema and the purge, shipped with the helper and byte-identical in the Node
    twin: the two tables, the status values, the three indexes, the two purge statements."""
    schema = (_SCHEMA / "postgres.sql").read_text(encoding="utf-8")
    for table in ("transcript_schema", "transcript (", "transcript_turn ("):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
    assert "CHECK (status IN ('open','done','refusal','cap','expired'))" in schema
    assert "CHECK (status IN ('done','tool_use','refusal','cap'))" in schema
    for index in ("transcript_correlation_id", "transcript_started_at", "transcript_expires_at",
                  "transcript_turn_trace_id", "transcript_turn_correlation_id",
                  "transcript_turn_started_at"):  # fmt: skip
        assert f"CREATE INDEX IF NOT EXISTS {index} " in schema
    assert "REFERENCES transcript (conversation) ON DELETE CASCADE" in schema
    purge = (_SCHEMA / "purge.sql").read_text(encoding="utf-8")
    assert "DELETE FROM transcript WHERE expires_at < now();" in purge
    assert ("UPDATE transcript SET status = 'expired' WHERE status = 'open' AND updated_at < now() "
            "- interval '7 days';") in purge


# --- the live store ----------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("LLM_TRANSCRIPT_AUDIT_TEST_URL"),
    reason="a live PostgreSQL: set LLM_TRANSCRIPT_AUDIT_TEST_URL, e.g. "
    "postgres://mercury:...@127.0.0.1:5432/mercury?sslmode=disable",
)
async def test_the_rows_land_on_a_live_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schema applied, a two-turn conversation written through the function (a write, a
    closing write, the check), the section-6 questions answered, a repeated check idempotent."""
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        os.environ["LLM_TRANSCRIPT_AUDIT_TEST_URL"], min_size=1, max_size=2, open=False
    )
    audit = helper.PostgresAuditStore(pool, 10.0)
    await audit.open()
    store = helper.MemoryStore()
    monkeypatch.setattr(helper, "_audit", audit)
    monkeypatch.setattr(helper, "_store", store)
    _capture(monkeypatch)
    conversation = None
    try:
        async with pool.connection() as connection:
            # several statements, no parameters: the simple query protocol runs the whole file
            await connection.execute((_SCHEMA / "postgres.sql").read_bytes())
        with trace_context(_TRACE_ID, _TRACE_PATH, my_correlation_id="corr-live"):
            conversation = await _first_turn(monkeypatch, _CONFIG)
            await helper.transcript_audit({}, {"conversation": conversation, "turn": 1})
            await _closing_turn(monkeypatch, _CONFIG, conversation, retention="1h")
        await helper.transcript_audit({}, {"conversation": conversation, "turn": 2})
        await helper.transcript_audit({}, {"conversation": conversation, "check": True})
        await helper.transcript_audit({}, {"conversation": conversation, "check": True})
        assert await store.audited(conversation) == 2
        async with pool.connection() as connection:
            head = await (await connection.execute(
                "SELECT status, turns, graph_id, node, correlation_id, input_tokens, output_tokens, "
                "cache_creation_input_tokens, cache_read_input_tokens, system, tools, "
                "ended_at IS NOT NULL, expires_at - started_at FROM transcript WHERE conversation = %s",
                (conversation,),
            )).fetchone()
            turns = await (await connection.execute(
                "SELECT turn, trace_id, status, stop_reason, request, response, calls "
                "FROM transcript_turn WHERE correlation_id = %s ORDER BY turn",
                ("corr-live",),
            )).fetchall()
            by_trace = await (await connection.execute(
                "SELECT count(*) FROM transcript_turn WHERE trace_id = %s", (_TRACE_ID,)
            )).fetchone()
        assert head is not None
        assert head[:10] == ("done", 2, "order-assistant", "assistant", "corr-live", 100, 21, 30, 30,
                             "Be brief.")  # fmt: skip
        assert head[10] == [_TOOL] and head[11] is True
        assert head[12].days == 365
        assert [(t[0], t[1], t[2], t[3]) for t in turns] == [
            (1, _TRACE_ID, "tool_use", "tool_use"), (2, _TRACE_ID, "done", "end_turn"),
        ]  # fmt: skip
        assert turns[0][4] == [_PROMPT] and turns[0][5] == _TURN["content"] and turns[0][6] == _CALLS
        assert turns[1][4] == [_RESULT_MESSAGE] and turns[1][6] is None
        assert by_trace == (2,)
    finally:
        if conversation is not None:
            async with pool.connection() as connection:
                await connection.execute(
                    "DELETE FROM transcript WHERE conversation = %s", (conversation,)
                )
        await audit.close()


# --- a single call (RFC-0015): a conversation of one turn, carried by the working record ----------
#
# tests/vectors/llm-audit-vectors.json (byte-identical in mercury-nodejs) states the rows a call
# without tools leaves and when it leaves none. Every case runs the real path: the call, the save
# the reply does not wait for, the check event over the in-process bus, the unchanged function.

_AUDIT_VECTORS = Path(__file__).resolve().parent / "vectors" / "llm-audit-vectors.json"
# the Node.js twin pins the same digest: change the file in both packs or in neither
_AUDIT_VECTORS_SHA256 = "d6f5ff9ba8d56d7397e13db406c9d31a146aaf7d4e5d69d219df8bc2ff8ab57f"
AUDIT_VECTORS: dict[str, Any] = json.loads(_AUDIT_VECTORS.read_text(encoding="utf-8"))
SINGLE_CASES: list[dict[str, Any]] = AUDIT_VECTORS["cases"]
_SINGLE = {**_CONFIG, "llm.transcript.audit.single": "true"}
_REFERENCE = re.compile(r"[0-9a-f]{32}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_ANSWER = {
    "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "REPLY-MARKER-1"}],
    "usage": {"input_tokens": 11, "output_tokens": 3}, "request_id": "req_single_test",
}  # fmt: skip


class _SingleRig:
    """The real path under a single call: the function behind its mailbox, the memory working
    store, the reference audit store, and every event the helper sends, kept in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, audit: Any = None, store: Any = None) -> None:
        self.registry = FunctionRegistry()
        self.registry.register(
            helper.AUDIT_ROUTE, helper.transcript_audit, instances=1, private=True
        )
        monkeypatch.setattr(helper, "_post_office", PostOffice(registry=self.registry))
        self.audit = audit or helper.MemoryAuditStore()
        self.store = store or helper.MemoryStore()
        monkeypatch.setattr(helper, "_audit", self.audit)
        monkeypatch.setattr(helper, "_store", self.store)
        self.events: list[dict[str, Any]] = []
        deliver = helper._audit_send

        async def recording(event: dict[str, Any]) -> None:
            self.events.append(event)
            await deliver(event)

        monkeypatch.setattr(helper, "_audit_send", recording)

    async def settled(self, done: Any) -> None:
        """Wait for the saves in flight, then for the mailbox to reach ``done()``."""
        await helper.audit_single_idle()
        for _ in range(400):
            if done():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("the audit function did not settle")

    async def close(self) -> None:
        await self.registry.bus.close()


def test_the_audit_vector_file_is_the_one_the_node_twin_runs() -> None:
    digest = hashlib.sha256(_AUDIT_VECTORS.read_bytes()).hexdigest()
    assert digest == _AUDIT_VECTORS_SHA256, "the shared vector file changed - update both packs"
    assert (AUDIT_VECTORS["contract"], AUDIT_VECTORS["version"]) == ("llm-audit-single", 1)
    assert {case["route"] for case in SINGLE_CASES} == {"llm.chat", "llm.stream"}


async def _single_call(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the case's call and check what the caller received."""
    client = _FakeClient(case.get("provider"))
    _install_backend(monkeypatch, client, case.get("config"))
    wanted = case["expect"]["caller"]
    headers = dict(case.get("headers", {}))
    if case["route"] == "llm.stream":
        out = _install_writer(monkeypatch)
        await helper.llm_stream(headers, _event(case["body"]))
        if "error" in wanted:
            _assert_error(out.error, wanted["error"])
        else:
            assert out.error is None and out.segments == wanted["frames"]
        return
    try:
        result = await helper.llm_chat(headers, case["body"])
    except AppException as error:
        assert "error" in wanted, error
        _assert_error(error, wanted["error"])
        return
    assert "error" not in wanted, "an error was expected"
    for key, value in wanted["result"].items():
        assert result[key] == value, key
    assert "conversation" not in result, "a single call's reply carries no reference"


@pytest.mark.parametrize("case", SINGLE_CASES, ids=[c["name"] for c in SINGLE_CASES])
async def test_single_call_audit_contract(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _SingleRig(monkeypatch)
    expect = case["expect"]
    try:
        trace = case.get("trace")
        if trace:
            with trace_context(trace["trace_id"], trace["trace_path"]):
                await _single_call(case, monkeypatch)
        else:
            await _single_call(case, monkeypatch)
        if expect["conversation"] is None:
            await rig.settled(lambda: True)
            assert expect["turn"] is None
            assert rig.events == [] and rig.store._records == {}
            assert rig.audit.conversations == {} and rig.audit.turn_rows == {}
            return
        await rig.settled(lambda: bool(rig.audit.conversations) and not rig.store._records)
        ((conversation, row),) = rig.audit.conversations.items()
        assert _REFERENCE.fullmatch(conversation) and row["conversation"] == conversation
        assert rig.events == [{"conversation": conversation, "check": True}]
        assert set(rig.audit.turn_rows[conversation]) == {1}
        turn = rig.audit.turn_rows[conversation][1]
        wanted = dict(expect["conversation"])
        expires_after_s = wanted.pop("expires_after_s")
        decided_by_the_run = ("conversation", "started_at", "updated_at", "ended_at", "expires_at")
        assert {k: v for k, v in row.items() if k not in decided_by_the_run} == wanted
        assert {
            k: v for k, v in turn.items() if k not in ("conversation", "started_at", "ended_at")
        } == expect["turn"]
        assert turn["conversation"] == conversation
        for stamp in (row["started_at"], row["updated_at"], row["ended_at"], row["expires_at"]):
            assert _STAMP.fullmatch(stamp), stamp
        assert (turn["started_at"], turn["ended_at"]) == (row["started_at"], row["ended_at"])
        assert row["started_at"] <= row["ended_at"] <= row["updated_at"]
        assert row["expires_at"] == helper._plus_seconds(row["started_at"], expires_after_s)
    finally:
        await rig.close()


async def test_the_reply_does_not_wait_for_the_save_and_the_shutdown_drains_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit never sits on the call's path: with a working store that does not answer, the
    call has replied; once the store answers, the save and the check follow, and a shutdown
    waits for a save in flight before the audit store closes."""
    gate = asyncio.Event()

    class _SlowStore(helper.MemoryStore):
        async def save(self, conversation: str, expected: int, record: Any, ttl_s: int) -> bool:
            await gate.wait()
            return await super().save(conversation, expected, record, ttl_s)

    rig = _SingleRig(monkeypatch, store=_SlowStore())
    monkeypatch.setattr(helper, "_sweep_task", None)
    try:
        _install_backend(monkeypatch, _FakeClient(_ANSWER), _SINGLE)
        reply = await asyncio.wait_for(helper.llm_chat({}, {"prompt": "hello"}), timeout=1)
        assert reply["text"] == "REPLY-MARKER-1"
        assert len(helper._single_saves) == 1 and rig.events == [] and not rig.audit.conversations
        shutdown = asyncio.get_running_loop().create_task(helper.audit_shutdown())
        await asyncio.sleep(0.02)
        assert not shutdown.done(), "the shutdown waits for the save in flight"
        gate.set()
        await shutdown
        assert not helper._single_saves and len(rig.events) == 1
    finally:
        await rig.close()


async def test_a_working_store_that_fails_is_logged_by_type_and_never_refuses_the_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A store that is down, or not configured, costs that call's audit and nothing else: the
    call answers, no event is sent, and the log names the failure without a word of content."""
    rig = _SingleRig(monkeypatch, store=_BrokenStore(save=ConnectionError("PROMPT-MARKER-1 down")))
    caplog.set_level(logging.INFO)
    try:
        _install_backend(monkeypatch, _FakeClient(_ANSWER), _SINGLE)
        reply = await helper.llm_chat({}, {"prompt": "PROMPT-MARKER-1"})
        await rig.settled(lambda: True)
        assert reply["text"] == "REPLY-MARKER-1" and rig.events == []
        assert "transcript.audit single call not saved - ConnectionError" in caplog.text
        monkeypatch.setattr(helper, "_store", None)  # and with no working store at all
        _install_backend(monkeypatch, _FakeClient(_ANSWER), {**_SINGLE, "llm.transcript.store": ""})
        assert (await helper.llm_chat({}, {"prompt": "PROMPT-MARKER-1"}))["text"] == "REPLY-MARKER-1"
        await rig.settled(lambda: True)
        assert "single call not saved - AppException: transcript store not configured" in caplog.text
        assert rig.events == [] and not rig.audit.conversations
        assert "REPLY-MARKER-1" not in caplog.text
        assert caplog.text.count("PROMPT-MARKER-1") == 1, "only the store's own connection error"
    finally:
        await rig.close()


async def test_a_failed_write_keeps_the_record_and_the_sweep_repairs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """What a single call gains from the record as its carrier: a write the database refused is
    not lost. The record waits with the grace, the sweep sends the check again, and only then is
    the record released - and the log of the failure quotes no row."""
    clock = _Clock()
    rig = _SingleRig(monkeypatch, audit=_FlakyAudit(1), store=helper.MemoryStore(clock))
    caplog.set_level(logging.INFO)
    try:
        _install_backend(monkeypatch, _FakeClient(_ANSWER), _SINGLE)
        await helper.llm_chat(_HEADERS, {"prompt": "PROMPT-MARKER-1", "system": "SYSTEM-MARKER-1"})
        await rig.settled(lambda: "check failed" in caplog.text)
        ((conversation, entry),) = rig.store._records.items()
        assert entry[0] - clock.now == 86400, "the record waits with the audit's grace (1d)"
        assert not rig.audit.conversations and await rig.store.unaudited() == [conversation]
        assert f"conversation={conversation} check failed - _RowError sqlstate=23514" in caplog.text
        assert await helper.audit_sweep() == 1
        await rig.settled(lambda: not rig.store._records)
        assert rig.audit.conversations[conversation]["status"] == "done"
        assert rig.audit.turn_rows[conversation][1]["request"] == [
            {"role": "user", "content": "PROMPT-MARKER-1"}
        ]
        assert rig.events == [{"conversation": conversation, "check": True}] * 2
        assert await helper.audit_sweep() == 0, "nothing lags once the record is released"
        for marker in ("PROMPT-MARKER-1", "SYSTEM-MARKER-1", "REPLY-MARKER-1", "SECRET-MARKER-ROW"):
            assert marker not in caplog.text, marker
    finally:
        await rig.close()


async def test_a_loop_turn_is_not_a_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch changes nothing for a call with tools: a loop turn sends its own two kinds of
    event and its conversation row keeps its catalog, the column that tells the kinds apart."""
    rig = _SingleRig(monkeypatch)
    try:
        conversation = await _first_turn(monkeypatch, _SINGLE)
        await rig.settled(lambda: conversation in rig.audit.conversations)
        assert rig.events == [{"conversation": conversation, "turn": 1}]
        assert not helper._single_saves
        assert rig.audit.conversations[conversation]["tools"] == [_TOOL]
        assert rig.audit.conversations[conversation]["status"] == "open"
    finally:
        await rig.close()


async def test_single_calls_without_a_working_store_stop_the_application_by_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(helper, "_audit", helper.MemoryAuditStore())
    monkeypatch.setattr(helper, "_sweep_task", None)
    _configure(monkeypatch, **{"llm.transcript.audit.single": "true", "llm.transcript.store": ""})
    caplog.set_level(logging.ERROR)
    with pytest.raises(SystemExit):
        await helper.audit_boot()
    assert "llm.transcript.audit.single needs the working store" in caplog.text
    assert helper._sweep_task is None
