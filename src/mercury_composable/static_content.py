"""Static web content: the engines' ``static.html.folder``, served by the host.

The Java engine serves the files of ``static.html.folder`` (default ``classpath:/public``) for a GET
no REST endpoint claims - ``index.html`` for a folder, a content type by extension, an ETag so a
browser revalidates - and the Rust engine serves a fixed ``resources/public``. The packs serve the
same way: the key is the Java engine's and its default the Rust engine's location, ``resources/public``
next to the application file, under the packs' resources rule (a relative folder is under the
application file's folder, never the working directory; ``file:`` is the engine's prefix for an
absolute path). The host's own paths and an extension's routes win over a file, a file wins over the
catch-all, and a folder's ``index.html`` replaces the embedded landing page at ``/``.

Served: GET and HEAD of a regular file inside the folder, ``index.html`` for a folder. Not found: a
path with a hidden segment (one starting with ``.``, so ``..`` too), a symbolic link that leaves the
folder, anything that is not a regular file. ``text/html`` is served ``Cache-Control: no-cache`` so a
redeployed page is revalidated on every load; every file carries an ETag and ``If-None-Match`` answers
304, so a browser keeps the rest and asks only whether it changed. Not ported: the Java engine's
static-content filter, its ``no-cache-pages`` list and its ``mime.types`` section - the table here
covers what a web bundle ships; anything else is ``application/octet-stream``.
"""

from __future__ import annotations

import os

from aiohttp import web

from .config import AppConfig
from .log import get_logger

log = get_logger("mercury.static")

STATIC_FOLDER = "static.html.folder"
DEFAULT_FOLDER = "resources/public"
INDEX_HTML = "index.html"
FILE_PREFIX = "file:"
CLASSPATH_PREFIX = "classpath:"
OCTET_STREAM = "application/octet-stream"

# the engines' minimalist table (platform-core's built-in types and its mime-types.yml) plus what a
# web bundle ships; the text types carry their charset, as the host's own responses do
CONTENT_TYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "map": "application/json; charset=utf-8",
    "webmanifest": "application/manifest+json",
    "txt": "text/plain; charset=utf-8",
    "xml": "application/xml",
    "svg": "image/svg+xml",
    "svgz": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "jpe": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/vnd.microsoft.icon",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "ttc": "font/collection",
    "pdf": "application/pdf",
    "wasm": "application/wasm",
}


def content_type(name: str) -> str:
    """The content type of a file name, by its extension - the engine's rule: a name without a
    dot, or ending in one, is ``application/octet-stream``, as is an extension the table lacks."""
    stem, dot, ext = name.rpartition(".")
    if dot and stem and ext:
        return CONTENT_TYPES.get(ext.lower(), OCTET_STREAM)
    return OCTET_STREAM


def resolve_folder(config: AppConfig) -> str | None:
    """The folder ``static.html.folder`` names, absolute, or None when there is nothing to serve:
    an empty value turns the service off; a relative value is under the application file's folder
    (none known - an interactive session - serves nothing); ``file:`` is stripped; ``classpath:``
    has no meaning in a pack, so the default stands and a warning names the value."""
    value = (config.get_property(STATIC_FOLDER, DEFAULT_FOLDER) or "").strip()
    if not value:
        return None
    if value.startswith(CLASSPATH_PREFIX):
        log.warning("%s '%s' names a classpath, which a pack does not have - serving %s",
                    STATIC_FOLDER, value, DEFAULT_FOLDER)
        value = DEFAULT_FOLDER
    elif value.startswith(FILE_PREFIX):
        value = value[len(FILE_PREFIX):]
    if not os.path.isabs(value):
        if config.app_dir is None:
            return None
        value = os.path.join(config.app_dir, value)
    return os.path.normpath(value)


class StaticContent:
    """The files of one folder, served by the host for the paths nothing else claims."""

    def __init__(self, folder: str | None) -> None:
        self.folder: str | None = folder if folder and os.path.isdir(folder) else None
        self._root = os.path.realpath(self.folder) if self.folder else None
        if self.folder:
            log.info("Static content folder: %s", self.folder)

    @property
    def enabled(self) -> bool:
        return self._root is not None

    def locate(self, path: str) -> str | None:
        """The regular file a request path names inside the folder (``index.html`` for a folder),
        resolved, or None: a hidden segment, a link out of the folder, a missing or special file."""
        root = self._root
        if root is None:
            return None
        segments = [segment for segment in path.split("/") if segment]
        if any(segment.startswith(".") for segment in segments):
            return None
        try:
            target = os.path.realpath(os.path.join(root, *segments)) if segments else root
            if os.path.isdir(target):
                target = os.path.realpath(os.path.join(target, INDEX_HTML))
            if not target.startswith(root + os.sep) or not os.path.isfile(target):
                return None
            return target
        except OSError:
            return None

    def response(self, file: str) -> web.FileResponse:
        """The file with its type and its caching: html ``no-cache``, everything with an ETag
        (aiohttp's FileResponse answers If-None-Match with 304 and serves HEAD without a body)."""
        kind = content_type(os.path.basename(file))
        headers = {"Content-Type": kind}
        if kind.startswith("text/html"):
            headers["Cache-Control"] = "no-cache"
        return web.FileResponse(file, headers=headers)

    def serve(self, request: web.Request) -> web.StreamResponse | None:
        """The response for a GET or HEAD that names a file, or None when the path is not ours."""
        if request.method not in ("GET", "HEAD"):
            return None
        file = self.locate(request.path)
        return self.response(file) if file else None
