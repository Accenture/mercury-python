"""The built-in agent tools (extensions/agent-tools/agent_tools.py).

The file tools against a workspace built in a temporary directory (a symbolic link that escapes
it, hidden entries, a binary, a file that is not UTF-8, a large file); the in-house HTML reader on
a fixture page; the web sandbox's allowlist and address rules without a network; the web tools
against a fixture site served in-process by aiohttp (pages that link each other, robots.txt,
redirects to allowed, private and foreign hosts, a slow page, a large body, charsets); the clock;
the boot that registers only the tools whose sandbox is configured and fails by name on a bad
one; the health route; the describe output; and the catalog files, which must equal the rows the
module publishes, byte for byte, since the Node.js twin carries the same files.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

from mercury_composable import AppException, FunctionRegistry, Platform, PostOffice, app_config

_TESTS = Path(__file__).resolve().parent
_MODULE = _TESTS.parent / "extensions" / "agent-tools" / "agent_tools.py"

_spec = importlib.util.spec_from_file_location("agent_tools_under_test", _MODULE)
assert _spec is not None
assert _spec.loader is not None
# typed Any: the module is loaded dynamically from a file path, so its attributes are
# unknowable statically - Any tells every analyzer to trust the runtime
tools: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("agent_tools_under_test", tools)
_spec.loader.exec_module(tools)

ROUTES = (
    "file.read",
    "file.write",
    "file.list",
    "file.search",
    "file.extract",
    "web.fetch",
    "web.crawl",
    "graph.schema",
    "graph.query",
    "graph.write",
    "graph.ingest",
    "time.now",
)
STEP_ONE_ROUTES = tuple(r for r in ROUTES if not r.startswith("graph."))
WRITERS = {"file_write", "graph_write", "graph_ingest"}

# the loop's closed schema subset (ToolSchema in the engines' agent loop), as the rows may use it
SCHEMA_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "title",
    "description",
    "default",
    "examples",
    "format",
}


# --- configuration -------------------------------------------------------------------------------


@pytest.fixture
def configure() -> Iterator[Callable[[dict[str, Any]], None]]:
    """Set configuration keys for one test and clear them after it (a cleared override reads as
    unset to the module's accessors)."""
    touched: list[str] = []

    def apply(settings: dict[str, Any]) -> None:
        for key, value in settings.items():
            app_config().set(key, value)
            touched.append(key)

    yield apply
    for key in touched:
        app_config().set(key, None)


def _status(call: Callable[[], Any]) -> tuple[int, str]:
    try:
        call()
    except AppException as exc:
        return exc.status, exc.message
    raise AssertionError("expected a refusal")


async def _astatus(coroutine: Any) -> tuple[int, str]:
    try:
        await coroutine
    except AppException as exc:
        return exc.status, exc.message
    raise AssertionError("expected a refusal")


# --- the workspace -------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    for folder in ("notes", "src", "data", "empty", ".secrets"):
        (root / folder).mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside the workspace\n", encoding="utf-8")
    (root / "notes" / "plan.md").write_text("# Plan\n\nWrite the tools.\n", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "def main():\n    print('hello')\n\n\ndef helper():\n    pass\n", encoding="utf-8"
    )
    (root / "src" / "util.py").write_text(
        "# TODO: tidy\nvalue = 1\n# todo: later\nTODO_COUNT = 2\n", encoding="utf-8"
    )
    (root / "data" / "blob.bin").write_bytes(b"\x00\x01\x02 not text")
    (root / "data" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (root / "data" / "latin.txt").write_bytes(b"caf\xe9\n")
    (root / "big.txt").write_text("x" * (300 * 1024), encoding="utf-8")
    (root / ".secrets" / "token.txt").write_text("hidden\n", encoding="utf-8")
    (root / ".env").write_text("KEY=value\n", encoding="utf-8")
    os.symlink(outside, root / "escape")
    os.symlink(outside / "secret.txt", root / "escape-file")
    return root


@pytest.fixture
def files(workspace: Path, configure: Callable[[dict[str, Any]], None]) -> Any:
    configure({tools.FILE_ROOT: str(workspace)})
    return tools.FileTools(tools.load_file_sandbox())


def test_read_a_text_file(files: Any) -> None:
    reply = files.read({}, {"path": "notes/plan.md"})
    assert reply == {
        "path": "notes/plan.md",
        "bytes": 25,
        "text": "# Plan\n\nWrite the tools.\n",
        "truncated": False,
    }
    # a '..' that stays inside resolves to the file; the reply names the workspace-relative path
    assert files.read({}, {"path": "src/../notes/plan.md"})["path"] == "notes/plan.md"


def test_read_cuts_at_max_bytes_and_the_workspace_limit(
    workspace: Path, configure: Callable[[dict[str, Any]], None]
) -> None:
    configure({tools.FILE_ROOT: str(workspace), tools.FILE_MAX_BYTES: "4096"})
    files = tools.FileTools(tools.load_file_sandbox())
    reply = files.read({}, {"path": "big.txt", "max_bytes": 1000})
    assert (len(reply["text"]), reply["bytes"], reply["truncated"]) == (1000, 300 * 1024, True)
    # the argument is capped by the workspace's limit, never raised past it
    assert len(files.read({}, {"path": "big.txt", "max_bytes": 1_000_000})["text"]) == 4096
    assert len(files.read({}, {"path": "big.txt"})["text"]) == 4096
    assert files.read({}, {"path": "notes/plan.md"})["truncated"] is False


@pytest.mark.parametrize(
    ("path", "status", "message"),
    [
        ("../outside/secret.txt", 400, "outside the workspace"),
        ("/etc/hosts", 400, "not absolute"),
        ("C:\\windows\\win.ini", 400, "not absolute"),
        ("escape/secret.txt", 400, "outside the workspace"),
        ("escape-file", 400, "outside the workspace"),
        (".secrets/token.txt", 400, "hidden"),
        (".env", 400, "hidden"),
        ("nope.txt", 404, "file not found: nope.txt"),
        ("notes", 400, "is a directory"),
        ("data/image.png", 400, "a PNG image"),
        ("data/blob.bin", 400, "(binary)"),
        ("data/latin.txt", 400, "not UTF-8 text"),
    ],
)
def test_read_refuses_by_rule(files: Any, path: str, status: int, message: str) -> None:
    got = _status(lambda: files.read({}, {"path": path}))
    assert got[0] == status, got
    assert message in got[1], got


def test_hidden_entries_are_available_when_the_workspace_exposes_them(
    workspace: Path, configure: Callable[[dict[str, Any]], None]
) -> None:
    configure({tools.FILE_ROOT: str(workspace), tools.FILE_HIDDEN: True})
    files = tools.FileTools(tools.load_file_sandbox())
    assert files.read({}, {"path": ".env"})["text"] == "KEY=value\n"
    listed = [entry["path"] for entry in files.list({}, {})["entries"]]
    assert ".env" in listed and ".secrets" in listed


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("notes/plan.md", "must be a map"),
        ({}, "path is required"),
        ({"path": 3}, "path must be a string"),
        ({"path": "notes/plan.md", "max_bytes": "10"}, "max_bytes must be an integer"),
        ({"path": "notes/plan.md", "max_bytes": True}, "max_bytes must be an integer"),
        ({"path": "notes/plan.md", "max_bytes": 0}, "max_bytes must be at least 1"),
        ({"path": "notes/plan.md", "encoding": "latin-1"}, "file_read does not take encoding"),
    ],
)
def test_arguments_are_checked_against_the_row(files: Any, body: Any, message: str) -> None:
    status, text = _status(lambda: files.read({}, body))
    assert status == 400
    assert message in text, text


def test_write_create_overwrite_and_append(files: Any, workspace: Path) -> None:
    created = files.write({}, {"path": "out/reports/summary.md", "content": "# Summary\n"})
    assert created == {"path": "out/reports/summary.md", "bytes": 10, "mode": "create"}
    assert (workspace / "out" / "reports" / "summary.md").read_text(
        encoding="utf-8"
    ) == "# Summary\n"
    status, message = _status(
        lambda: files.write({}, {"path": "out/reports/summary.md", "content": "again"})
    )
    assert status == 409 and "exists" in message
    assert (
        files.write({}, {"path": "out/reports/summary.md", "content": "new", "mode": "overwrite"})[
            "mode"
        ]
        == "overwrite"
    )
    assert (
        files.write({}, {"path": "out/reports/summary.md", "content": " more", "mode": "append"})[
            "bytes"
        ]
        == 5
    )
    assert (workspace / "out" / "reports" / "summary.md").read_text(encoding="utf-8") == "new more"
    # Unicode is written as UTF-8 and the byte count is the encoded size
    assert files.write({}, {"path": "out/café.txt", "content": "café"})["bytes"] == 5


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({"path": "../x.txt", "content": "a"}, 400, "outside the workspace"),
        ({"path": "/tmp/x.txt", "content": "a"}, 400, "not absolute"),
        ({"path": ".hidden/x.txt", "content": "a"}, 400, "hidden"),
        ({"path": "escape/x.txt", "content": "a"}, 400, "outside the workspace"),
        ({"path": "notes", "content": "a"}, 400, "is a directory"),
        ({"path": ".", "content": "a"}, 400, "is a directory"),
        ({"path": "x.txt", "content": 1}, 400, "content must be a string"),
        ({"path": "x.txt"}, 400, "content is required"),
        (
            {"path": "x.txt", "content": "a", "mode": "replace"},
            400,
            "mode must be one of create, overwrite, append",
        ),
    ],
)
def test_write_refuses_by_rule(files: Any, body: Any, status: int, message: str) -> None:
    got = _status(lambda: files.write({}, body))
    assert got[0] == status, got
    assert message in got[1], got


def test_write_respects_the_extension_list_and_the_size_limit(
    workspace: Path, configure: Callable[[dict[str, Any]], None]
) -> None:
    configure(
        {
            tools.FILE_ROOT: str(workspace),
            tools.FILE_WRITE_EXTENSIONS: ".md, txt",
            tools.FILE_MAX_BYTES: 1024,
        }
    )
    files = tools.FileTools(tools.load_file_sandbox())
    assert files.write({}, {"path": "out/a.txt", "content": "ok"})["bytes"] == 2
    assert files.write({}, {"path": "out/A.MD", "content": "ok"})["bytes"] == 2
    status, message = _status(lambda: files.write({}, {"path": "out/a.py", "content": "x"}))
    assert status == 400 and "does not end in .md, .txt" in message
    status, message = _status(lambda: files.write({}, {"path": "out/b.txt", "content": "x" * 1025}))
    assert status == 400 and "over the workspace's limit of 1024" in message


def test_list_the_root_and_a_folder(files: Any) -> None:
    reply = files.list({}, {})
    assert reply["truncated"] is False
    assert [(e["path"], e["type"]) for e in reply["entries"]] == [
        ("big.txt", "file"),
        ("data", "directory"),
        ("empty", "directory"),
        ("notes", "directory"),
        ("src", "directory"),
    ], "hidden entries and links that escape the workspace are not listed"
    big = reply["entries"][0]
    assert big["bytes"] == 300 * 1024
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", big["modified"])
    folder = files.list({}, {"path": "src"})
    assert [e["path"] for e in folder["entries"]] == ["src/app.py", "src/util.py"]
    assert files.list({}, {"path": "empty"}) == {"entries": [], "truncated": False}


def test_list_with_a_glob_and_a_cap(files: Any) -> None:
    assert [e["path"] for e in files.list({}, {"glob": "**/*.py"})["entries"]] == [
        "src/app.py",
        "src/util.py",
    ]
    assert [e["path"] for e in files.list({}, {"path": "src", "glob": "*.py"})["entries"]] == [
        "src/app.py",
        "src/util.py",
    ]
    cut = files.list({}, {"glob": "**/*", "max_entries": 2})
    assert len(cut["entries"]) == 2 and cut["truncated"] is True
    assert _status(lambda: files.list({}, {"glob": "/abs/*"}))[0] == 400
    assert _status(lambda: files.list({}, {"path": "notes/plan.md"})) == (
        400,
        "notes/plan.md is not a directory",
    )
    assert _status(lambda: files.list({}, {"path": "nowhere"})) == (
        404,
        "directory not found: nowhere",
    )
    assert files.list({}, {"max_entries": 5000})["truncated"] is False, (
        "over the cap is capped, not refused"
    )


def test_search_literal_regex_case_and_context(files: Any) -> None:
    reply = files.search({}, {"pattern": "todo"})
    assert reply["files_searched"] == 5, "text files only - the binaries are skipped"
    assert reply["truncated"] is False
    assert [(m["path"], m["line"], m["text"]) for m in reply["matches"]] == [
        ("src/util.py", 1, "# TODO: tidy"),
        ("src/util.py", 3, "# todo: later"),
        ("src/util.py", 4, "TODO_COUNT = 2"),
    ]
    assert [
        m["line"] for m in files.search({}, {"pattern": "TODO", "case_sensitive": True})["matches"]
    ] == [1, 4]
    assert [
        m["line"] for m in files.search({}, {"pattern": r"def \w+\(", "regex": True})["matches"]
    ] == [1, 5]
    assert files.search({}, {"pattern": r"def \w+\("})["matches"] == [], (
        "without regex the pattern is literal"
    )
    scoped = files.search({}, {"pattern": "e", "path": "notes"})
    assert {m["path"] for m in scoped["matches"]} == {"notes/plan.md"}
    assert {
        m["path"] for m in files.search({}, {"pattern": "e", "glob": "**/*.md"})["matches"]
    } == {"notes/plan.md"}
    context = files.search({}, {"pattern": "value", "context": 1})["matches"][0]
    assert (context["before"], context["after"]) == (["# TODO: tidy"], ["# todo: later"])
    first = files.search({}, {"pattern": "value", "context": 3})["matches"][0]
    assert first["before"] == ["# TODO: tidy"], "context stops at the top of the file"


def test_search_caps_and_refusals(
    workspace: Path, configure: Callable[[dict[str, Any]], None]
) -> None:
    configure({tools.FILE_ROOT: str(workspace), tools.FILE_LINE_MAX_CHARS: 20})
    files = tools.FileTools(tools.load_file_sandbox())
    cut = files.search({}, {"pattern": "todo", "max_results": 2})
    assert len(cut["matches"]) == 2 and cut["truncated"] is True
    long_line = files.search({}, {"pattern": "xxxx"})["matches"][0]
    assert len(long_line["text"]) == 20, "a line is cut to tools.file.line.max.chars"
    status, message = _status(lambda: files.search({}, {"pattern": "(", "regex": True}))
    assert status == 400 and "not a valid regular expression" in message
    assert _status(lambda: files.search({}, {"pattern": ""})) == (400, "pattern must not be empty")
    assert files.search({}, {"pattern": "a", "context": 9})["matches"], (
        "over the cap is capped, not refused"
    )
    assert _status(lambda: files.search({}, {"pattern": "a", "path": "../"}))[0] == 400


# --- HTML to text --------------------------------------------------------------------------------

PAGE = """<!DOCTYPE html><html><head><title> Page   Title </title><meta charset="utf-8">
<style>body { color: red }</style><script>var x = "<p>not text</p>";</script></head>
<body><header><a href="/">Site</a></header><nav><ul><li><a href="/menu">Menu</a></li></ul></nav>
<main><h1>Heading &amp; more</h1><p>First paragraph with a <a href="/docs/a#section">link to A</a>,
<code>inline code</code> and caf&eacute;.</p><ul><li>one</li><li>two <ul><li>nested</li></ul></li></ul>
<pre>line 1
  line 2</pre><p>Line<br>break</p><table><tr><th>H1</th><th>H2</th></tr><tr><td>c1</td><td>c2</td></tr></table>
<a href="mailto:x@example.com">mail</a> <a href="#top">top</a> <a href="https://other.example/x">other</a>
<a href="/docs/a">again</a><img src="pic.png" alt="pic"></main><footer>Footer text</footer></body></html>"""


def test_html_to_text_drops_chrome_and_keeps_the_readable_text() -> None:
    title, text, links = tools.html_to_text(PAGE, "https://site.example/docs/index.html")
    assert title == "Page Title"
    blocks = text.split("\n\n")
    assert blocks[0] == "Heading & more"
    assert blocks[1] == "First paragraph with a link to A, inline code and café."
    assert blocks[2:5] == ["- one", "- two", "  - nested"]
    assert blocks[5] == "line 1\n  line 2"
    assert blocks[6] == "Line break"
    assert "H1 H2" in blocks[7] and "c1 c2" in blocks[8]
    for dropped in ("Menu", "Site", "Footer text", "color: red", "not text"):
        assert dropped not in text
    assert links == ["https://site.example/docs/a", "https://other.example/x"], (
        "absolute, in order, without fragments or duplicates; mailto and #anchors left out"
    )


def test_html_to_markdown_keeps_headings_lists_links_and_code() -> None:
    _title, text, _links = tools.html_to_text(PAGE, "https://site.example/", markdown=True)
    blocks = text.split("\n\n")
    assert blocks[0] == "# Heading & more"
    assert (
        blocks[1]
        == "First paragraph with a [link to A](https://site.example/docs/a#section), `inline code` and café."
    )
    assert blocks[2:5] == ["- one", "- two", "  - nested"]
    assert blocks[5] == "```\nline 1\n  line 2\n```"
    assert "[other](https://other.example/x)" in text


def test_html_to_text_drops_chrome_marked_by_role_or_hidden() -> None:
    page = (
        '<body><div role="navigation"><h3>Navigation</h3><ul><li><a href="/i">index</a></li></ul></div>'
        '<div role="banner">Banner</div><span aria-hidden="true">hidden icon</span>'
        '<div class="body"><h1>Topic</h1><p>Real <img src="x.png" alt="x"> text.</p></div>'
        '<div role="contentinfo">Copyright</div><div role="main">Kept</div></body>'
    )
    _title, text, links = tools.html_to_text(page, "https://h/")
    assert text == "Topic\n\nReal text.\n\nKept"
    assert links == [], "a link inside dropped chrome is not collected"


def test_html_to_text_survives_broken_markup() -> None:
    title, text, links = tools.html_to_text(
        "<p>unclosed <b>bold <a href='x'>link</p><div>next</div></span>", "https://h/"
    )
    assert title == ""
    assert text == "unclosed bold link\n\nnext"
    assert links == ["https://h/x"]
    assert tools.html_to_text("", "")[1] == ""


# --- the web sandbox without a network -----------------------------------------------------------


def test_host_rules_are_parsed_with_scheme_and_port() -> None:
    rules = tools.parse_host_rules(
        (
            "docs.example.com",
            "*.example.org",
            "http://127.0.0.1",
            "host:8443",
            "http://[::1]:8080",
            "HTTPS://Upper.Example.Com.",
        )
    )
    assert [(r.pattern, r.scheme, r.port) for r in rules] == [
        ("docs.example.com", "https", None),
        ("*.example.org", "https", None),
        ("127.0.0.1", "http", None),
        ("host", "https", 8443),
        ("::1", "http", 8080),
        ("upper.example.com", "https", None),
    ]
    assert [str(r) for r in rules[:4]] == [
        "docs.example.com",
        "*.example.org",
        "http://127.0.0.1",
        "https://host:8443",
    ]
    for bad, message in (
        ("https://x/path", "not a host pattern"),
        ("ftp://x", "scheme must be http or https"),
        ("host:abc", "port must be a number"),
        ("host:70000", "between 1 and 65535"),
        ("user@host", "not a host pattern"),
    ):
        with pytest.raises(tools.ToolsError) as failure:
            tools.parse_host_rules((bad,))
        assert message in str(failure.value)


@pytest.fixture
def sandbox() -> Any:
    rules = tools.parse_host_rules(
        ("docs.example.com", "*.example.org", "http://127.0.0.1", "host:8443")
    )
    return tools.WebSandbox(rules=rules)


@pytest.mark.parametrize(
    ("url", "expect"),
    [
        ("https://docs.example.com/a/b?x=1#frag", "https://docs.example.com/a/b?x=1"),
        ("https://DOCS.example.com", "https://DOCS.example.com/"),
        ("https://sub.example.org/", "https://sub.example.org/"),
        ("https://a.b.example.org/", "https://a.b.example.org/"),
        ("http://127.0.0.1:8080/x", "http://127.0.0.1:8080/x"),
        ("https://127.0.0.1/x", "https://127.0.0.1/x"),
        ("https://host:8443/", "https://host:8443/"),
    ],
)
def test_check_url_allows_listed_hosts(sandbox: Any, url: str, expect: str) -> None:
    assert sandbox.check_url(url).geturl() == expect


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "http://docs.example.com/",
            "allowed over https only - list it as http://docs.example.com",
        ),
        ("https://evil.example/", "evil.example is not one of the sites this agent may reach"),
        ("https://example.org/", "example.org is not one of the sites"),
        ("https://docs.example.com.evil.example/", "not one of the sites"),
        ("https://user:pw@docs.example.com/", "must not carry credentials"),
        ("ftp://docs.example.com/", "must start with https://"),
        ("docs.example.com/page", "must start with https://"),
        ("https://host:9443/", "host is not allowed on port 9443"),
        ("https:///path", "url has no host"),
    ],
)
def test_check_url_refuses_by_rule(sandbox: Any, url: str, message: str) -> None:
    status, text = _status(lambda: sandbox.check_url(url))
    assert status == 400
    assert message in text, text


@pytest.mark.parametrize(
    ("address", "kind"),
    [
        ("127.0.0.1", "loopback"),
        ("10.0.0.1", "private"),
        ("172.16.5.5", "private"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "link-local"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("::1", "loopback"),
        ("::ffff:10.0.0.1", "private"),
        ("fe80::1", "link-local"),
        ("fc00::1", "private"),
        ("93.184.216.34", None),
        ("2606:2800:220:1:248:1893:25c8:1946", None),
    ],
)
def test_address_kinds(address: str, kind: str | None) -> None:
    import ipaddress

    assert tools.address_kind(ipaddress.ip_address(address)) == kind


@pytest.mark.asyncio
async def test_pin_refuses_private_addresses_unless_allowed() -> None:
    closed = tools.WebSandbox(
        rules=tools.parse_host_rules(("http://127.0.0.1", "http://169.254.169.254", "localhost"))
    )
    status, message = await _astatus(closed.pin("127.0.0.1", 80))
    assert status == 400 and "loopback address" in message and tools.WEB_ALLOW_PRIVATE in message
    status, message = await _astatus(closed.pin("169.254.169.254", 80))
    assert status == 400 and "link-local address" in message, (
        "the cloud metadata service is refused by range"
    )
    status, message = await _astatus(closed.pin("localhost", 80))
    assert status == 400 and "localhost resolves to a loopback address" in message
    open_ = tools.WebSandbox(rules=closed.rules, allow_private=True)
    assert await open_.pin("127.0.0.1", 80) == "127.0.0.1"
    assert await open_.pin("localhost", 80) in ("127.0.0.1", "::1")
    status, message = await _astatus(open_.pin("no-such-host.invalid", 443))
    assert status == 502 and "cannot be resolved" in message


# --- the fixture site ----------------------------------------------------------------------------

SITE_PAGES = {
    "/": (
        "Home",
        (
            '<nav><a href="/menu">Menu</a></nav><h1>Welcome</h1><p>Start <a href="/docs/a">Section A</a> and '
            '<a href="/docs/b">Section B</a>, <a href="/other">elsewhere</a>, <a href="/private/secret">secret</a>, '
            '<a href="https://elsewhere.example/x">external</a>, <a href="#top">top</a>, '
            '<a href="/docs/a#again">A again</a>.</p>'
        ),
    ),
    "/docs/a": ("Section A", '<h1>A</h1><p>Text of A, see <a href="/docs/c">C</a>.</p>'),
    "/docs/b": ("Section B", '<h1>B</h1><p>Text of B, back to <a href="/docs/a">A</a>.</p>'),
    "/docs/c": ("Section C", "<h1>C</h1><p>Text of C.</p>"),
    "/other": ("Other", "<p>Outside the docs.</p>"),
    "/private/secret": ("Secret", "<p>Robots keep out.</p>"),
}


def _page(title: str, body: str) -> str:
    return f"<!DOCTYPE html><html><head><title>{title}</title></head><body>{body}</body></html>"


@pytest_asyncio.fixture
async def site() -> AsyncIterator[str]:
    """A small site served in-process: pages that link each other, robots.txt, redirects to an
    allowed page, to a private address and to a foreign host, a slow page, a large body and two
    pages whose charset is not UTF-8."""
    app = web.Application()

    async def page(request: web.Request) -> web.Response:
        title, body = SITE_PAGES[request.path]
        return web.Response(text=_page(title, body), content_type="text/html")

    async def robots(_request: web.Request) -> web.Response:
        return web.Response(text="User-agent: *\nDisallow: /private/\n", content_type="text/plain")

    async def plain(_request: web.Request) -> web.Response:
        return web.Response(text="plain text body\n", content_type="text/plain")

    async def data(_request: web.Request) -> web.Response:
        return web.json_response({"a": 1})

    async def image(_request: web.Request) -> web.Response:
        return web.Response(body=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, content_type="image/png")

    async def big(_request: web.Request) -> web.Response:
        return web.Response(text="x" * 3_000_000, content_type="text/plain")

    async def slow(_request: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(text="late", content_type="text/plain")

    def redirect(location: str) -> Callable[[web.Request], Any]:
        async def handler(_request: web.Request) -> web.Response:
            raise web.HTTPFound(location)

        return handler

    async def latin1(_request: web.Request) -> web.Response:
        return web.Response(
            body=_page("Caf\xe9", "<p>Caf\xe9</p>").encode("latin-1"),
            content_type="text/html",
            charset="iso-8859-1",
        )

    async def meta_charset(_request: web.Request) -> web.Response:
        html = '<html><head><meta charset="iso-8859-1"><title>Caf\xe9</title></head><body><p>Caf\xe9</p></body></html>'
        return web.Response(body=html.encode("latin-1"), headers={"Content-Type": "text/html"})

    async def missing(_request: web.Request) -> web.Response:
        return web.Response(
            text=_page("Not found", "<p>No such page.</p>"), content_type="text/html", status=404
        )

    for path in SITE_PAGES:
        app.router.add_get(path, page)
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/plain.txt", plain)
    app.router.add_get("/data.json", data)
    app.router.add_get("/image.png", image)
    app.router.add_get("/big", big)
    app.router.add_get("/slow", slow)
    app.router.add_get("/redirect", redirect("/docs/a"))
    app.router.add_get("/redirect-private", redirect("http://10.0.0.1/"))
    app.router.add_get("/redirect-other", redirect("https://not-allowed.example/"))
    app.router.add_get("/loop", redirect("/loop"))
    app.router.add_get("/latin1", latin1)
    app.router.add_get("/meta-charset", meta_charset)
    app.router.add_get("/missing", missing)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


SITE_SETTINGS = {
    tools.WEB_ALLOW: "http://127.0.0.1",
    tools.WEB_ALLOW_PRIVATE: "true",
    tools.WEB_CRAWL_DELAY_MS: 0,
    tools.WEB_TIMEOUT_MS: 500,
    tools.WEB_MAX_BYTES: 65536,
}


@pytest_asyncio.fixture
async def web_tools(site: str, configure: Callable[[dict[str, Any]], None]) -> AsyncIterator[Any]:
    configure(SITE_SETTINGS)
    client = tools.WebTools(tools.load_web_sandbox())
    await client.open()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fetch_a_page_as_text_markdown_and_raw(site: str, web_tools: Any) -> None:
    reply = await web_tools.web_fetch({}, {"url": f"{site}/"})
    assert (reply["url"], reply["status"], reply["content_type"], reply["title"]) == (
        f"{site}/",
        200,
        "text/html",
        "Home",
    )
    assert reply["text"].startswith("Welcome\n\nStart Section A and Section B")
    assert "Menu" not in reply["text"]
    assert reply["bytes"] > 100 and reply["truncated"] is False
    markdown = await web_tools.web_fetch({}, {"url": f"{site}/", "as": "markdown"})
    assert markdown["text"].startswith(
        f"# Welcome\n\nStart [Section A]({site}/docs/a) and [Section B]({site}/docs/b)"
    )
    raw = await web_tools.web_fetch({}, {"url": f"{site}/", "as": "raw"})
    assert raw["text"].startswith("<!DOCTYPE html>") and raw["title"] == ""


@pytest.mark.asyncio
async def test_fetch_text_resources_and_refuse_binaries(site: str, web_tools: Any) -> None:
    plain = await web_tools.web_fetch({}, {"url": f"{site}/plain.txt"})
    assert (plain["content_type"], plain["text"], plain["title"]) == (
        "text/plain",
        "plain text body\n",
        "",
    )
    data = await web_tools.web_fetch({}, {"url": f"{site}/data.json", "as": "markdown"})
    assert json.loads(data["text"]) == {"a": 1}
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/image.png"}))
    assert status == 400 and message == "the response is image/png, not text"
    missing = await web_tools.web_fetch({}, {"url": f"{site}/missing"})
    assert (missing["status"], missing["title"]) == (404, "Not found"), (
        "a page's own status is a result, not an error"
    )


@pytest.mark.asyncio
async def test_fetch_cuts_the_body_and_the_text(
    site: str, web_tools: Any, configure: Callable[[dict[str, Any]], None]
) -> None:
    big = await web_tools.web_fetch({}, {"url": f"{site}/big"})
    assert (big["bytes"], big["truncated"]) == (65536, True)
    assert len(big["text"]) == tools.DEFAULT_PAGE_MAX_CHARS
    configure({tools.WEB_PAGE_MAX_CHARS: 100})
    small = tools.WebTools(tools.load_web_sandbox())
    try:
        cut = await small.web_fetch({}, {"url": f"{site}/", "as": "markdown"})
        assert len(cut["text"]) == 100 and cut["truncated"] is True
    finally:
        await small.close()


@pytest.mark.asyncio
async def test_fetch_decodes_the_charset_of_the_header_or_the_meta_tag(
    site: str, web_tools: Any
) -> None:
    header = await web_tools.web_fetch({}, {"url": f"{site}/latin1"})
    assert (header["title"], header["text"]) == ("Café", "Café")
    meta = await web_tools.web_fetch({}, {"url": f"{site}/meta-charset"})
    assert (meta["title"], meta["text"]) == ("Café", "Café")


@pytest.mark.asyncio
async def test_fetch_follows_allowed_redirects_and_refuses_the_others(
    site: str, web_tools: Any
) -> None:
    followed = await web_tools.web_fetch({}, {"url": f"{site}/redirect"})
    assert (followed["url"], followed["status"], followed["title"]) == (
        f"{site}/docs/a",
        200,
        "Section A",
    )
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/redirect-private"}))
    assert status == 400 and "10.0.0.1 is not one of the sites" in message
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/redirect-other"}))
    assert status == 400 and "not-allowed.example is not one of the sites" in message
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/loop"}))
    assert status == 400 and "too many redirects" in message


@pytest.mark.asyncio
async def test_fetch_times_out_and_refuses_outside_the_allowlist(site: str, web_tools: Any) -> None:
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/slow"}))
    assert status == 408 and "did not answer within 500 ms" in message
    status, message = await _astatus(web_tools.web_fetch({}, {"url": "https://docs.example.com/"}))
    assert status == 400 and "not one of the sites" in message
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/", "as": "pdf"}))
    assert status == 400 and "as must be one of text, markdown, raw" in message
    status, message = await _astatus(web_tools.web_fetch({}, {"url": f"{site}/", "mode": "text"}))
    assert status == 400 and "web_fetch does not take mode" in message


@pytest.mark.asyncio
async def test_the_private_range_gate_closes_the_fixture_without_the_flag(
    site: str, configure: Callable[[dict[str, Any]], None]
) -> None:
    configure({tools.WEB_ALLOW: "http://127.0.0.1", tools.WEB_ALLOW_PRIVATE: False})
    closed = tools.WebTools(tools.load_web_sandbox())
    try:
        status, message = await _astatus(closed.web_fetch({}, {"url": f"{site}/"}))
        assert status == 400 and "127.0.0.1 resolves to a loopback address" in message
    finally:
        await closed.close()
    configure({tools.WEB_ALLOW: "127.0.0.1", tools.WEB_ALLOW_PRIVATE: True})
    https_only = tools.WebTools(tools.load_web_sandbox())
    try:
        status, message = await _astatus(https_only.web_fetch({}, {"url": f"{site}/"}))
        assert status == 400 and "allowed over https only" in message
    finally:
        await https_only.close()


@pytest.mark.asyncio
async def test_logs_carry_the_host_and_the_size_never_the_url(
    site: str, web_tools: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await web_tools.web_fetch({}, {"url": f"{site}/docs/a?token=do-not-log-me"})
    assert "web.fetch 127.0.0.1 200" in caplog.text
    assert "do-not-log-me" not in caplog.text and "/docs/a" not in caplog.text


@pytest.mark.asyncio
async def test_crawl_follows_links_breadth_first_inside_the_include(
    site: str, web_tools: Any
) -> None:
    reply = await web_tools.web_crawl({}, {"url": f"{site}/", "include": "/docs/"})
    assert [p["url"] for p in reply["pages"]] == [
        f"{site}/",
        f"{site}/docs/a",
        f"{site}/docs/b",
        f"{site}/docs/c",
    ]
    assert [p["title"] for p in reply["pages"]] == ["Home", "Section A", "Section B", "Section C"]
    assert reply["pages"][1]["text"] == "A\n\nText of A, see C."
    assert (reply["visited"], reply["truncated"]) == (4, False)
    relative = await web_tools.web_crawl({}, {"url": f"{site}/", "include": "ocs/"})
    assert [p["url"] for p in relative["pages"]] == [
        f"{site}/",
        f"{site}/docs/a",
        f"{site}/docs/b",
        f"{site}/docs/c",
    ], "a relative include matches anywhere in the path"
    shallow = await web_tools.web_crawl({}, {"url": f"{site}/", "include": "docs/", "max_depth": 1})
    assert [p["url"] for p in shallow["pages"]] == [f"{site}/", f"{site}/docs/a", f"{site}/docs/b"]
    assert shallow["truncated"] is False
    alone = await web_tools.web_crawl({}, {"url": f"{site}/docs/c", "max_depth": 0})
    assert len(alone["pages"]) == 1 and alone["truncated"] is False


@pytest.mark.asyncio
async def test_crawl_honours_robots_the_host_and_the_caps(
    site: str, web_tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = await web_tools.web_crawl({}, {"url": f"{site}/"})
    urls = [p["url"] for p in reply["pages"]]
    assert f"{site}/private/secret" not in urls, "robots.txt is honoured"
    assert f"{site}/other" in urls and not any("elsewhere.example" in u for u in urls), (
        "same host only"
    )
    assert reply["visited"] == 5
    capped = await web_tools.web_crawl({}, {"url": f"{site}/", "max_pages": 2})
    assert len(capped["pages"]) == 2 and capped["truncated"] is True
    monkeypatch.setattr(tools, "CRAWL_RESULT_MAX_CHARS", 30)
    budget = await web_tools.web_crawl({}, {"url": f"{site}/"})
    assert budget["truncated"] is True and sum(len(p["text"]) for p in budget["pages"]) <= 30


@pytest.mark.asyncio
async def test_crawl_caps_max_pages_by_the_configured_cap(
    site: str, configure: Callable[[dict[str, Any]], None]
) -> None:
    configure({**SITE_SETTINGS, tools.WEB_CRAWL_MAX_PAGES: 3})
    client = tools.WebTools(tools.load_web_sandbox())
    try:
        reply = await client.web_crawl({}, {"url": f"{site}/", "max_pages": 50})
        assert len(reply["pages"]) == 3 and reply["truncated"] is True
    finally:
        await client.close()


# --- the clock -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_now_in_utc_and_a_zone() -> None:
    reply = await tools.time_now({}, None)
    assert set(reply) == {"utc", "local", "timezone", "offset", "weekday", "epoch_ms"}
    assert reply["utc"].endswith("Z") and reply["timezone"] == "UTC" and reply["offset"] == "+00:00"
    moment = datetime.fromtimestamp(reply["epoch_ms"] / 1000, tz=UTC)
    assert reply["weekday"] == moment.strftime("%A")
    assert reply["local"].startswith(reply["utc"][:19]) and reply["local"].endswith("+00:00")
    singapore = await tools.time_now({}, {"timezone": "Asia/Singapore"})
    assert singapore["offset"] == "+08:00" and singapore["local"].endswith("+08:00")
    assert singapore["timezone"] == "Asia/Singapore"
    status, message = await _astatus(tools.time_now({}, {"timezone": "Mars/Olympus"}))
    assert status == 400 and "is not a time zone" in message
    status, message = await _astatus(tools.time_now({}, {"tz": "UTC"}))
    assert status == 400 and "time_now does not take tz" in message


# --- the boot: what registers, and the health route -------------------------------------------


@pytest.mark.asyncio
async def test_without_a_sandbox_only_the_clock_registers(caplog: pytest.LogCaptureFixture) -> None:
    registry = FunctionRegistry()
    app = tools.AgentTools(registry)
    with caplog.at_level(logging.INFO):
        await app.boot()
    assert app.registered == ["time.now"]
    assert app.files is None and app.web is None
    assert "File tools not registered - tools.file.root is not set" in caplog.text
    assert "Web tools not registered - tools.web.allow is empty" in caplog.text
    service = registry.get("time.now")
    assert service is not None and not service.private and service.instances == 10
    await app.shutdown()


@pytest.mark.asyncio
async def test_the_clock_can_be_switched_off(configure: Callable[[dict[str, Any]], None]) -> None:
    configure({tools.TIME_ENABLED: "false"})
    app = tools.AgentTools(FunctionRegistry())
    await app.boot()
    assert app.registered == []


@pytest.mark.asyncio
async def test_a_bad_sandbox_stops_the_boot_by_name(
    tmp_path: Path, configure: Callable[[dict[str, Any]], None], caplog: pytest.LogCaptureFixture
) -> None:
    configure({tools.FILE_ROOT: str(tmp_path / "missing")})
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stop:
        await tools.AgentTools(FunctionRegistry()).boot()
    assert stop.value.code == 1 and "tools.file.root is not a directory" in caplog.text
    configure({tools.FILE_ROOT: None, tools.WEB_ALLOW: "https://x/path"})
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await tools.AgentTools(FunctionRegistry()).boot()
    assert "is not a host pattern" in caplog.text
    configure({tools.WEB_ALLOW: "docs.example.com", tools.WEB_TIMEOUT_MS: "soon"})
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await tools.AgentTools(FunctionRegistry()).boot()
    assert "tools.web.timeout.ms must be an integer" in caplog.text


@pytest.mark.asyncio
async def test_a_full_configuration_registers_every_tool_and_serves_them_through_the_bus(
    workspace: Path,
    site: str,
    configure: Callable[[dict[str, Any]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure({**SITE_SETTINGS, tools.FILE_ROOT: str(workspace), tools.WEB_CRAWL_MAX_PAGES: 80})
    registry = FunctionRegistry()
    app = tools.AgentTools(registry)
    monkeypatch.setattr(tools, "agent_tools", app)
    with caplog.at_level(logging.WARNING):
        await app.boot()
    assert "tools.web.crawl.max.pages=80 is over the tool's cap 50" in caplog.text
    assert app.registered == list(STEP_ONE_ROUTES)
    for route in STEP_ONE_ROUTES:
        service = registry.get(route)
        assert service is not None and not service.private and service.instances == 10, route
    po = PostOffice(registry=registry)
    try:
        read = await po.request("file.read", body={"path": "notes/plan.md"}, timeout_ms=5000)
        assert read.get_status() == 200 and read.body["text"].startswith("# Plan")
        refused = await po.request("file.read", body={"path": "../x"}, timeout_ms=5000)
        assert refused.get_status() == 400 and "outside the workspace" in str(refused.body)
        fetched = await po.request("web.fetch", body={"url": f"{site}/docs/c"}, timeout_ms=5000)
        assert fetched.get_status() == 200 and fetched.body["title"] == "Section C"
        clock = await po.request("time.now", body={}, timeout_ms=5000)
        assert clock.get_status() == 200 and clock.body["timezone"] == "UTC"
        info = await tools.tools_health({"type": "info"}, None)
        assert info["service"] == "agent.tools"
        assert info["tools"] == list(STEP_ONE_ROUTES)
        assert info["graph"] is None
        assert info["workspace"] == str(workspace.resolve())
        assert info["allowed_sites"] == ["http://127.0.0.1"]
        assert await tools.tools_health({"type": "health"}, None) == "agent.tools is running fine"
    finally:
        await registry.bus.close()
        await app.shutdown()


@pytest.mark.asyncio
async def test_the_platform_hooks_boot_and_shut_the_tools_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = Platform(FunctionRegistry())
    app = tools.AgentTools(fresh.registry)
    monkeypatch.setattr(tools, "platform", fresh)
    monkeypatch.setattr(tools, "agent_tools", app)
    tools._attach()
    runner = web.AppRunner(fresh.create_app())
    await runner.setup()
    try:
        assert app.registered == ["time.now"], "the boot ran inside the server's startup"
    finally:
        await runner.cleanup()


# --- describe and the catalog files -----------------------------------------------------------


def test_describe_prints_every_row_and_what_registers(
    capsys: pytest.CaptureFixture[str], workspace: Path, configure: Callable[[dict[str, Any]], None]
) -> None:
    assert tools.describe([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 12 built-in tool(s)")
    assert "## graph.schema (graph_schema) - NOT registered (tools.graph.uri is not set)" in out
    assert "## file.extract (file_extract) - NOT registered (tools.file.root is not set)" in out
    assert "## file.read (file_read) - NOT registered (tools.file.root is not set)" in out
    assert "## web.fetch (web_fetch) - NOT registered (tools.web.allow is empty)" in out
    assert "## time.now (time_now) - registers" in out
    for row in tools.ROWS:
        assert tools.row_json(row) in out
    configure({tools.FILE_ROOT: str(workspace), tools.WEB_ALLOW: "docs.example.com, *.example.org"})
    assert tools.describe(["file_read", "web.crawl"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 2 built-in tool(s)")
    assert f"## file.read (file_read) - registers (workspace {workspace.resolve()})" in out
    assert "## web.crawl (web_crawl) - registers (2 allowed site pattern(s))" in out
    assert "file_write" not in out
    assert tools.describe(["nope"]) == 1
    assert "Unknown tool(s): nope" in capsys.readouterr().err


def test_the_catalog_files_are_the_rows_byte_for_byte() -> None:
    folder: Path = tools.CATALOG_DIR
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        f"{row['name']}.json" for row in tools.ROWS
    )
    for row in tools.ROWS:
        assert (folder / f"{row['name']}.json").read_text(encoding="utf-8") == tools.row_json(
            row
        ), row["name"]
        assert json.loads(tools.row_json(row)) == row


@pytest.mark.parametrize("row", tools.ROWS, ids=lambda r: r["name"])
def test_the_rows_satisfy_the_loops_catalog_rules(row: dict[str, Any]) -> None:
    assert list(row) in (
        ["name", "description", "input_schema", "route"],
        ["name", "description", "input_schema", "route", "write"],
    )
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", row["name"])
    assert row["name"] == row["route"].replace(".", "_")
    assert row["description"].strip() and len(row["description"]) < 700
    schema = row["input_schema"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])
    _check_schema(schema)
    assert row.get("write", False) is (row["name"] in WRITERS), (
        "the writers carry the mark, nothing else"
    )


def _check_schema(schema: dict[str, Any]) -> None:
    """Every keyword in the loop's closed subset, every type a JSON type, objects and arrays checked
    through."""
    assert set(schema) <= SCHEMA_KEYWORDS, set(schema) - SCHEMA_KEYWORDS
    assert schema["type"] in ("string", "integer", "boolean", "object", "array")
    if schema["type"] == "object":
        for prop in schema.get("properties", {}).values():
            _check_schema(prop)
        assert set(schema.get("required", [])) <= set(schema.get("properties", {}))
    if schema["type"] == "array":
        _check_schema(schema["items"])


# --- the knowledge graph: the pure half ---------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "clause"),
    [
        ("MATCH (n) RETURN n", None),
        ("MATCH (n) WHERE n.name = 'please SET this' RETURN n", None),
        ("MATCH (n:`CREATE`) RETURN n.offset, n.settings // DELETE later", None),
        ("MATCH (n) RETURN n /* MERGE */", None),
        ("CREATE (n:X) RETURN n", "CREATE"),
        ("match (n) set n.x = 1", "SET"),
        ("MATCH (n) DETACH DELETE n", "DELETE"),
        ("MERGE (n:X {k: 1})", "MERGE"),
        ("MATCH (n) REMOVE n:X", "REMOVE"),
        ("DROP INDEX x", "DROP"),
        ("LOAD   CSV FROM 'file:///x.csv' AS row RETURN row", "LOAD CSV"),
    ],
)
def test_write_clauses_are_found_outside_literals_and_comments(
    query: str, clause: str | None
) -> None:
    assert tools.write_clause(query) == clause


def test_delete_clauses() -> None:
    assert tools.delete_clause("MATCH (n) DETACH DELETE n") == "DELETE"
    assert tools.delete_clause("DROP CONSTRAINT c") == "DROP"
    assert tools.delete_clause("MERGE (n:X) SET n.deleted = 'DELETE'") is None
    assert tools.delete_clause("CREATE INDEX i FOR (n:X) ON (n.key)") is None


@dataclass
class FakeNode:
    element_id: str
    labels: frozenset[str]
    props: dict[str, Any]

    def items(self) -> Any:
        return self.props.items()


@dataclass
class FakeRelationship:
    element_id: str
    type: str
    start_node: FakeNode
    end_node: FakeNode
    props: dict[str, Any]

    def items(self) -> Any:
        return self.props.items()


@dataclass
class FakePath:
    nodes: list[FakeNode]
    relationships: list[FakeRelationship]


def test_render_value_shapes_nodes_relationships_paths_and_driver_types() -> None:
    import neo4j.graph
    import neo4j.spatial
    import neo4j.time

    a = FakeNode("4:g:1", frozenset({"Party", "Clause"}), {"key": "clause:1", "n": 1})
    b = FakeNode("4:g:2", frozenset({"Party"}), {"key": "party:a"})
    r = FakeRelationship("5:g:9", "BINDS", a, b, {"since": 2026})
    assert tools.render_value(a) == {
        "id": "4:g:1",
        "labels": ["Clause", "Party"],
        "properties": {"key": "clause:1", "n": 1},
    }
    assert tools.render_value(r) == {
        "id": "5:g:9",
        "type": "BINDS",
        "start": "4:g:1",
        "end": "4:g:2",
        "properties": {"since": 2026},
    }
    rendered_path = tools.render_value(FakePath([a, b], [r]))
    assert [item.get("id") for item in rendered_path] == ["4:g:1", "5:g:9", "4:g:2"]
    # the driver's own node class renders the same way
    real = neo4j.graph.Node(neo4j.graph.Graph(), "4:real:7", 7, ["Clause"], {"key": "clause:7"})
    assert tools.render_value(real) == {
        "id": "4:real:7",
        "labels": ["Clause"],
        "properties": {"key": "clause:7"},
    }
    assert (
        tools.render_value(neo4j.time.DateTime(2026, 11, 17, 10, 0, 0))
        == "2026-11-17T10:00:00.000000000"
    )
    assert tools.render_value(neo4j.time.Date(2026, 11, 17)) == "2026-11-17"
    assert tools.render_value(neo4j.spatial.CartesianPoint((1.0, 2.0))) == {
        "srid": 7203,
        "coordinates": [1.0, 2.0],
    }
    assert tools.render_value(b"\x00\x01") == "AAE="
    assert tools.render_value({"k": [1, "a", None, True]}) == {"k": [1, "a", None, True]}
    assert tools.render_value(3.5) == 3.5


def test_graph_errors_map_to_statuses_by_type() -> None:
    import neo4j.exceptions as errors

    def hydrate(code: str, message: str) -> BaseException:
        return errors.Neo4jError._hydrate_neo4j(
            code=code, message=message
        )  # the driver's own factory

    syntax = tools.graph_error(
        hydrate("Neo.ClientError.Statement.SyntaxError", "Invalid input 'X'"), timeout_ms=10
    )
    assert (syntax.status, syntax.message) == (
        400,
        "Neo.ClientError.Statement.SyntaxError: Invalid input 'X'",
    )
    timed = tools.graph_error(
        hydrate("Neo.ClientError.Transaction.TransactionTimedOut", "took too long"), timeout_ms=10
    )
    assert timed.status == 408 and "within 10 ms" in timed.message
    auth = tools.graph_error(
        hydrate("Neo.ClientError.Security.Unauthorized", "the credential itself"), timeout_ms=10
    )
    assert auth.status == 503 and "the credential itself" not in auth.message
    transient = tools.graph_error(
        hydrate("Neo.TransientError.Transaction.DeadlockDetected", "deadlock"), timeout_ms=10
    )
    assert transient.status == 503
    unavailable = tools.graph_error(errors.ServiceUnavailable("gone"), timeout_ms=10)
    assert (unavailable.status, unavailable.message) == (
        503,
        "the graph database is not reachable (ServiceUnavailable)",
    )
    assert tools.graph_error(TimeoutError(), timeout_ms=10).status == 408
    assert tools.graph_error(RuntimeError("x"), timeout_ms=10).status == 502
    passthrough = AppException(400, "mine")
    assert tools.graph_error(passthrough, timeout_ms=10) is passthrough


def test_plan_ingest_groups_and_validates() -> None:
    plan = tools.plan_ingest(
        {
            "nodes": [
                {
                    "key": "clause:1",
                    "label": "Clause",
                    "properties": {"text": "a", "n": 1, "tags": ["x", "y"], "gone": None},
                },
                {"key": "party:a", "label": "Party"},
                {"key": "clause:2", "label": "Clause", "properties": {}},
            ],
            "relationships": [
                {
                    "from": "clause:1",
                    "to": "party:a",
                    "type": "BINDS",
                    "properties": {"since": 2026},
                }
            ],
            "source": "docs/a.md",
        },
        max_nodes=10,
    )
    assert list(plan.nodes) == ["Clause", "Party"]
    assert plan.nodes["Clause"] == [
        {"key": "clause:1", "properties": {"text": "a", "n": 1, "tags": ["x", "y"]}},
        {"key": "clause:2", "properties": {}},
    ]
    assert plan.relationships == {
        "BINDS": [{"from": "clause:1", "to": "party:a", "properties": {"since": 2026}}]
    }
    assert (plan.node_count, plan.relationship_count, plan.source) == (3, 1, "docs/a.md")
    assert plan.endpoint_keys == ["clause:1", "party:a"]
    statements = tools.ingest_statements(plan)
    assert [q for q, _ in statements] == [
        "UNWIND $rows AS row MERGE (n:`Clause` {key: row.key}) SET n += row.properties, n.source = $source",
        "UNWIND $rows AS row MERGE (n:`Party` {key: row.key}) SET n += row.properties, n.source = $source",
        (
            "UNWIND $rows AS row MATCH (a {key: row.from}) MATCH (b {key: row.to}) MERGE (a)-[r:`BINDS`]->(b) "
            "SET r += row.properties, r.source = $source"
        ),
    ]
    assert statements[0][1] == {"rows": plan.nodes["Clause"], "source": "docs/a.md"}
    without_source = tools.ingest_statements(
        tools.plan_ingest({"nodes": [{"key": "k", "label": "L"}]}, max_nodes=10)
    )
    assert without_source[0][0].endswith("SET n += row.properties")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "nothing to ingest"),
        ({"nodes": "x"}, "must be arrays"),
        ({"nodes": [{"key": "k", "label": "Bad Label"}]}, "nodes[0].label must be an identifier"),
        ({"nodes": [{"key": "k", "label": "9L"}]}, "nodes[0].label must be an identifier"),
        ({"nodes": [{"key": "", "label": "L"}]}, "nodes[0].key must be a non-empty string"),
        ({"nodes": [{"key": "k", "label": "L"}, {"key": "k", "label": "L"}]}, "appears twice"),
        (
            {"nodes": [{"key": "k", "label": "L", "properties": {"bad-name": 1}}]},
            "not an identifier",
        ),
        ({"nodes": [{"key": "k", "label": "L", "properties": {"key": "x"}}]}, "must not set key"),
        (
            {"nodes": [{"key": "k", "label": "L", "properties": {"nested": {"a": 1}}}]},
            "scalar or a list of scalars",
        ),
        (
            {"nodes": [{"key": "k", "label": "L", "properties": {"mixed": [1, {"a": 1}]}}]},
            "list of scalars",
        ),
        ({"nodes": [{"key": "k", "label": "L", "extra": 1}]}, "nodes[0] does not take extra"),
        (
            {"relationships": [{"from": "a", "to": "b", "type": "has space"}]},
            "relationships[0].type must be an identifier",
        ),
        (
            {"relationships": [{"from": "a", "to": "b"}]},
            "relationships[0].type must be an identifier",
        ),
        ({"nodes": [{"key": "k", "label": "L"}], "source": 3}, "source must be a string"),
        (
            {"nodes": [{"key": f"k{i}", "label": "L"} for i in range(3)]},
            "3 nodes is over the limit of 2",
        ),
        (
            {"relationships": [{"from": "a", "to": "b", "type": "T"} for _ in range(9)]},
            "9 relationships is over the limit of 8",
        ),
    ],
)
def test_plan_ingest_refuses_by_rule(args: dict[str, Any], message: str) -> None:
    status, text = _status(lambda: tools.plan_ingest(args, max_nodes=2))
    assert status == 400
    assert message in text, text


# --- the knowledge graph: the tools over a fake backend ----------------------------------------


class FakeTransaction(tools.Transaction):
    def __init__(self, script: Callable[[str, dict[str, Any], int | None], Any]):
        self.script = script
        self.calls: list[tuple[str, dict[str, Any], int | None]] = []

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> Any:
        self.calls.append((query, parameters or {}, limit))
        return self.script(query, parameters or {}, limit)


class FakeBackend:
    """Runs the work of a transaction against a script: (query, parameters, limit) -> Rows, or raise."""

    def __init__(self, script: Callable[[str, dict[str, Any], int | None], Any]):
        self.script = script
        self.transactions: list[FakeTransaction] = []
        self.modes: list[bool] = []
        self.closed = False

    async def verify(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def execute(self, work: Any, *, write: bool) -> Any:
        tx = FakeTransaction(self.script)
        self.transactions.append(tx)
        self.modes.append(write)
        return await work(tx)

    @property
    def calls(self) -> list[tuple[str, dict[str, Any], int | None]]:
        return [call for tx in self.transactions for call in tx.calls]


def _rows(
    columns: list[str],
    values: list[list[Any]],
    *,
    truncated: bool = False,
    counters: dict[str, int] | None = None,
) -> Any:
    return tools.Rows(columns, values, truncated, counters)


def _graph(
    script: Callable[[str, dict[str, Any], int | None], Any], **settings: Any
) -> tuple[Any, FakeBackend]:
    sandbox = tools.GraphSandbox(uri="bolt://graph.example:7687", **settings)
    backend = FakeBackend(script)
    return tools.GraphTools(sandbox, backend=backend), backend


def _schema_script(*, memgraph: bool = False) -> Callable[[str, dict[str, Any], int | None], Any]:
    def script(query: str, _parameters: dict[str, Any], _limit: int | None) -> Any:
        if query == tools.LABEL_COUNTS_QUERY:
            return _rows(["label", "count"], [["Clause", 12], ["Party", 3]])
        if query == tools.RELATIONSHIP_COUNTS_QUERY:
            return _rows(
                ["type", "from", "to", "count"],
                [["BINDS", ["Clause"], ["Party"], 7], ["BINDS", ["Rule"], ["Party"], 1]],
            )
        if query == tools.NODE_PROPERTIES_QUERIES[0]:
            if memgraph:
                raise RuntimeError("There is no procedure named 'db.schema.nodeTypeProperties'.")
            return _rows(
                ["nodeLabels", "propertyName", "propertyTypes"],
                [
                    [["Clause"], "text", ["String"]],
                    [["Clause"], "key", ["String"]],
                    [["Party"], "key", ["String"]],
                    [["Orphan"], None, None],
                ],
            )
        if query == tools.NODE_PROPERTIES_QUERIES[1]:
            assert memgraph, "the fallback runs only when the first procedure is missing"
            return _rows(
                ["nodeLabels", "propertyName", "propertyTypes"], [[["Clause"], "key", ["String"]]]
            )
        if query == tools.RELATIONSHIP_PROPERTIES_QUERIES[0]:
            if memgraph:
                raise RuntimeError("There is no procedure named 'db.schema.relTypeProperties'.")
            return _rows(
                ["relType", "propertyName", "propertyTypes"],
                [[":`BINDS`", "source", ["String"]], [":`CITES`", None, None]],
            )
        if query == tools.RELATIONSHIP_PROPERTIES_QUERIES[1]:
            return _rows(
                ["relType", "propertyName", "propertyTypes"], [[":BINDS", "source", ["String"]]]
            )
        raise AssertionError(f"unexpected query {query}")

    return script


@pytest.mark.asyncio
async def test_graph_schema_merges_counts_and_properties() -> None:
    graph, backend = _graph(_schema_script())
    schema = await graph.schema({}, None)
    assert schema["labels"] == [
        {
            "label": "Clause",
            "count": 12,
            "properties": [
                {"name": "key", "types": ["String"]},
                {"name": "text", "types": ["String"]},
            ],
        },
        {"label": "Orphan", "count": 0, "properties": []},
        {"label": "Party", "count": 3, "properties": [{"name": "key", "types": ["String"]}]},
    ]
    assert schema["relationship_types"] == [
        {
            "type": "BINDS",
            "count": 8,
            "from": ["Clause", "Rule"],
            "to": ["Party"],
            "properties": [{"name": "source", "types": ["String"]}],
        },
        {"type": "CITES", "count": 0, "from": [], "to": [], "properties": []},
    ]
    assert backend.modes == [False], "the schema is read in one read transaction"


@pytest.mark.asyncio
async def test_graph_schema_falls_back_to_memgraphs_procedures() -> None:
    graph, backend = _graph(_schema_script(memgraph=True))
    schema = await graph.schema({}, {})
    assert [label["label"] for label in schema["labels"]] == ["Clause", "Party"]
    assert schema["relationship_types"][0]["properties"] == [
        {"name": "source", "types": ["String"]}
    ]
    queries = [call[0] for call in backend.calls]
    assert (
        tools.NODE_PROPERTIES_QUERIES[1] in queries
        and tools.RELATIONSHIP_PROPERTIES_QUERIES[1] in queries
    )


@pytest.mark.asyncio
async def test_graph_query_runs_read_only_with_the_row_cap() -> None:
    node = FakeNode("4:g:1", frozenset({"Clause"}), {"key": "clause:1"})

    def script(query: str, parameters: dict[str, Any], limit: int | None) -> Any:
        assert "MATCH" in query and parameters == {"k": "clause:1"} and limit == 50
        return _rows(["n", "c"], [[node, 2]], truncated=True)

    graph, backend = _graph(script, max_rows=50)
    reply = await graph.query(
        {}, {"query": "MATCH (n {key: $k}) RETURN n, 2 AS c", "parameters": {"k": "clause:1"}}
    )
    assert reply == {
        "columns": ["n", "c"],
        "rows": [[{"id": "4:g:1", "labels": ["Clause"], "properties": {"key": "clause:1"}}, 2]],
        "truncated": True,
    }
    assert backend.modes == [False]
    for body, message in (
        ({"query": "MATCH (n) SET n.x = 1 RETURN n"}, "query carries SET, a write clause"),
        ({"query": "   "}, "query must not be empty"),
        ({"query": "MATCH (n) RETURN n", "parameters": [1]}, "parameters must be a map"),
        ({"cypher": "MATCH (n) RETURN n"}, "graph_query does not take cypher"),
    ):
        status, text = await _astatus(graph.query({}, body))
        assert status == 400 and message in text, text
    assert len(backend.transactions) == 1, "a refused query never reaches the database"


@pytest.mark.asyncio
async def test_graph_query_maps_a_database_failure() -> None:
    import neo4j.exceptions as errors

    def script(_query: str, _parameters: dict[str, Any], _limit: int | None) -> Any:
        raise errors.Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError", message="Invalid input"
        )

    graph, _backend = _graph(script)
    status, text = await _astatus(graph.query({}, {"query": "MATCH (n RETURN n"}))
    assert status == 400 and text == "Neo.ClientError.Statement.SyntaxError: Invalid input"


@pytest.mark.asyncio
async def test_graph_write_returns_counters_and_guards_deletion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def script(query: str, _parameters: dict[str, Any], _limit: int | None) -> Any:
        assert query.startswith("CREATE INDEX")
        return _rows([], [], counters={"indexes_added": 1})

    graph, backend = _graph(script, write=True)
    with caplog.at_level(logging.INFO):
        reply = await graph.write(
            {}, {"query": "CREATE INDEX clause_key FOR (n:Clause) ON (n.key)"}
        )
    assert reply["counters"] == {
        name: (1 if name == "indexes_added" else 0) for name in tools.COUNTER_NAMES
    }
    assert (reply["columns"], reply["rows"], reply["truncated"]) == ([], [], False)
    assert backend.modes == [True]
    assert "graph.write 200 indexes_added=1" in caplog.text
    status, text = await _astatus(graph.write({}, {"query": "MATCH (n:Clause) DETACH DELETE n"}))
    assert (
        status == 400
        and "query carries DELETE - deletion is not allowed" in text
        and tools.GRAPH_DELETE in text
    )
    status, text = await _astatus(graph.write({}, {"query": "DROP INDEX clause_key"}))
    assert status == 400 and "DROP" in text
    allowed, backend = _graph(
        lambda q, p, limit: _rows([], [], counters={"nodes_deleted": 3}), write=True, delete=True
    )
    reply = await allowed.write({}, {"query": "MATCH (n:Clause) DETACH DELETE n"})
    assert reply["counters"]["nodes_deleted"] == 3


@pytest.mark.asyncio
async def test_graph_ingest_merges_nodes_then_checks_endpoints_then_merges_relationships() -> None:
    def script(query: str, parameters: dict[str, Any], _limit: int | None) -> Any:
        if query == tools.ENDPOINTS_QUERY:
            assert parameters == {"keys": ["clause:1", "party:a"]}
            return _rows(["key"], [])
        return _rows([], [], counters={"nodes_created": 1})

    graph, backend = _graph(script, write=True)
    body = {
        "nodes": [
            {"key": "clause:1", "label": "Clause", "properties": {"text": "pay within 30 days"}},
            {"key": "party:a", "label": "Party"},
        ],
        "relationships": [{"from": "clause:1", "to": "party:a", "type": "BINDS"}],
        "source": "contracts/a.md",
    }
    assert await graph.ingest({}, body) == {"nodes": 2, "relationships": 1}
    queries = [call[0] for call in backend.calls]
    assert [q.split(" ")[0] if q != tools.ENDPOINTS_QUERY else "CHECK" for q in queries] == [
        "UNWIND",
        "UNWIND",
        "CHECK",
        "UNWIND",
    ]
    assert (
        "MERGE (n:`Clause`" in queries[0]
        and "MERGE (n:`Party`" in queries[1]
        and "MERGE (a)-[r:`BINDS`]->(b)" in queries[3]
    )
    assert backend.calls[0][1] == {
        "rows": [{"key": "clause:1", "properties": {"text": "pay within 30 days"}}],
        "source": "contracts/a.md",
    }
    assert backend.modes == [True], "one write transaction"
    # the same ingest again runs the same statements - idempotence is the MERGE's
    assert await graph.ingest({}, body) == {"nodes": 2, "relationships": 1}


@pytest.mark.asyncio
async def test_graph_ingest_refuses_a_missing_endpoint_before_any_relationship_is_written() -> None:
    def script(query: str, _parameters: dict[str, Any], _limit: int | None) -> Any:
        if query == tools.ENDPOINTS_QUERY:
            return _rows(["key"], [["ghost:1"]])
        return _rows([], [])

    graph, backend = _graph(script, write=True)
    status, text = await _astatus(
        graph.ingest(
            {},
            {
                "nodes": [{"key": "clause:1", "label": "Clause"}],
                "relationships": [{"from": "clause:1", "to": "ghost:1", "type": "BINDS"}],
            },
        )
    )
    assert (
        status == 400
        and text == "relationship endpoints not found in the graph or in this call: ghost:1"
    )
    assert [q for q in (call[0] for call in backend.calls) if "MERGE (a)" in q] == [], (
        "no relationship statement ran"
    )
    status, text = await _astatus(graph.ingest({}, {"nodes": [{"key": "k", "label": "Bad Label"}]}))
    assert status == 400 and "identifier" in text
    assert len(backend.transactions) == 1, "a refused ingest never reaches the database"


def test_graph_sandbox_address_hides_the_credential() -> None:
    sandbox = tools.GraphSandbox(uri="neo4j+s://user:secret@graph.example:7687/x?y=1")
    assert sandbox.address == "neo4j+s://graph.example:7687"


# --- file.extract --------------------------------------------------------------------------------


def _pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of text per page, with a correct cross-reference table."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_ids[index] + 1} 0 R >>".encode()
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def _docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                grid.cell(r, c).text = cell
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def documents(workspace: Path, files: Any) -> Any:
    (workspace / "docs").mkdir()
    (workspace / "docs" / "agreement.pdf").write_bytes(
        _pdf(
            [
                "Hello page one",
                "Second page here",
                "Third and last page of the agreement, with more words",
            ]
        )
    )
    (workspace / "docs" / "policy.docx").write_bytes(
        _docx(["Policy title", "Pay within thirty days."], [["Party", "Role"], ["A", "Supplier"]])
    )
    (workspace / "docs" / "page.html").write_text(
        "<html><head><title>T</title></head><body><h1>Heading</h1><p>Body text.</p></body></html>",
        encoding="utf-8",
    )
    (workspace / "docs" / "archive.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 30)
    return files


def test_extract_a_pdf_with_page_markers_and_a_range(documents: Any) -> None:
    reply = documents.extract({}, {"path": "docs/agreement.pdf"})
    assert (reply["path"], reply["type"], reply["pages"], reply["truncated"]) == (
        "docs/agreement.pdf",
        "pdf",
        3,
        False,
    )
    assert (
        reply["text"]
        == "--- page 1 ---\nHello page one\n\n--- page 2 ---\nSecond page here\n\n--- page 3 ---\nThird and last page of the agreement, with more words"
    )
    assert (
        documents.extract({}, {"path": "docs/agreement.pdf", "pages": "2"})["text"]
        == "--- page 2 ---\nSecond page here"
    )
    ranged = documents.extract({}, {"path": "docs/agreement.pdf", "pages": "3, 1-2, 9"})
    assert [line for line in ranged["text"].splitlines() if line.startswith("---")] == [
        "--- page 3 ---",
        "--- page 1 ---",
        "--- page 2 ---",
    ]
    cut = documents.extract({}, {"path": "docs/agreement.pdf", "max_chars": 100})
    assert len(cut["text"]) == 100 and cut["truncated"] is True


def test_extract_word_html_and_text_documents(documents: Any) -> None:
    word = documents.extract({}, {"path": "docs/policy.docx"})
    assert (word["type"], word["pages"]) == ("docx", 1)
    assert (
        word["text"]
        == "--- page 1 ---\nPolicy title\n\nPay within thirty days.\n\nParty | Role\nA | Supplier"
    )
    html = documents.extract({}, {"path": "docs/page.html"})
    assert (html["type"], html["text"]) == ("html", "--- page 1 ---\nHeading\n\nBody text.")
    text = documents.extract({}, {"path": "notes/plan.md"})
    assert (text["type"], text["text"]) == ("text", "--- page 1 ---\n# Plan\n\nWrite the tools.")


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({"path": "data/image.png"}, 400, "not a PDF, Word, HTML or text document (a PNG image)"),
        ({"path": "docs/archive.zip"}, 400, "not a PDF, Word, HTML or text document"),
        (
            {"path": "docs/agreement.pdf", "pages": "x"},
            400,
            "pages must be page numbers and ranges",
        ),
        ({"path": "docs/agreement.pdf", "pages": "0"}, 400, "counted from 1"),
        ({"path": "docs/agreement.pdf", "pages": "3-1"}, 400, "counted from 1"),
        ({"path": "docs/agreement.pdf", "pages": 2}, 400, "pages must be a string"),
        ({"path": "docs/agreement.pdf", "max_chars": 10}, 400, "max_chars must be at least 100"),
        ({"path": "docs/missing.pdf"}, 404, "file not found"),
        ({"path": "docs"}, 400, "is a directory"),
        ({"path": "../outside/secret.txt"}, 400, "outside the workspace"),
    ],
)
def test_extract_refuses_by_rule(
    documents: Any, body: dict[str, Any], status: int, message: str
) -> None:
    got = _status(lambda: documents.extract({}, body))
    assert got[0] == status, got
    assert message in got[1], got


def test_page_range_and_document_kind() -> None:
    assert tools.page_range(None, 3) == [1, 2, 3]
    assert tools.page_range(" ", 2) == [1, 2]
    assert tools.page_range("2-9", 3) == [2, 3]
    assert tools.page_range("1,1,2", 3) == [1, 2]
    assert tools.page_range("1-3", 0) == []
    assert tools.document_kind(b"%PDF-1.7 ...", ".pdf") == "pdf"
    assert tools.document_kind(b"<!DOCTYPE html><html>", ".txt") == "html"
    assert tools.document_kind(b"plain words", ".htm") == "html"
    assert tools.document_kind(b"plain words", ".md") == "text"
    assert tools.document_kind(b"\x89PNG\r\n", ".png") is None
    assert tools.document_kind(_docx(["x"]), ".docx") == "docx"


# --- the boot with the graph and the documents --------------------------------------------------


class FakeBolt(FakeBackend):
    def __init__(self, sandbox: Any):
        super().__init__(lambda q, p, limit: _rows([], []))
        self.sandbox = sandbox


@pytest.mark.asyncio
async def test_the_graph_tools_register_by_the_uri_and_the_write_gate(
    configure: Callable[[dict[str, Any]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(tools, "Bolt", FakeBolt)
    configure(
        {
            tools.GRAPH_URI: "bolt://graph.example:7687",
            tools.GRAPH_USERNAME: "neo4j",
            tools.GRAPH_PASSWORD: "a-value-never-logged",
            tools.TIME_ENABLED: False,
        }
    )
    reads = tools.AgentTools(FunctionRegistry())
    with caplog.at_level(logging.INFO):
        await reads.boot()
    assert reads.registered == ["graph.schema", "graph.query"]
    assert "graph.write and graph.ingest not registered - tools.graph.write is false" in caplog.text
    assert "Graph tools registered - bolt://graph.example:7687" in caplog.text
    assert "a-value-never-logged" not in caplog.text
    assert reads.graph is not None and reads.graph.sandbox.password == "a-value-never-logged"
    await reads.shutdown()
    assert reads.graph.backend.closed
    configure({tools.GRAPH_WRITE: "true", tools.GRAPH_DELETE: True})
    writes = tools.AgentTools(FunctionRegistry())
    with caplog.at_level(logging.INFO):
        await writes.boot()
    assert writes.registered == ["graph.schema", "graph.query", "graph.write", "graph.ingest"]
    assert "writes allowed, deletion allowed" in caplog.text
    monkeypatch.setattr(tools, "agent_tools", writes)
    info = await tools.tools_health({"type": "info"}, None)
    assert info["graph"] == "bolt://graph.example:7687" and info["tools"] == writes.registered


@pytest.mark.asyncio
async def test_a_graph_that_does_not_answer_at_boot_is_logged_not_fatal(
    configure: Callable[[dict[str, Any]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Unreachable(FakeBolt):
        async def verify(self) -> None:
            raise OSError("connection refused")

    monkeypatch.setattr(tools, "Bolt", Unreachable)
    configure({tools.GRAPH_URI: "bolt://graph.example:7687", tools.TIME_ENABLED: False})
    app = tools.AgentTools(FunctionRegistry())
    with caplog.at_level(logging.WARNING):
        await app.boot()
    assert app.registered == ["graph.schema", "graph.query"]
    assert "does not answer yet (OSError); calls fail until it does" in caplog.text


@pytest.mark.asyncio
async def test_a_bad_graph_configuration_stops_the_boot_by_name(
    configure: Callable[[dict[str, Any]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure({tools.GRAPH_URI: "http://graph.example:7474"})
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await tools.AgentTools(FunctionRegistry()).boot()
    assert "tools.graph.uri must be a Bolt URI" in caplog.text
    configure({tools.GRAPH_URI: "bolt://graph.example:7687"})
    monkeypatch.setattr(tools, "neo4j", None)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await tools.AgentTools(FunctionRegistry()).boot()
    assert (
        "the Bolt driver is not installed - pip install 'mercury-composable[graph]'" in caplog.text
    )


@pytest.mark.asyncio
async def test_file_extract_registers_only_with_the_documents_extra(
    workspace: Path,
    configure: Callable[[dict[str, Any]], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure({tools.FILE_ROOT: str(workspace), tools.TIME_ENABLED: False})
    with_documents = tools.AgentTools(FunctionRegistry())
    await with_documents.boot()
    assert "file.extract" in with_documents.registered
    monkeypatch.setattr(tools, "pypdf", None)
    without = tools.AgentTools(FunctionRegistry())
    with caplog.at_level(logging.INFO):
        await without.boot()
    assert "file.extract" not in without.registered
    assert (
        "file.extract not registered - pip install 'mercury-composable[documents]'" in caplog.text
    )
    assert tools.describe(["file_extract"]) == 0


def test_describe_states_for_the_graph_and_the_documents(
    capsys: pytest.CaptureFixture[str], configure: Callable[[dict[str, Any]], None]
) -> None:
    configure({tools.GRAPH_URI: "bolt://graph.example:7687"})
    assert tools.describe(["graph_query", "graph_write"]) == 0
    out = capsys.readouterr().out
    assert "## graph.query (graph_query) - registers (bolt://graph.example:7687)" in out
    assert "## graph.write (graph_write) - NOT registered (tools.graph.write is false)" in out
    configure({tools.GRAPH_WRITE: True})
    assert tools.describe(["graph_ingest"]) == 0
    assert (
        "## graph.ingest (graph_ingest) - registers (bolt://graph.example:7687)"
        in capsys.readouterr().out
    )


# --- the live graph (gated) ---------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("TOOLS_GRAPH_TEST_URI"),
    reason="a live graph database: set TOOLS_GRAPH_TEST_URI (and TOOLS_GRAPH_TEST_PASSWORD, TOOLS_GRAPH_TEST_USER), "
    "e.g. the one extensions/agent-tools/dev/graph-up.sh starts",
)
@pytest.mark.asyncio
async def test_live_graph_round_trip(configure: Callable[[dict[str, Any]], None]) -> None:
    configure(
        {
            tools.GRAPH_URI: os.environ["TOOLS_GRAPH_TEST_URI"],
            tools.GRAPH_USERNAME: os.environ.get("TOOLS_GRAPH_TEST_USER", "neo4j"),
            tools.GRAPH_PASSWORD: os.environ.get("TOOLS_GRAPH_TEST_PASSWORD", ""),
            tools.GRAPH_WRITE: True,
            tools.GRAPH_DELETE: True,
        }
    )
    graph = tools.GraphTools(tools.load_graph_sandbox())
    cleanup = "MATCH (n) WHERE n:T_Clause OR n:T_Party DETACH DELETE n"
    body = {
        "nodes": [
            {
                "key": "t_clause:1",
                "label": "T_Clause",
                "properties": {"text": "pay within 30 days", "days": 30},
            },
            {"key": "t_party:a", "label": "T_Party", "properties": {"name": "supplier a"}},
        ],
        "relationships": [
            {
                "from": "t_clause:1",
                "to": "t_party:a",
                "type": "T_BINDS",
                "properties": {"since": 2026},
            }
        ],
        "source": "contracts/a.md",
    }
    try:
        await graph.backend.verify()
        await graph.write({}, {"query": cleanup})
        assert await graph.ingest({}, body) == {"nodes": 2, "relationships": 1}
        assert await graph.ingest({}, body) == {"nodes": 2, "relationships": 1}, "idempotent"
        count = await graph.query({}, {"query": "MATCH (n:T_Clause) RETURN count(n) AS c"})
        assert count["rows"] == [[1]], "the second ingest created nothing"
        rows = await graph.query(
            {},
            {
                "query": "MATCH (a:T_Clause)-[r:T_BINDS]->(b) RETURN a, r, b, a.days + $x AS sum",
                "parameters": {"x": 1},
            },
        )
        node, relationship, party, total = rows["rows"][0]
        assert (
            node["labels"] == ["T_Clause"]
            and node["properties"]["key"] == "t_clause:1"
            and node["properties"]["source"] == "contracts/a.md"
        )
        assert (
            relationship["type"] == "T_BINDS"
            and relationship["start"] == node["id"]
            and relationship["end"] == party["id"]
        )
        assert (
            relationship["properties"] == {"since": 2026, "source": "contracts/a.md"}
            and total == 31
        )
        path = await graph.query(
            {}, {"query": "MATCH p = (:T_Clause)-[:T_BINDS]->(:T_Party) RETURN p"}
        )
        assert [item.get("labels", item.get("type")) for item in path["rows"][0][0]] == [
            ["T_Clause"],
            "T_BINDS",
            ["T_Party"],
        ]
        schema = await graph.schema({}, None)
        clause = next(label for label in schema["labels"] if label["label"] == "T_Clause")
        assert clause["count"] == 1 and {p["name"] for p in clause["properties"]} >= {
            "key",
            "text",
            "days",
            "source",
        }
        binds = next(kind for kind in schema["relationship_types"] if kind["type"] == "T_BINDS")
        assert (binds["count"], binds["from"], binds["to"]) == (1, ["T_Clause"], ["T_Party"])
        status, text = await _astatus(
            graph.query({}, {"query": "MATCH (n:T_Clause) SET n.x = 1 RETURN n"})
        )
        assert status == 400 and "write clause" in text
        status, text = await _astatus(graph.query({}, {"query": "MATCH (n:T_Clause RETURN n"}))
        assert status == 400 and text.startswith("Neo.ClientError.Statement.SyntaxError")
        status, text = await _astatus(
            graph.ingest(
                {}, {"relationships": [{"from": "t_clause:1", "to": "ghost:9", "type": "T_BINDS"}]}
            )
        )
        assert status == 400 and "ghost:9" in text
        guarded = tools.GraphTools(
            tools.GraphSandbox(
                uri=graph.sandbox.uri,
                username=graph.sandbox.username,
                password=graph.sandbox.password,
                write=True,
            )
        )
        try:
            status, text = await _astatus(guarded.write({}, {"query": cleanup}))
            assert status == 400 and "deletion is not allowed" in text
        finally:
            await guarded.close()
        deleted = await graph.write({}, {"query": cleanup})
        assert (
            deleted["counters"]["nodes_deleted"] == 2
            and deleted["counters"]["relationships_deleted"] == 1
        )
    finally:
        await graph.close()
