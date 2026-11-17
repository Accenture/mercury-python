"""
The essential built-in tools for agent loops - files inside one workspace, the web inside an
allowlist of sites, and a clock - as composable functions of a pack extension (RFC-0013; the
engines' ``draft-design-specs/agent-essential-tools.md``). A graph's agent loop (``llm.run``)
names a route of this application in its tool catalog, the engine reaches the route through
declarative Event-over-HTTP like any other function, and the model's arguments arrive as the
event body. The loop, the catalog and the graph see nothing special about a built-in tool: a route
names the capability (``file.read``, ``web.fetch``), never the implementation.

The sandbox is deployment configuration, never an argument of a call:

- ``tools.file.root`` names the one directory tree the four file tools may touch (``file.read``,
  ``file.write``, ``file.list``, ``file.search``). Every path is relative to it, resolved with
  symbolic links followed, and refused when the result is outside the root.
- ``tools.web.allow`` names the host patterns the two web tools may reach (``web.fetch``,
  ``web.crawl``), over https unless a host is listed with ``http://``; an address in a loopback,
  private, link-local or multicast range is refused unless ``tools.web.allow.private=true``, and
  every redirect is checked again.
- ``time.now`` reads nothing and registers unless ``tools.time=false``.

A tool whose sandbox is not configured does not register, so an unconfigured tool is a 404 to the
loop's pre-flight check, not an open door. Every tool refuses an argument outside its sandbox with
a 400 that names the rule - the loop returns it to the model as that call's error, and the node
never fails because of a tool. Results are text, capped by the tool's own limits, and data a model
reads, never an instruction the loop obeys.

Run:  mercury-serve extensions/agent-tools/agent_tools.py -Dtools.file.root=/srv/workspace

Each tool ships the catalog row an author copies onto the graph (the name the model sees, the
description, the input schema, the route and the write mark); ``describe`` prints them and says
which tools register under the current configuration::

    python extensions/agent-tools/agent_tools.py describe [name ...]

The same rows are kept as files under ``catalog/`` next to this module, shared byte for byte
with the Node.js twin (mercury-nodejs ``extensions/agent-tools``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import json
import re
import socket
import sys
import urllib.robotparser
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from mercury_composable import (
    AppException,
    Body,
    FunctionRegistry,
    __version__,
    app_config,
    default_registry,
    get_logger,
    iso_utc,
    platform,
    preload,
)

log = get_logger(__name__)

# --- the contract's defaults and limits -----------------------------------------------------

INSTANCES = 10
USER_AGENT = f"Mercury-Composable-AgentTools/{__version__} (+https://github.com/Accenture/mercury-composable)"
# the token robots.txt rules are matched against
ROBOTS_AGENT = "MercuryComposable"

# files: the workspace and its limits
FILE_ROOT = "tools.file.root"
FILE_MAX_BYTES = "tools.file.max.bytes"
FILE_HIDDEN = "tools.file.hidden"
FILE_WRITE_EXTENSIONS = "tools.file.write.extensions"
FILE_LINE_MAX_CHARS = "tools.file.line.max.chars"
DEFAULT_FILE_MAX_BYTES = 256 * 1024
DEFAULT_LINE_MAX_CHARS = 500
LIST_DEFAULT, LIST_CAP = 200, 2000
SEARCH_DEFAULT, SEARCH_CAP = 100, 1000
CONTEXT_CAP = 3
# how many directory entries a list or a search walks before it gives up - the tool's own bound
SCAN_LIMIT = 20000
WRITE_MODES = ("create", "overwrite", "append")

# the web: the allowlist and its limits
WEB_ALLOW = "tools.web.allow"
WEB_ALLOW_PRIVATE = "tools.web.allow.private"
WEB_MAX_BYTES = "tools.web.max.bytes"
WEB_TIMEOUT_MS = "tools.web.timeout.ms"
WEB_CRAWL_MAX_PAGES = "tools.web.crawl.max.pages"
WEB_CRAWL_DELAY_MS = "tools.web.crawl.delay.ms"
WEB_PAGE_MAX_CHARS = "tools.web.page.max.chars"
DEFAULT_WEB_MAX_BYTES = 1024 * 1024
DEFAULT_WEB_TIMEOUT_MS = 10000
MIN_WEB_TIMEOUT_MS = 100
CRAWL_PAGES_DEFAULT, CRAWL_PAGES_CAP = 10, 50
CRAWL_DEPTH_DEFAULT, CRAWL_DEPTH_CAP = 2, 5
DEFAULT_CRAWL_DELAY_MS = 500
DEFAULT_PAGE_MAX_CHARS = 20000
# a crawl's whole text budget, under the loop's default result limit (llm.run.result.limit, 65536)
CRAWL_RESULT_MAX_CHARS = 60000
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
FETCH_MODES = ("text", "markdown", "raw")

# the clock
TIME_ENABLED = "tools.time"

# the row's keys, in the order the files carry them (the loop's catalog accepts name, description,
# input_schema, route, strict, write and ask)
ROW_KEYS = ("name", "description", "input_schema", "route", "write")


class ToolsError(Exception):
    """A configuration failure: the application must not start."""


# --- the catalog rows - what an author copies onto the graph ---------------------------------
#
# The description is what the model reads; the input schema is the closed subset the loop
# validates against (every row refuses an unknown argument). No row sets `strict`: the graph's
# author adds it when the model in use constrains optional properties by grammar.


def _row(
    name: str,
    route: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    write: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "route": route,
    }
    if write:
        row["write"] = True
    return row


FILE_READ_ROW = _row(
    "file_read",
    "file.read",
    "Read a UTF-8 text file from the agent's workspace. path is relative to the workspace root "
    "(for example notes/plan.md); an absolute path or one that leaves the workspace is refused. "
    "Returns the text, the file's size in bytes and whether the text was cut at max_bytes. A binary "
    "file is an error naming its type.",
    {
        "path": {"type": "string", "description": "The file, relative to the workspace root."},
        "max_bytes": {
            "type": "integer",
            "minimum": 1,
            "description": "Read at most this many bytes (default and cap: the workspace's limit).",
        },
    },
    ["path"],
)

FILE_WRITE_ROW = _row(
    "file_write",
    "file.write",
    "Write a UTF-8 text file in the agent's workspace. path is relative to the workspace root; "
    "missing parent folders are created. mode is create (the default; it refuses a file that exists), "
    "overwrite (replace the file) or append (add to its end). Returns the path, the bytes written "
    "and the mode.",
    {
        "path": {"type": "string", "description": "The file, relative to the workspace root."},
        "content": {"type": "string", "description": "The text to write."},
        "mode": {
            "type": "string",
            "enum": list(WRITE_MODES),
            "default": "create",
            "description": "create refuses an existing file; overwrite replaces it; append extends it.",
        },
    },
    ["path", "content"],
    write=True,
)

FILE_LIST_ROW = _row(
    "file_list",
    "file.list",
    "List the files and folders of a workspace directory. path is a folder relative to the "
    "workspace root (default: the root); glob filters by pattern, recursively with ** (for example "
    "**/*.md). Each entry has its path, type (file or directory), size in bytes and modification "
    "time. Hidden entries are not listed. The list is cut at max_entries.",
    {
        "path": {
            "type": "string",
            "default": ".",
            "description": "The folder, relative to the workspace root.",
        },
        "glob": {
            "type": "string",
            "description": "A pattern such as *.md or **/*.py; without it, the folder's "
            "direct entries.",
        },
        "max_entries": {
            "type": "integer",
            "minimum": 1,
            "maximum": LIST_CAP,
            "default": LIST_DEFAULT,
            "description": "At most this many entries.",
        },
    },
    [],
)

FILE_SEARCH_ROW = _row(
    "file_search",
    "file.search",
    "Search the text files of the workspace for a literal (the default) or a regular expression "
    "(regex: true), case-insensitive unless case_sensitive is true. path narrows the search to a "
    "folder, glob to matching files (for example **/*.py); context adds up to 3 lines before and "
    "after each match. Returns the matching lines with their file and line number, how many files "
    "were searched and whether the result was cut at max_results. Binary files are skipped.",
    {
        "pattern": {
            "type": "string",
            "description": "The text to find, or a regular expression with regex: true.",
        },
        "regex": {
            "type": "boolean",
            "default": False,
            "description": "Treat pattern as a regular expression.",
        },
        "path": {
            "type": "string",
            "default": ".",
            "description": "The folder to search, relative to the root.",
        },
        "glob": {
            "type": "string",
            "default": "**/*",
            "description": "The files to search, as a pattern.",
        },
        "case_sensitive": {"type": "boolean", "default": False},
        "context": {
            "type": "integer",
            "minimum": 0,
            "maximum": CONTEXT_CAP,
            "default": 0,
            "description": "Lines of context before and after each match.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": SEARCH_CAP,
            "default": SEARCH_DEFAULT,
            "description": "At most this many matches.",
        },
    },
    ["pattern"],
)

WEB_FETCH_ROW = _row(
    "web_fetch",
    "web.fetch",
    "Fetch one web page or text resource from a site this agent may reach and return its readable "
    "text. HTML is reduced to its text with scripts, styles and navigation dropped; as: markdown "
    "keeps headings, lists and links as Markdown; as: raw returns the body as received when it is "
    "text. A URL outside the allowed sites is an error naming the rule. Returns the final URL, the "
    "HTTP status, the content type, the title, the text, the size in bytes and whether the text was "
    "cut.",
    {
        "url": {"type": "string", "description": "The https URL to fetch."},
        "as": {
            "type": "string",
            "enum": list(FETCH_MODES),
            "default": "text",
            "description": "text: readable text; markdown: text with headings, lists and links; raw: the body.",
        },
    },
    ["url"],
)

WEB_CRAWL_ROW = _row(
    "web_crawl",
    "web.crawl",
    "Read a documentation section or a small site: fetch the start page and follow its links on "
    "the same site, breadth-first, up to max_pages pages and max_depth links deep, honouring "
    "robots.txt. include keeps the crawl under a path prefix (for example /docs/guides/). Returns "
    "each page's URL, title and readable text, how many pages were fetched and whether the result "
    "was cut; when it was cut, ask again from a narrower start page or with include.",
    {
        "url": {"type": "string", "description": "The start page."},
        "max_pages": {
            "type": "integer",
            "minimum": 1,
            "maximum": CRAWL_PAGES_CAP,
            "default": CRAWL_PAGES_DEFAULT,
            "description": "At most this many pages.",
        },
        "max_depth": {
            "type": "integer",
            "minimum": 0,
            "maximum": CRAWL_DEPTH_CAP,
            "default": CRAWL_DEPTH_DEFAULT,
            "description": "How many links deep to follow from the start page.",
        },
        "include": {
            "type": "string",
            "description": "A path prefix the followed links must start with.",
        },
    },
    ["url"],
)

TIME_NOW_ROW = _row(
    "time_now",
    "time.now",
    "The current date and time. timezone is an IANA name (for example Europe/Berlin; default UTC). "
    "Returns the UTC time, the local time in that zone, its offset, the weekday and the epoch in "
    "milliseconds. Use it before reasoning about today, deadlines or periods.",
    {
        "timezone": {"type": "string", "default": "UTC", "description": "An IANA time zone name."},
    },
    [],
)

ROWS: tuple[dict[str, Any], ...] = (
    FILE_READ_ROW,
    FILE_WRITE_ROW,
    FILE_LIST_ROW,
    FILE_SEARCH_ROW,
    WEB_FETCH_ROW,
    WEB_CRAWL_ROW,
    TIME_NOW_ROW,
)
ROW_BY_ROUTE: dict[str, dict[str, Any]] = {row["route"]: row for row in ROWS}
CATALOG_DIR = Path(__file__).resolve().parent / "catalog"


def row_json(row: dict[str, Any]) -> str:
    """A row as its catalog file carries it: two-space indent, Unicode kept, one trailing newline."""
    return json.dumps(row, indent=2, ensure_ascii=False) + "\n"


# --- configuration helpers ---------------------------------------------------------------------


def _setting(key: str, default: Any = None) -> Any:
    value = app_config().get(key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return value


def _int_setting(key: str, default: int, *, floor: int = 1, cap: int | None = None) -> int:
    raw = _setting(key, default)
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ToolsError(f"{key} must be an integer, not {raw!r}") from exc
    if value < floor:
        raise ToolsError(f"{key} must be at least {floor}")
    if cap is not None and value > cap:
        log.warning(
            "%s=%d is over the tool's cap %d - a deployment lowers a limit, never raises it; using %d",
            key,
            value,
            cap,
            cap,
        )
        return cap
    return value


def _bool_setting(key: str, default: bool) -> bool:
    raw = _setting(key, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    raise ToolsError(f"{key} must be true or false, not {raw!r}")


def _list_setting(key: str) -> tuple[str, ...]:
    raw = _setting(key)
    if raw is None:
        return ()
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return tuple(str(item).strip() for item in items if str(item).strip())


# --- argument helpers - a tool's body is a map of the row's properties ------------------------


def _arguments(body: Body, row: dict[str, Any]) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise AppException(400, f"The arguments of {row['name']} must be a map")
    known = row["input_schema"]["properties"]
    unknown = sorted(str(key) for key in body if key not in known)
    if unknown:
        raise AppException(
            400,
            f"{row['name']} does not take {', '.join(unknown)} - "
            f"its arguments are {', '.join(known)}",
        )
    return body


def _string(args: dict[str, Any], name: str, *, default: str | None = None) -> str:
    value = args.get(name)
    if value is None:
        if default is None:
            raise AppException(400, f"{name} is required")
        return default
    if not isinstance(value, str):
        raise AppException(400, f"{name} must be a string")
    return value


def _integer(args: dict[str, Any], name: str, *, default: int, floor: int, cap: int) -> int:
    value = args.get(name)
    if value is None:
        return min(default, cap)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise AppException(400, f"{name} must be an integer")
    number = int(value)
    if number < floor:
        raise AppException(400, f"{name} must be at least {floor}")
    return min(number, cap)


def _flag(args: dict[str, Any], name: str, *, default: bool) -> bool:
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AppException(400, f"{name} must be true or false")
    return value


def _choice(args: dict[str, Any], name: str, choices: tuple[str, ...], *, default: str) -> str:
    value = _string(args, name, default=default)
    if value not in choices:
        raise AppException(400, f"{name} must be one of {', '.join(choices)}")
    return value


# --- files, inside one workspace ----------------------------------------------------------------


@dataclass(frozen=True)
class FileSandbox:
    """The workspace: one directory tree and the limits of the tools inside it."""

    root: Path
    max_bytes: int = DEFAULT_FILE_MAX_BYTES
    hidden: bool = False
    write_extensions: tuple[str, ...] = ()
    line_max_chars: int = DEFAULT_LINE_MAX_CHARS


def load_file_sandbox() -> FileSandbox | None:
    """The file tools' sandbox from the configuration - ``None`` when ``tools.file.root`` is not
    set, so the file tools do not register; a root that is not a directory stops the boot."""
    root_setting = _setting(FILE_ROOT)
    if root_setting is None:
        return None
    root = Path(str(root_setting)).expanduser()
    if not root.is_dir():
        raise ToolsError(f"{FILE_ROOT} is not a directory - {root}")
    extensions = tuple(
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in _list_setting(FILE_WRITE_EXTENSIONS)
    )
    return FileSandbox(
        root=root.resolve(),
        max_bytes=_int_setting(FILE_MAX_BYTES, DEFAULT_FILE_MAX_BYTES),
        hidden=_bool_setting(FILE_HIDDEN, False),
        write_extensions=extensions,
        line_max_chars=_int_setting(FILE_LINE_MAX_CHARS, DEFAULT_LINE_MAX_CHARS, floor=20),
    )


_DRIVE = re.compile(r"^[A-Za-z]:")
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", "a PDF document"),
    (b"\x89PNG", "a PNG image"),
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"GIF8", "a GIF image"),
    (b"PK\x03\x04", "a ZIP archive or an Office document"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"\x7fELF", "an executable"),
)


def _is_absolute(text: str) -> bool:
    return (
        PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or text.startswith(("/", "\\"))
        or bool(_DRIVE.match(text))
    )


def _is_hidden(relative: str) -> bool:
    return any(part.startswith(".") and part not in (".", "..") for part in relative.split("/"))


def binary_kind(head: bytes) -> str | None:
    """What a file that is not text looks like, from its first bytes: a known format by its magic
    number, else ``binary`` when it carries a NUL byte; ``None`` for text."""
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    return "binary" if b"\x00" in head else None


def decode_utf8(data: bytes, *, truncated: bool) -> str | None:
    """The bytes as UTF-8, or ``None`` when they are not; a cut that split a multi-byte character
    is forgiven at the tail."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            return None
    for trim in range(1, 4):
        try:
            return data[:-trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _modified(stat: Any) -> str:
    moment = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class FileTools:
    """The four file tools over one workspace. The handlers are synchronous - the host runs them
    in its thread pool - and every path goes through ``resolve`` first."""

    def __init__(self, sandbox: FileSandbox):
        self.sandbox = sandbox

    @property
    def root(self) -> Path:
        return self.sandbox.root

    def resolve(self, text: str, *, what: str = "path") -> tuple[Path, str]:
        """A path argument to the file it names inside the workspace, with symbolic links followed,
        and its workspace-relative form; outside the root, hidden when hidden entries are off, or
        absolute, it is refused by rule."""
        value = text.strip() or "."
        if _is_absolute(value):
            raise AppException(400, f"{what} must be relative to the workspace, not absolute")
        try:
            resolved = (self.root / value).resolve()
        except (OSError, RuntimeError) as exc:
            raise AppException(400, f"{what} cannot be resolved ({type(exc).__name__})") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise AppException(400, f"{what} is outside the workspace")
        relative = "." if resolved == self.root else resolved.relative_to(self.root).as_posix()
        if not self.sandbox.hidden and _is_hidden(relative):
            raise AppException(
                400,
                f"{what} names a hidden entry, which the workspace does not expose ({FILE_HIDDEN})",
            )
        return resolved, relative

    def _inside(self, entry: Path) -> Path | None:
        """The resolved target of a directory entry when it stays inside the workspace, else None."""
        try:
            resolved = entry.resolve()
        except (OSError, RuntimeError):
            return None
        if resolved != self.root and self.root not in resolved.parents:
            return None
        return resolved

    def _walk(
        self, base: Path, pattern: str | None, *, files_only: bool
    ) -> tuple[list[tuple[str, Path]], bool]:
        """The entries under a directory, sorted by their workspace-relative path, as (relative,
        resolved) pairs; hidden entries and entries that escape the workspace are left out. The
        second value says the walk hit the scan bound."""
        try:
            candidates = base.glob(pattern) if pattern else base.iterdir()
        except (ValueError, NotImplementedError) as exc:
            raise AppException(400, f"glob is not a valid pattern ({exc})") from exc
        found: list[tuple[str, Path]] = []
        bounded = False
        try:
            for entry in candidates:
                if len(found) >= SCAN_LIMIT:
                    bounded = True
                    break
                try:
                    relative = entry.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if not self.sandbox.hidden and _is_hidden(relative):
                    continue
                resolved = self._inside(entry)
                if resolved is None or (files_only and not resolved.is_file()):
                    continue
                found.append((relative, resolved))
        except (ValueError, NotImplementedError) as exc:
            raise AppException(400, f"glob is not a valid pattern ({exc})") from exc
        found.sort(key=lambda item: item[0])
        return found, bounded

    def read(self, _headers: dict[str, str], body: Body) -> Any:
        args = _arguments(body, FILE_READ_ROW)
        path = _string(args, "path")
        max_bytes = _integer(
            args, "max_bytes", default=self.sandbox.max_bytes, floor=1, cap=self.sandbox.max_bytes
        )
        target, relative = self.resolve(path)
        if target.is_dir():
            raise AppException(400, f"{relative} is a directory - file_list lists it")
        if not target.is_file():
            raise AppException(404, f"file not found: {relative}")
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        kind = binary_kind(data[:8192])
        if kind is not None:
            raise AppException(400, f"{relative} is not a text file ({kind})")
        text = decode_utf8(data, truncated=truncated)
        if text is None:
            raise AppException(400, f"{relative} is not UTF-8 text")
        log.info("file.read 200 %d bytes%s", len(data), " (cut)" if truncated else "")
        return {"path": relative, "bytes": size, "text": text, "truncated": truncated}

    def write(self, _headers: dict[str, str], body: Body) -> Any:
        args = _arguments(body, FILE_WRITE_ROW)
        path = _string(args, "path")
        content = _string(args, "content")
        mode = _choice(args, "mode", WRITE_MODES, default="create")
        target, relative = self.resolve(path)
        if relative == "." or target.is_dir():
            raise AppException(400, f"{relative} is a directory - name a file")
        allowed = self.sandbox.write_extensions
        if allowed and target.suffix.lower() not in allowed:
            raise AppException(
                400, f"{relative} does not end in {', '.join(allowed)} ({FILE_WRITE_EXTENSIONS})"
            )
        data = content.encode("utf-8")
        if len(data) > self.sandbox.max_bytes:
            raise AppException(
                400,
                f"content is {len(data)} bytes, over the workspace's limit of "
                f"{self.sandbox.max_bytes} ({FILE_MAX_BYTES})",
            )
        if mode == "create" and target.exists():
            raise AppException(
                409, f"{relative} exists - mode overwrite replaces it, append extends it"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = {"create": "xb", "overwrite": "wb", "append": "ab"}[mode]
        try:
            with target.open(flags) as handle:
                handle.write(data)
        except FileExistsError as exc:
            raise AppException(
                409, f"{relative} exists - mode overwrite replaces it, append extends it"
            ) from exc
        log.info("file.write 200 %d bytes (%s)", len(data), mode)
        return {"path": relative, "bytes": len(data), "mode": mode}

    def list(self, _headers: dict[str, str], body: Body) -> Any:
        args = _arguments(body, FILE_LIST_ROW)
        path = _string(args, "path", default=".")
        pattern = args.get("glob")
        if pattern is not None and not isinstance(pattern, str):
            raise AppException(400, "glob must be a string")
        max_entries = _integer(args, "max_entries", default=LIST_DEFAULT, floor=1, cap=LIST_CAP)
        base, relative = self.resolve(path)
        if not base.is_dir():
            raise AppException(
                404 if not base.exists() else 400,
                f"{relative} is not a directory"
                if base.exists()
                else f"directory not found: {relative}",
            )
        found, bounded = self._walk(base, pattern or None, files_only=False)
        entries: list[dict[str, Any]] = []
        for shown, resolved in found[:max_entries]:
            stat = resolved.stat()
            kind = "directory" if resolved.is_dir() else "file" if resolved.is_file() else "other"
            entries.append(
                {
                    "path": shown,
                    "type": kind,
                    "bytes": stat.st_size if kind == "file" else 0,
                    "modified": _modified(stat),
                }
            )
        truncated = bounded or len(found) > max_entries
        log.info("file.list 200 %d entries%s", len(entries), " (cut)" if truncated else "")
        return {"entries": entries, "truncated": truncated}

    def search(self, _headers: dict[str, str], body: Body) -> Any:
        args = _arguments(body, FILE_SEARCH_ROW)
        pattern = _string(args, "pattern")
        regex = _flag(args, "regex", default=False)
        path = _string(args, "path", default=".")
        file_glob = _string(args, "glob", default="**/*")
        case_sensitive = _flag(args, "case_sensitive", default=False)
        context = _integer(args, "context", default=0, floor=0, cap=CONTEXT_CAP)
        max_results = _integer(args, "max_results", default=SEARCH_DEFAULT, floor=1, cap=SEARCH_CAP)
        if not pattern:
            raise AppException(400, "pattern must not be empty")
        try:
            matcher = re.compile(
                pattern if regex else re.escape(pattern), 0 if case_sensitive else re.IGNORECASE
            )
        except re.error as exc:
            raise AppException(400, f"pattern is not a valid regular expression ({exc})") from exc
        base, relative = self.resolve(path)
        if not base.is_dir():
            raise AppException(
                404 if not base.exists() else 400,
                f"{relative} is not a directory"
                if base.exists()
                else f"directory not found: {relative}",
            )
        found, bounded = self._walk(base, file_glob, files_only=True)
        width = self.sandbox.line_max_chars
        matches: list[dict[str, Any]] = []
        searched = 0
        truncated = bounded
        for shown, resolved in found:
            if truncated and len(matches) >= max_results:
                break
            try:
                data = resolved.read_bytes()
            except OSError:
                continue
            if binary_kind(data[:8192]) is not None:
                continue
            searched += 1
            lines = data.decode("utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines):
                if not matcher.search(line):
                    continue
                if len(matches) >= max_results:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": shown,
                        "line": index + 1,
                        "text": line[:width],
                        "before": [text[:width] for text in lines[max(0, index - context) : index]],
                        "after": [text[:width] for text in lines[index + 1 : index + 1 + context]],
                    }
                )
            if truncated and len(matches) >= max_results:
                break
        log.info(
            "file.search 200 %d matches in %d files%s",
            len(matches),
            searched,
            " (cut)" if truncated else "",
        )
        return {"matches": matches, "files_searched": searched, "truncated": truncated}


# --- HTML to text, in-house ----------------------------------------------------------------------

_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "nav", "header", "footer"}
)
# an element marked as page chrome by its ARIA role is dropped like the tags above, and so is one
# hidden from assistive technology - a documentation site\'s sidebar is a div with role="navigation"
# as often as a nav
_DROP_ROLES = frozenset({"navigation", "banner", "contentinfo"})
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "aside",
        "blockquote",
        "pre",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "dl",
        "dt",
        "dd",
        "figure",
        "figcaption",
        "form",
        "fieldset",
        "hr",
        "address",
        "details",
        "summary",
    }
)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_VOID_TAGS = frozenset(
    {
        "br",
        "hr",
        "img",
        "input",
        "meta",
        "link",
        "area",
        "base",
        "col",
        "embed",
        "source",
        "track",
        "wbr",
        "param",
    }
)
_SPACES = re.compile(r"[ \t\r\n\f\v]+")


def _is_chrome(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == "role" and (value or "").strip().lower() in _DROP_ROLES:
            return True
        if name == "aria-hidden" and (value or "").strip().lower() == "true":
            return True
    return False


class HtmlText(HTMLParser):
    """A small reader of HTML for a model: ``script``, ``style``, ``nav``, ``header`` and
    ``footer`` dropped, the text of the rest kept, with headings, list items, paragraphs, code and
    links rendered as readable text or as Markdown. Enough for documentation and articles."""

    def __init__(self, base_url: str = "", *, markdown: bool = False):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.markdown = markdown
        self.title = ""
        self.links: list[str] = []
        self._blocks: list[str] = []
        self._current: list[str] = []
        self._drop = 0
        self._pre = 0
        self._in_title = False
        self._heading: int | None = None
        self._list_depth = 0
        self._item_prefix: str | None = None
        self._href: str | None = None
        self._link_text: list[str] = []
        self._code = 0

    # -- the blocks

    def _flush(self) -> None:
        text = "".join(self._current)
        self._current = []
        if self._pre:
            body = text.strip("\n")
            if body.strip():
                self._blocks.append(f"```\n{body}\n```" if self.markdown else body)
            return
        body = _SPACES.sub(" ", text).strip()
        if not body:
            return
        if self._heading is not None:
            self._blocks.append(f"{'#' * self._heading} {body}" if self.markdown else body)
        elif self._item_prefix is not None:
            self._blocks.append(f"{self._item_prefix}{body}")
        else:
            self._blocks.append(body)

    def _open_block(self) -> None:
        self._end_link()
        self._flush()

    def _end_link(self) -> None:
        """Close the open link, if any: its target is collected and, in Markdown, written after
        its text - also when broken markup never closed it."""
        if self._href is None:
            return
        self.links.append(self._href.split("#", 1)[0])
        label = _SPACES.sub(" ", "".join(self._link_text)).strip()
        if self.markdown and label:
            self._current.append(f"]({self._href})")
        self._href = None
        self._link_text = []

    # -- the parser's callbacks

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop:
            if tag not in _VOID_TAGS:
                self._drop += 1
            return
        if tag in _DROP_TAGS or (tag not in _VOID_TAGS and _is_chrome(attrs)):
            self._open_block()
            self._drop = 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _HEADINGS:
            self._open_block()
            self._heading = _HEADINGS[tag]
            return
        if tag in ("ul", "ol"):
            self._open_block()
            self._list_depth += 1
            return
        if tag == "li":
            self._open_block()
            self._item_prefix = "  " * max(0, self._list_depth - 1) + "- "
            return
        if tag == "pre":
            self._open_block()
            self._pre += 1
            return
        if tag == "br":
            self._current.append("\n" if self._pre else " ")
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._end_link()
                self._href = urljoin(self.base_url, href) if self.base_url else href
                self._link_text = []
            return
        if tag == "code" and not self._pre:
            self._code += 1
            if self.markdown:
                self._current.append("`")
            return
        if tag in ("td", "th"):
            self._current.append(" ")
            return
        if tag in _BLOCK_TAGS:
            self._open_block()

    def handle_endtag(self, tag: str) -> None:
        if self._drop:
            if tag not in _VOID_TAGS:
                self._drop -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _HEADINGS:
            self._flush()
            self._heading = None
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_depth = max(0, self._list_depth - 1)
            self._item_prefix = None
            return
        if tag == "li":
            self._flush()
            self._item_prefix = None
            return
        if tag == "pre":
            self._flush()
            self._pre = max(0, self._pre - 1)
            return
        if tag == "a":
            self._end_link()
            return
        if tag == "code" and not self._pre:
            self._code = max(0, self._code - 1)
            if self.markdown:
                self._current.append("`")
            return
        if tag in _BLOCK_TAGS:
            self._end_link()
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._drop:
            return
        if self._in_title:
            self.title += data
            return
        if self._href is not None:
            if self.markdown and not self._link_text and data.strip():
                self._current.append("[")
            self._link_text.append(data)
        self._current.append(data)

    def close(self) -> None:
        super().close()
        self._end_link()
        self._flush()

    @property
    def text(self) -> str:
        return "\n\n".join(self._blocks)


def html_to_text(
    html: str, base_url: str = "", *, markdown: bool = False
) -> tuple[str, str, list[str]]:
    """A page's title, its readable text (Markdown when asked) and the absolute links it carries,
    in document order and without fragments."""
    parser = HtmlText(base_url, markdown=markdown)
    parser.feed(html)
    parser.close()
    links: list[str] = []
    seen: set[str] = set()
    for link in parser.links:
        if link not in seen:
            seen.add(link)
            links.append(link)
    return _SPACES.sub(" ", parser.title).strip(), parser.text, links


# --- the web, inside an allowlist -------------------------------------------------------------


@dataclass(frozen=True)
class HostRule:
    """One entry of ``tools.web.allow``: a host pattern, the scheme it is reached over and,
    when listed, the port."""

    pattern: str
    scheme: str = "https"
    port: int | None = None

    def __str__(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        return (
            f"{self.scheme}://{self.pattern}{port}"
            if self.scheme == "http" or port
            else self.pattern
        )


def parse_host_rules(entries: tuple[str, ...]) -> tuple[HostRule, ...]:
    """``docs.example.com``, ``*.example.com``, ``http://127.0.0.1``, ``docs.example.com:8443``:
    a host pattern with an optional scheme and port; a path, a query or credentials are refused."""
    rules: list[HostRule] = []
    for entry in entries:
        text = entry.strip()
        scheme = "https"
        if "://" in text:
            scheme, _, text = text.partition("://")
            scheme = scheme.lower()
            if scheme not in ("http", "https"):
                raise ToolsError(f"{WEB_ALLOW}: {entry} - the scheme must be http or https")
        if "/" in text or "?" in text or "@" in text or not text:
            raise ToolsError(
                f"{WEB_ALLOW}: {entry} is not a host pattern (a host, a *.pattern, "
                f"optionally with http:// or a :port)"
            )
        port: int | None = None
        host = text
        if text.startswith("["):
            host, _, rest = text[1:].partition("]")
            if rest.startswith(":"):
                port = _port(entry, rest[1:])
        elif text.count(":") == 1:
            host, _, port_text = text.partition(":")
            port = _port(entry, port_text)
        rules.append(HostRule(pattern=host.lower().rstrip("."), scheme=scheme, port=port))
    return tuple(rules)


def _port(entry: str, text: str) -> int:
    try:
        port = int(text)
    except ValueError as exc:
        raise ToolsError(f"{WEB_ALLOW}: {entry} - the port must be a number") from exc
    if not 1 <= port <= 65535:
        raise ToolsError(f"{WEB_ALLOW}: {entry} - the port must be between 1 and 65535")
    return port


@dataclass(frozen=True)
class WebSandbox:
    """The allowlist and the limits of the web tools."""

    rules: tuple[HostRule, ...]
    allow_private: bool = False
    max_bytes: int = DEFAULT_WEB_MAX_BYTES
    timeout_ms: int = DEFAULT_WEB_TIMEOUT_MS
    crawl_max_pages: int = CRAWL_PAGES_CAP
    crawl_delay_ms: int = DEFAULT_CRAWL_DELAY_MS
    page_max_chars: int = DEFAULT_PAGE_MAX_CHARS

    def matches(self, host: str, scheme: str, port: int) -> tuple[bool, bool]:
        """Whether a host is listed at all, and whether it is allowed over this scheme and port."""
        listed = False
        for rule in self.rules:
            if not fnmatch.fnmatchcase(host, rule.pattern):
                continue
            listed = True
            if (scheme == "https" or rule.scheme == "http") and (
                rule.port is None or rule.port == port
            ):
                return True, True
        return listed, False

    def check_url(self, url: str) -> SplitResult:
        """The URL a tool may fetch, by the allowlist alone (no network): an http(s) URL without
        credentials, whose host is listed for its scheme and port. Anything else is a 400 that
        names the rule; the fragment is dropped."""
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise AppException(
                400, "url must start with https:// (or http:// for a host listed with it)"
            )
        if parts.username is not None or parts.password is not None:
            raise AppException(400, "url must not carry credentials")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise AppException(400, "url has no host")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise AppException(400, "url has an invalid port") from exc
        listed, allowed = self.matches(host, scheme, port)
        if not allowed:
            if listed and scheme == "http":
                raise AppException(
                    400,
                    f"{host} is allowed over https only - list it as http://{host} to allow http",
                )
            if listed:
                raise AppException(400, f"{host} is not allowed on port {port} ({WEB_ALLOW})")
            raise AppException(
                400, f"{host} is not one of the sites this agent may reach ({WEB_ALLOW})"
            )
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return SplitResult(scheme, netloc, parts.path or "/", parts.query, "")

    async def pin(self, host: str, port: int) -> str:
        """The address a host resolves to, checked against the private ranges, so the request is
        made to the address that was checked and a second resolution cannot point elsewhere."""
        try:
            literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
                host
            )
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [literal]
        else:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            except socket.gaierror as exc:
                raise AppException(502, f"{host} cannot be resolved") from exc
            addresses = []
            for info in infos:
                text = str(info[4][0]).split("%", 1)[0]
                try:
                    address = ipaddress.ip_address(text)
                except ValueError:
                    continue
                if address not in addresses:
                    addresses.append(address)
            if not addresses:
                raise AppException(502, f"{host} cannot be resolved")
        if not self.allow_private:
            for address in addresses:
                kind = address_kind(address)
                if kind is not None:
                    raise AppException(
                        400,
                        f"{host} resolves to a {kind} address, which is refused "
                        f"({WEB_ALLOW_PRIVATE})",
                    )
        preferred = next((a for a in addresses if a.version == 4), addresses[0])
        return str(preferred)


def address_kind(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why an address is refused by default - loopback, private, link-local, multicast,
    reserved or unspecified - or ``None`` for a public address."""
    candidate: ipaddress.IPv4Address | ipaddress.IPv6Address = address
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        candidate = address.ipv4_mapped
    if candidate.is_loopback:
        return "loopback"
    if candidate.is_link_local:
        return "link-local"
    if candidate.is_multicast:
        return "multicast"
    if candidate.is_unspecified:
        return "unspecified"
    if candidate.is_private:
        return "private"
    if candidate.is_reserved:
        return "reserved"
    return None


def load_web_sandbox() -> WebSandbox | None:
    """The web tools' sandbox from the configuration - ``None`` when ``tools.web.allow`` is empty,
    so the web tools do not register."""
    entries = _list_setting(WEB_ALLOW)
    if not entries:
        return None
    return WebSandbox(
        rules=parse_host_rules(entries),
        allow_private=_bool_setting(WEB_ALLOW_PRIVATE, False),
        max_bytes=_int_setting(WEB_MAX_BYTES, DEFAULT_WEB_MAX_BYTES, floor=1024),
        timeout_ms=_int_setting(WEB_TIMEOUT_MS, DEFAULT_WEB_TIMEOUT_MS, floor=MIN_WEB_TIMEOUT_MS),
        crawl_max_pages=_int_setting(WEB_CRAWL_MAX_PAGES, CRAWL_PAGES_CAP, cap=CRAWL_PAGES_CAP),
        crawl_delay_ms=_int_setting(WEB_CRAWL_DELAY_MS, DEFAULT_CRAWL_DELAY_MS, floor=0),
        page_max_chars=_int_setting(WEB_PAGE_MAX_CHARS, DEFAULT_PAGE_MAX_CHARS, floor=100),
    )


@dataclass
class Fetched:
    """One response: the final URL, the status, the media type and charset, the body as read
    (cut at the byte limit) and whether it was cut."""

    url: str
    status: int
    content_type: str
    charset: str | None
    data: bytes
    truncated: bool

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml")

    @property
    def is_text(self) -> bool:
        kind = self.content_type
        return (
            kind.startswith("text/")
            or kind
            in (
                "application/json",
                "application/xml",
                "application/javascript",
                "application/x-yaml",
                "application/yaml",
            )
            or kind.endswith(("+json", "+xml"))
        )


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)


def decode_body(fetched: Fetched) -> str:
    """The body as text: the charset of the content type, else the one a ``meta`` tag names, else
    UTF-8; undecodable bytes are replaced, never an error."""
    charset = fetched.charset
    if charset is None and fetched.is_html:
        match = _META_CHARSET.search(fetched.data[:4096])
        if match:
            charset = match.group(1).decode("ascii", errors="ignore")
    for name in (charset, "utf-8"):
        if not name:
            continue
        try:
            return fetched.data.decode(name, errors="replace")
        except LookupError:
            continue
    return fetched.data.decode("utf-8", errors="replace")


def _host_header(parts: SplitResult) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return host if parts.port in (None, default) else f"{host}:{parts.port}"


class WebTools:
    """The two web tools over one allowlist, sharing one HTTP client without a cookie jar."""

    def __init__(self, sandbox: WebSandbox):
        self.sandbox = sandbox
        self._session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        if self._session is None:
            seconds = self.sandbox.timeout_ms / 1000
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=seconds),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, application/xhtml+xml, text/*;q=0.9, application/json;q=0.8, */*;q=0.1",
                },
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, url: str) -> Fetched:
        """One URL through the allowlist and the address check, every redirect checked again, the
        body read up to the byte limit; a timeout is a 408, a transport failure a 502."""
        if self._session is None:
            await self.open()
        session = self._session
        assert session is not None
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            parts = self.sandbox.check_url(current)
            host = (parts.hostname or "").lower().rstrip(".")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            address = await self.sandbox.pin(host, port)
            pinned_host = f"[{address}]" if ":" in address else address
            pinned = urlunsplit(
                (parts.scheme, f"{pinned_host}:{port}", parts.path, parts.query, "")
            )
            extra: dict[str, Any] = {"server_hostname": host} if parts.scheme == "https" else {}
            try:
                async with session.get(
                    pinned, headers={"Host": _host_header(parts)}, allow_redirects=False, **extra
                ) as response:
                    location = response.headers.get("Location")
                    if response.status in REDIRECT_STATUSES and location:
                        current = urljoin(urlunsplit(parts), location)
                        continue
                    data = bytearray()
                    truncated = False
                    async for chunk in response.content.iter_chunked(65536):
                        room = self.sandbox.max_bytes - len(data)
                        if len(chunk) >= room:
                            data.extend(chunk[:room])
                            truncated = len(chunk) > room or not response.content.at_eof()
                            break
                        data.extend(chunk)
                    content_type = (response.content_type or "").lower()
                    return Fetched(
                        url=urlunsplit(parts),
                        status=response.status,
                        content_type=content_type,
                        charset=response.charset,
                        data=bytes(data),
                        truncated=truncated,
                    )
            except asyncio.TimeoutError as exc:
                raise AppException(
                    408, f"{host} did not answer within {self.sandbox.timeout_ms} ms"
                ) from exc
            except aiohttp.ClientError as exc:
                raise AppException(
                    502, f"{host} could not be fetched ({type(exc).__name__})"
                ) from exc
        raise AppException(400, f"too many redirects (more than {MAX_REDIRECTS})")

    async def web_fetch(self, _headers: dict[str, str], body: Body) -> Any:
        args = _arguments(body, WEB_FETCH_ROW)
        url = _string(args, "url")
        mode = _choice(args, "as", FETCH_MODES, default="text")
        fetched = await self.fetch(url)
        host = urlsplit(fetched.url).hostname or ""
        if not (fetched.is_html or fetched.is_text):
            log.info("web.fetch %s %d %d bytes - not text", host, fetched.status, len(fetched.data))
            raise AppException(
                400, f"the response is {fetched.content_type or 'of an unknown type'}, not text"
            )
        page = decode_body(fetched)
        if fetched.is_html and mode != "raw":
            title, text, _links = html_to_text(page, fetched.url, markdown=mode == "markdown")
        else:
            title, text = "", page
        limit = self.sandbox.page_max_chars
        cut = len(text) > limit
        log.info(
            "web.fetch %s %d %d bytes%s",
            host,
            fetched.status,
            len(fetched.data),
            " (cut)" if fetched.truncated or cut else "",
        )
        return {
            "url": fetched.url,
            "status": fetched.status,
            "content_type": fetched.content_type,
            "title": title,
            "text": text[:limit],
            "bytes": len(fetched.data),
            "truncated": fetched.truncated or cut,
        }

    async def _robots(self, parts: SplitResult) -> urllib.robotparser.RobotFileParser | None:
        """The site's robots.txt, read through the same checks; a site without one allows all."""
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            fetched = await self.fetch(robots_url)
        except AppException as exc:
            log.info("web.crawl robots.txt of %s not read (%d)", parts.hostname, exc.status)
            return None
        if fetched.status != 200 or not (fetched.is_text or fetched.is_html):
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(decode_body(fetched).splitlines())
        return parser

    async def web_crawl(self, _headers: dict[str, str], body: Body) -> Any:
        args = _arguments(body, WEB_CRAWL_ROW)
        url = _string(args, "url")
        max_pages = _integer(
            args,
            "max_pages",
            default=CRAWL_PAGES_DEFAULT,
            floor=1,
            cap=self.sandbox.crawl_max_pages,
        )
        max_depth = _integer(
            args, "max_depth", default=CRAWL_DEPTH_DEFAULT, floor=0, cap=CRAWL_DEPTH_CAP
        )
        include = args.get("include")
        if include is not None and not isinstance(include, str):
            raise AppException(400, "include must be a string")
        if include and not include.startswith("/"):
            include = "/" + include
        start = self.sandbox.check_url(url)
        site = (start.scheme, (start.hostname or "").lower(), start.port)
        robots = await self._robots(start)
        start_url = urlunsplit(start)
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        seen = {start_url}
        pages: list[dict[str, Any]] = []
        visited = 0
        truncated = False
        budget = CRAWL_RESULT_MAX_CHARS
        while queue and len(pages) < max_pages:
            current, depth = queue.popleft()
            if robots is not None and not robots.can_fetch(ROBOTS_AGENT, current):
                continue
            if visited and self.sandbox.crawl_delay_ms:
                await asyncio.sleep(self.sandbox.crawl_delay_ms / 1000)
            try:
                fetched = await self.fetch(current)
            except AppException as exc:
                log.info("web.crawl %s skipped a page (%d)", site[1], exc.status)
                continue
            visited += 1
            if fetched.status != 200 or not (fetched.is_html or fetched.is_text):
                continue
            page = decode_body(fetched)
            if fetched.is_html:
                title, text, links = html_to_text(page, fetched.url)
            else:
                title, text, links = "", page, []
            if len(text) > self.sandbox.page_max_chars:
                text, truncated = text[: self.sandbox.page_max_chars], True
            if len(text) > budget:
                text, truncated = text[:budget], True
            budget -= len(text)
            pages.append(
                {"url": fetched.url, "title": title, "text": text, "bytes": len(fetched.data)}
            )
            if budget <= 0:
                truncated = True
                break
            if depth >= max_depth:
                continue
            for link in links:
                candidate = urlsplit(link)
                if (
                    candidate.scheme.lower(),
                    (candidate.hostname or "").lower(),
                    candidate.port,
                ) != site:
                    continue
                if include and not candidate.path.startswith(include):
                    continue
                normalized = urlunsplit(
                    (
                        candidate.scheme.lower(),
                        candidate.netloc,
                        candidate.path or "/",
                        candidate.query,
                        "",
                    )
                )
                if normalized not in seen:
                    seen.add(normalized)
                    queue.append((normalized, depth + 1))
        if queue and len(pages) >= max_pages:
            truncated = True
        log.info(
            "web.crawl %s 200 %d pages, %d fetched%s",
            site[1],
            len(pages),
            visited,
            " (cut)" if truncated else "",
        )
        return {"pages": pages, "visited": visited, "truncated": truncated}


# --- the clock ----------------------------------------------------------------------------------


async def time_now(_headers: dict[str, str], body: Body) -> Any:
    args = _arguments(body, TIME_NOW_ROW)
    name = _string(args, "timezone", default="UTC").strip() or "UTC"
    if name.upper() == "UTC":
        zone: Any = timezone.utc
        name = "UTC"
    else:
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AppException(
                400, f"{name} is not a time zone - an IANA name such as Europe/Berlin"
            ) from exc
    now = datetime.now(timezone.utc)
    local = now.astimezone(zone)
    offset = local.strftime("%z")
    return {
        "utc": iso_utc(now),
        "local": local.replace(microsecond=0).isoformat(),
        "timezone": name,
        "offset": f"{offset[:3]}:{offset[3:]}" if offset else "+00:00",
        "weekday": local.strftime("%A"),
        "epoch_ms": int(now.timestamp() * 1000),
    }


# --- the application: register what is configured, before the first request ------------------


class AgentTools:
    """The built-in tools of this process: the sandboxes read from the configuration at boot and
    the routes registered for the tools whose sandbox is set."""

    def __init__(self, registry: FunctionRegistry | None = None):
        self.registry = registry or default_registry
        self.files: FileTools | None = None
        self.web: WebTools | None = None
        self.registered: list[str] = []

    def _register(self, route: str, handler: Any) -> None:
        self.registry.register(route, handler, instances=INSTANCES)
        self.registered.append(route)

    async def boot(self) -> None:
        """Read the sandboxes and register the tools - inside the serving loop, before the first
        request; a configuration failure stops the application by name."""
        try:
            file_sandbox = load_file_sandbox()
            if file_sandbox is None:
                log.info("File tools not registered - %s is not set", FILE_ROOT)
            else:
                self.files = FileTools(file_sandbox)
                for route, handler in (
                    ("file.read", self.files.read),
                    ("file.write", self.files.write),
                    ("file.list", self.files.list),
                    ("file.search", self.files.search),
                ):
                    self._register(route, handler)
                log.info("File tools registered - workspace %s", file_sandbox.root)
            web_sandbox = load_web_sandbox()
            if web_sandbox is None:
                log.info("Web tools not registered - %s is empty", WEB_ALLOW)
            else:
                self.web = WebTools(web_sandbox)
                await self.web.open()
                self._register("web.fetch", self.web.web_fetch)
                self._register("web.crawl", self.web.web_crawl)
                log.info(
                    "Web tools registered - %d allowed site pattern(s)%s",
                    len(web_sandbox.rules),
                    ", private addresses allowed" if web_sandbox.allow_private else "",
                )
            if _bool_setting(TIME_ENABLED, True):
                self._register("time.now", time_now)
            else:
                log.info("time.now not registered - %s is false", TIME_ENABLED)
            log.info("Built-in tools ready - %s", ", ".join(self.registered) or "none registered")
        except ToolsError as exc:
            log.error("Built-in tools cannot start - %s", exc)
            await self.shutdown()
            raise SystemExit(1) from exc

    async def shutdown(self) -> None:
        if self.web is not None:
            await self.web.close()


agent_tools = AgentTools()


@preload(route="tools.health", instances=5, private=True)
async def tools_health(headers: dict[str, str], _body: Body) -> Any:
    """Health check speaking the engines' interface contract (type=info / type=health): which
    tools are registered and the sandboxes they run in."""
    if headers.get("type") == "info":
        files = agent_tools.files
        web = agent_tools.web
        return {
            "service": "agent.tools",
            "href": "http://127.0.0.1",
            "tools": list(agent_tools.registered),
            "workspace": str(files.root) if files is not None else None,
            "allowed_sites": [str(rule) for rule in web.sandbox.rules] if web is not None else [],
        }
    return "agent.tools is running fine"


# --- describe: the rows and what registers under this configuration ------------------------------


def _states() -> dict[str, str]:
    """For each route, whether it registers under the current configuration, and why not."""
    states: dict[str, str] = {}
    try:
        files = load_file_sandbox()
        file_state = (
            f"registers (workspace {files.root})"
            if files
            else f"NOT registered ({FILE_ROOT} is not set)"
        )
    except ToolsError as exc:
        file_state = f"NOT registered ({exc})"
    try:
        web = load_web_sandbox()
        web_state = (
            f"registers ({len(web.rules)} allowed site pattern(s))"
            if web
            else f"NOT registered ({WEB_ALLOW} is empty)"
        )
    except ToolsError as exc:
        web_state = f"NOT registered ({exc})"
    try:
        time_state = (
            "registers"
            if _bool_setting(TIME_ENABLED, True)
            else f"NOT registered ({TIME_ENABLED} is false)"
        )
    except ToolsError as exc:
        time_state = f"NOT registered ({exc})"
    for route in ROW_BY_ROUTE:
        group = route.split(".", 1)[0]
        states[route] = {"file": file_state, "web": web_state, "time": time_state}[group]
    return states


def describe(names: list[str]) -> int:
    """Print every catalog row (or the ones named, by name or route) as the JSON a catalog node
    takes, each with whether it registers under the current configuration."""
    wanted = [row for row in ROWS if not names or row["name"] in names or row["route"] in names]
    known = {row["name"] for row in ROWS} | set(ROW_BY_ROUTE)
    missing = sorted(set(names) - known)
    if missing:
        print(
            f"Unknown tool(s): {', '.join(missing)} - the tools are {', '.join(r['name'] for r in ROWS)}",
            file=sys.stderr,
        )
        return 1
    states = _states()
    print(
        f"# {len(wanted)} built-in tool(s) - the catalog rows a graph copies, and what registers here"
    )
    for row in wanted:
        print(f"\n## {row['route']} ({row['name']}) - {states[row['route']]}")
        print(row_json(row), end="")
    return 0


def _attach() -> None:
    platform.on_startup(agent_tools.boot)
    platform.on_cleanup(agent_tools.shutdown)


def _main(argv: list[str]) -> int:
    # -Dkey=value arguments belong to the configuration, which reads them from sys.argv itself
    words = [a for a in argv if not a.startswith("-D")]
    if words and words[0] == "describe":
        return describe(words[1:])
    _attach()
    platform.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
else:
    # loaded by mercury-serve: the tools register inside the serving loop, before the first request
    _attach()
