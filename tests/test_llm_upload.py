"""The LLM helper's document intake (llm.upload): the files of a multipart upload saved under
uploads/ inside the attachments root and the references a later llm.chat call names."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_llm_helper import helper

from mercury_composable import AppException, AsyncHttpRequest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "attachments"


def _request(*files: tuple[str, str, bytes, str]) -> dict[str, object]:
    request = AsyncHttpRequest().set_method("POST").set_url("/api/llm/upload")
    for name, content_type, content, tag in files:
        request.add_file(name, content_type, content, tag)
    return request.to_map()


async def test_the_files_are_saved_under_new_names_and_their_references_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(helper, "_attachments_root", root.resolve())
    pdf = (_FIXTURES / "three-pages.pdf").read_bytes()
    docx = b"PK\x03\x04 a word-processor file"
    result = await helper.llm_upload({}, _request(
        ("Quarterly Report (final).pdf", "application/pdf", pdf, "doc"),
        ("../.hidden/notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         docx, "doc"),
    ))
    assert isinstance(result, dict)
    saved = result["files"]
    assert len(saved) == 2
    first, second = saved
    # a new name under uploads/, the original name kept beside it, hidden dots and odd characters gone
    assert first["path"].startswith("uploads/") and first["path"].endswith("-Quarterly_Report__final_.pdf")
    assert second["path"].endswith("-hidden_notes.docx") or second["path"].endswith("-notes.docx")
    assert first["filename"] == "Quarterly Report (final).pdf"
    # the kind by the first bytes, the declared type when the bytes say nothing the model takes
    assert first["media_type"] == "application/pdf" and first["attachable"] is True
    assert second["media_type"].startswith("application/vnd.openxmlformats") and second["attachable"] is False
    assert first["bytes"] == len(pdf) and first["sha256"] == hashlib.sha256(pdf).hexdigest()
    # the saved file is the one uploaded, and it is reachable the way `attachments` reads a file
    stored = helper._attachment_file(root.resolve(), 0, first["path"])
    assert stored.read_bytes() == pdf
    assert helper._attachment_file(root.resolve(), 1, second["path"]).read_bytes() == docx


async def test_without_a_file_the_request_is_a_400_and_without_a_root_a_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(helper, "_attachments_root", root.resolve())
    with pytest.raises(AppException) as raised:
        await helper.llm_upload({}, _request())
    assert raised.value.status == 400 and raised.value.message == helper.UPLOAD_NO_FILES
    monkeypatch.setattr(helper, "_attachments_root", None)
    monkeypatch.setattr(helper, "_prop", lambda key, default=None: None)
    with pytest.raises(AppException) as raised:
        await helper.llm_upload({}, _request(("a.pdf", "application/pdf", b"%PDF-1.4", "doc")))
    assert raised.value.status == 503 and raised.value.message == helper.MISSING_ATTACHMENTS_ROOT


def test_safe_names() -> None:
    assert helper._safe_name("Quarterly Report (final).pdf") == "Quarterly_Report__final_.pdf"
    assert helper._safe_name("../.hidden/evil name.pdf") == "evil_name.pdf"
    assert helper._safe_name(".bashrc") == "bashrc"
    assert helper._safe_name("") == ""
    assert len(helper._safe_name("x" * 200 + ".pdf")) == 80
