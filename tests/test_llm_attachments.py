"""Attachments on a single llm.chat call (RFC-0016) in the LLM helper - token-free.

tests/vectors/llm-attachments-vectors.json and the files of tests/fixtures/attachments (both
byte-identical in mercury-nodejs) state the contract: the shape of the field, where it does not
belong, the one root a request reads inside, the files refused before any model call, the blocks
the model gets and the reference the audit keeps. Every case builds its own root and a folder
beside it. The tests after the vector run pin what a vector cannot: an image's limit by backend,
the helper without the PDF library, the deadline over the read, the worker thread, the log and the
trace annotations, and the start with a root that is not a folder.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pypdf
import pytest
from test_llm_helper import (
    _assert_error,
    _event,
    _FakeClient,
    _install_backend,
    _install_writer,
    helper,
)
from test_transcript_audit import _SingleRig

from mercury_composable import AppException

_TESTS = Path(__file__).resolve().parent
_FIXTURES = _TESTS / "fixtures" / "attachments"
_VECTORS = _TESTS / "vectors" / "llm-attachments-vectors.json"
# the Node.js twin pins the same digests: change a file in both packs or in neither
_VECTORS_SHA256 = "06b0e85f7fa67c51eb9e63e0833d25a5e96f660c91210e460d3a346f2c574153"
_FIXTURES_SHA256 = {
    "damaged.pdf": "36a27d716a382936c825c5c3cb7bd7a2d38c0af35242d89b186b31faf2eb4ee1",
    "encrypted.pdf": "9f9e621bbe4dad963b3f5846b449dcc7424344335722c670e5d425efc7580dc7",
    "notes.txt": "98118d59bb278aef85d7f37cf587551fef57622d220e869e589d2850ffde82ce",
    "pixel.gif": "963d31f918ceb12c5322161ea10ecaa1fc3d0c31390cd4f0cd49e1c43729b31e",
    "pixel.jpg": "95ec656f94ada529679e4c7da103599777c24e090c592c6f052b503b231ff9e5",
    "pixel.png": "b1ff9c8ea3a780bad09b346c423d2d0e46815926879b18e841d928376a946640",
    "pixel.webp": "86be52bdb7547413cafb3ed175a806a798c65de98b40849e0b974c47d187de65",
    "three-pages.pdf": "c609d99e859256615fe1fe853eb4fe6c449de56a859bbb6b6b66e0ac8c086416",
}
VECTORS: dict[str, Any] = json.loads(_VECTORS.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = VECTORS["cases"]
_ANSWER = {
    "kind": "message", "model": "claude-opus-5-5", "stop_reason": "end_turn",
    "deltas": ["A page."], "usage": {"input_tokens": 900, "output_tokens": 4},
    "request_id": "req_attach_test",
}  # fmt: skip


def _fill(folder: Path, files: dict[str, Any]) -> None:
    for relative, source in files.items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, dict):
            target.symlink_to(source["link"])
        else:
            shutil.copyfile(_FIXTURES / source, target)


def _root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, Any], **outside: Any
) -> Path:
    """A new root holding the named fixtures, a folder beside it, and no root resolved yet."""
    root, beside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    beside.mkdir()
    _fill(root, files)
    _fill(beside, outside)
    monkeypatch.setattr(helper, "_attachments_root", None)
    return root


def _encoded(fixture: str) -> str:
    return base64.b64encode((_FIXTURES / fixture).read_bytes()).decode("ascii")


def _assert_block(block: dict[str, Any], wanted: dict[str, Any]) -> None:
    """A sent block against its short form in the vector file."""
    if "text" in wanted:
        assert block == {"type": "text", "text": wanted["text"]}
        return
    if "image" in wanted:
        source = {"type": "base64", "media_type": wanted["media_type"],
                  "data": _encoded(wanted["image"])}  # fmt: skip
        assert block == {"type": "image", "source": source}
        return
    assert set(block) == {"type", "source"} and block["type"] == "document"
    source = block["source"]
    assert set(source) == {"type", "media_type", "data"}
    assert (source["type"], source["media_type"]) == ("base64", "application/pdf")
    if "pages" not in wanted:
        assert source["data"] == _encoded(wanted["document"]), "a whole document goes as it is"
        return
    # a cut is never compared byte for byte: page n of the fixture is 100+n points wide
    cut = pypdf.PdfReader(io.BytesIO(base64.b64decode(source["data"])))
    assert [round(float(page.mediabox.width)) - 100 for page in cut.pages] == wanted["pages"]


def test_the_vector_file_and_the_fixtures_are_the_ones_the_node_twin_runs() -> None:
    digest = hashlib.sha256(_VECTORS.read_bytes()).hexdigest()
    assert digest == _VECTORS_SHA256, "the shared vector file changed - update both packs"
    assert (VECTORS["contract"], VECTORS["version"]) == ("llm-attachments", 1)
    held = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in _FIXTURES.iterdir()}
    assert held == _FIXTURES_SHA256, "a shared fixture changed - update both packs"
    assert set(VECTORS["fixtures"]) == set(_FIXTURES_SHA256)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_attachments_contract(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path, case.get("files", {}), **case.get("outside", {}))
    config = dict(case.get("config", {}))
    if case.get("root", True):
        config[helper.ATTACHMENTS_ROOT] = str(root)
    expect = case["expect"]
    rig = _SingleRig(monkeypatch) if "audit_request" in expect else None
    client = _FakeClient(case.get("provider"))
    _install_backend(monkeypatch, client, config)
    result: Any = None
    error: AppException | None = None
    try:
        if case["route"] == "llm.stream":
            out = _install_writer(monkeypatch)
            await helper.llm_stream({}, _event(case["body"]))
            error = out.error
        else:
            try:
                result = await helper.llm_chat({}, case["body"])
            except AppException as exc:
                error = exc
        if "error" in expect:
            _assert_error(error, expect["error"])
            assert error is not None and str(tmp_path) not in error.message, "no host path"
            assert client.ledger.calls == [], "a refused attachment reaches no model call"
            return
        assert error is None, error
        for key, value in expect["result"].items():
            assert result[key] == value, key
        ((_namespace, params),) = client.ledger.calls
        sent, wanted = params["messages"], expect["messages"]
        assert sent[:-1] == wanted[:-1] and len(sent) == len(wanted)
        assert sent[-1]["role"] == "user" and len(sent[-1]["content"]) == len(wanted[-1]["content"])
        for block, short in zip(sent[-1]["content"], wanted[-1]["content"], strict=True):
            _assert_block(block, short)
        if rig is not None:
            await rig.settled(lambda: bool(rig.audit.conversations) and not rig.store._records)
            ((conversation, _row),) = rig.audit.conversations.items()
            turn = rig.audit.turn_rows[conversation][1]
            assert turn["request"] == expect["audit_request"]
            assert "base64" not in json.dumps(turn), "the audit holds a reference, never the bytes"
    finally:
        if rig is not None:
            await rig.close()


# --- what a vector cannot express --------------------------------------------------------------


async def test_an_image_over_the_backends_limit_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The provider counts an image base64-encoded, and the limit depends on the route to the
    models: 10 MB on the Claude API, 5 MB on Amazon Bedrock. The helper refuses by name first."""
    assert helper.backend_for("anthropic", None).image_max_encoded == 10 * 1024 * 1024
    assert helper.backend_for("bedrock", None).image_max_encoded == 5 * 1024 * 1024
    root = _root(monkeypatch, tmp_path, {"page.png": "pixel.png"})
    client = _FakeClient(_ANSWER)
    _install_backend(monkeypatch, client, {helper.ATTACHMENTS_ROOT: str(root)})
    body = {"prompt": "x", "attachments": ["page.png"]}
    # the 69 bytes of the fixture are 92 base64-encoded: exactly at the limit passes
    monkeypatch.setattr(helper, "_backend", dataclasses.replace(helper._backend, image_max_encoded=92))
    assert (await helper.llm_chat({}, body))["text"] == "A page."
    monkeypatch.setattr(helper, "_backend", dataclasses.replace(helper._backend, image_max_encoded=91))
    with pytest.raises(AppException) as refused:
        await helper.llm_chat({}, body)
    assert refused.value.status == 400
    assert refused.value.message == (
        "attachments[0]: an image of 92 bytes base64-encoded - the backend's limit is 91"
    )
    assert len(client.ledger.calls) == 1, "only the call that passed reached the provider"


async def test_without_the_pdf_library_pages_are_a_503_and_a_whole_document_goes_as_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The library is the documents extra: cutting pages needs it, and the 503 names the extra.
    A whole document needs none - it goes as it is, unchecked, and the provider judges it."""
    root = _root(monkeypatch, tmp_path, {"form.pdf": "three-pages.pdf", "locked.pdf": "encrypted.pdf"})
    client = _FakeClient(_ANSWER)
    _install_backend(monkeypatch, client, {helper.ATTACHMENTS_ROOT: str(root)})
    find_spec = helper.importlib.util.find_spec
    monkeypatch.setattr(
        helper.importlib.util, "find_spec",
        lambda name, *more: None if name == "pypdf" else find_spec(name, *more),
    )  # fmt: skip
    with pytest.raises(AppException) as missing:
        await helper.llm_chat({}, {"prompt": "x", "attachments": [{"path": "form.pdf", "pages": 1}]})
    assert missing.value.status == 503
    assert missing.value.message == (
        "attachments: pages needs pypdf - pip install 'mercury-composable[documents]'"
    )
    assert client.ledger.calls == []
    await helper.llm_chat({}, {"prompt": "x", "attachments": ["form.pdf", "locked.pdf"]})
    ((_namespace, params),) = client.ledger.calls
    blocks = params["messages"][-1]["content"]
    assert [block["source"]["data"] for block in blocks[:2]] == [
        _encoded("three-pages.pdf"), _encoded("encrypted.pdf"),
    ]  # fmt: skip


async def test_the_deadline_covers_the_reading_and_the_files_are_read_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """params.timeout_ms bounds the whole call, the files included: a read that outlasts it is
    the 408 of the contract and no model call follows. The read runs in a worker thread, so a slow
    disk never holds the event loop, which carries every other call and stream of the process."""
    root = _root(monkeypatch, tmp_path, {"page.png": "pixel.png"})
    client = _FakeClient(_ANSWER)
    _install_backend(monkeypatch, client, {helper.ATTACHMENTS_ROOT: str(root)})
    load = helper._load_attachments
    threads: list[bool] = []

    def slow(*args: Any) -> Any:
        threads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.3)
        return load(*args)

    monkeypatch.setattr(helper, "_load_attachments", slow)
    body = {"prompt": "x", "attachments": ["page.png"], "params": {"timeout_ms": 50}}
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticking = asyncio.get_running_loop().create_task(tick())
    with pytest.raises(AppException) as late:
        await helper.llm_chat({}, body)
    ticking.cancel()
    assert late.value.status == 408 and "timed out after 50 ms" in late.value.message
    assert client.ledger.calls == [], "no model call after the deadline"
    assert threads == [False], "the files are read in a worker thread"
    assert ticks >= 2, "the event loop kept running while the files were read"
    await asyncio.sleep(0.35)  # let the worker thread end before the folder is removed


async def test_a_log_carries_the_count_and_the_bytes_and_never_a_path_or_a_files_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _root(monkeypatch, tmp_path, {"SCAN-MARKER/page.png": "pixel.png", "form.pdf": "three-pages.pdf"})
    seen: dict[str, Any] = {}
    monkeypatch.setattr(helper, "annotate_trace", lambda key, value: seen.__setitem__(key, value))
    _install_backend(monkeypatch, _FakeClient(_ANSWER), {helper.ATTACHMENTS_ROOT: str(root)})
    caplog.set_level(logging.INFO)
    body = {"prompt": "x", "attachments": ["SCAN-MARKER/page.png", {"path": "form.pdf", "pages": "2"}]}
    await helper.llm_chat({}, body)
    assert "llm.chat attachments=2 bytes=803" in caplog.text
    assert (seen["llm_attachments"], seen["llm_attachment_bytes"]) == ("2", "803")
    for hidden in ("SCAN-MARKER", "form.pdf", str(tmp_path), _encoded("pixel.png")[:24]):
        assert hidden not in caplog.text, hidden
    with pytest.raises(AppException):  # and a refusal names the entry, never the path
        await helper.llm_chat({}, {"prompt": "x", "attachments": ["SCAN-MARKER/gone.png"]})
    assert "SCAN-MARKER" not in caplog.text and "llm.chat failed - status=400" in caplog.text


async def test_a_call_without_attachments_reads_no_root_and_sends_text_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field is optional: without it nothing of this is touched - no root is needed, the turn
    stays a string, and the trace carries no attachment annotation."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(helper, "annotate_trace", lambda key, value: seen.__setitem__(key, value))
    monkeypatch.setattr(helper, "_attachments_root", None)
    client = _FakeClient(_ANSWER)
    _install_backend(monkeypatch, client)
    await helper.llm_chat({}, {"prompt": "x", "attachments": None})
    ((_namespace, params),) = client.ledger.calls
    assert params["messages"] == [{"role": "user", "content": "x"}]
    assert "llm_attachments" not in seen and helper._attachments_root is None


async def test_the_root_is_resolved_once_and_a_root_that_is_not_a_folder_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A root given through a link is resolved, once, so a request is measured against the real
    folder. A configured root that is not a folder is a deployment error: the application stops at
    start, by name; with no root configured the start does nothing."""
    root = _root(monkeypatch, tmp_path, {"page.png": "pixel.png"})
    (tmp_path / "through-a-link").symlink_to(root)
    config = {helper.ATTACHMENTS_ROOT: str(tmp_path / "through-a-link")}
    _install_backend(monkeypatch, _FakeClient(_ANSWER), config)
    caplog.set_level(logging.INFO)
    await helper.attachments_boot()
    assert helper._attachments_root == root.resolve()
    assert "attachments ready - llm.attachments.root is set" in caplog.text
    assert (await helper.llm_chat({}, {"prompt": "x", "attachments": ["page.png"]}))["text"] == "A page."
    # the root stays the folder it resolved to, whatever the link is pointed at afterwards
    (tmp_path / "through-a-link").unlink()
    (tmp_path / "through-a-link").symlink_to(tmp_path / "outside")
    assert (await helper.llm_chat({}, {"prompt": "x", "attachments": ["page.png"]}))["text"] == "A page."
    monkeypatch.setattr(helper, "_attachments_root", None)
    _install_backend(
        monkeypatch, _FakeClient(_ANSWER), {helper.ATTACHMENTS_ROOT: str(root / "page.png")}
    )
    with pytest.raises(SystemExit):
        await helper.attachments_boot()
    assert "attachments cannot start - llm.attachments.root is not a folder" in caplog.text
    with pytest.raises(AppException) as broken:  # and a request that meets it is a 500
        await helper.llm_chat({}, {"prompt": "x", "attachments": ["page.png"]})
    assert broken.value.status == 500
    _install_backend(monkeypatch, _FakeClient(_ANSWER))  # with no root configured: nothing
    await helper.attachments_boot()
    assert helper._attachments_root is None
