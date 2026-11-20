"""Static content pins: the engines' ``static.html.folder``, served by the host for the paths
nothing else claims - the folder's index.html at /, the types, the ETag, what is never found, and
what wins over a file."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

from mercury_composable import AppConfig, FunctionRegistry, app_config, load_config
from mercury_composable.server import EventApiServer
from mercury_composable.static_content import (
    OCTET_STREAM,
    StaticContent,
    content_type,
    resolve_folder,
)

Serve = Callable[[FunctionRegistry], AbstractAsyncContextManager[str]]
NOT_FOUND = {"status": 404, "message": "Resource not found", "type": "error"}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def bundle(public: Path, outside: Path) -> None:
    """A web bundle the way a build deploys one, plus the entries that must never be served."""
    write(public / "index.html", "<h1>bundle</h1>")
    write(public / "app.js", "console.log('bundle')")
    write(public / "style.css", "body { margin: 0 }")
    write(public / "assets" / "logo.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    write(public / "sub" / "index.html", "<h1>sub</h1>")
    write(public / "plain" / "readme.txt", "a folder without an index page")
    write(public / ".secret", "hidden")
    write(public / "info", "a file named like the actuator's path")
    write(public / "hello", "a file named like an extension's route")
    write(outside, "outside the folder")
    os.symlink(outside, public / "link.txt")


@pytest.fixture
def app_dir(tmp_path: Path) -> Iterator[Path]:
    """An application folder whose resources/public holds a bundle, loaded as the shared
    configuration (the host resolves the folder from it at construction); a fresh default
    configuration afterwards."""
    folder = tmp_path / "app"
    bundle(folder / "resources" / "public", tmp_path / "outside.txt")
    load_config(app_dir=str(folder))
    yield folder
    load_config()


async def fetch(url: str | URL, method: str = "GET",
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, url, headers=headers) as response,
    ):
        return (response.status, {k.lower(): v for k, v in response.headers.items()},
                await response.read())


async def fetch_json(url: str | URL) -> tuple[int, object]:
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.json(content_type=None)


@asynccontextmanager
async def serve_with_routes(registry: FunctionRegistry,
                            mount: Callable[[web.Application], None]) -> AsyncIterator[str]:
    runner = web.AppRunner(EventApiServer(registry).create_app([mount]))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()
        await registry.bus.close()


# --- the table and the folder -----------------------------------------------------------------


def test_the_content_type_follows_the_engines_table():
    assert content_type("index.html") == "text/html; charset=utf-8"
    assert content_type("app.JS") == "text/javascript; charset=utf-8"
    assert content_type("bundle.mjs") == "text/javascript; charset=utf-8"
    assert content_type("style.css") == "text/css; charset=utf-8"
    assert content_type("logo.svg") == "image/svg+xml"
    assert content_type("favicon.ico") == "image/vnd.microsoft.icon"
    assert content_type("font.woff2") == "font/woff2"
    assert content_type("app.js.map") == "application/json; charset=utf-8"
    # the engine's rule: no dot, a trailing dot or an unknown extension is an octet stream
    assert content_type("README") == OCTET_STREAM
    assert content_type("trailing.") == OCTET_STREAM
    assert content_type("archive.tgz") == OCTET_STREAM


def test_the_folder_is_resolved_under_the_application_file(tmp_path: Path):
    config = AppConfig(argv=[], app_dir=str(tmp_path))
    assert resolve_folder(config) == str(tmp_path / "resources" / "public")
    config.set("static.html.folder", "web/site")
    assert resolve_folder(config) == str(tmp_path / "web" / "site")
    config.set("static.html.folder", f"file:{tmp_path / 'absolute'}")
    assert resolve_folder(config) == str(tmp_path / "absolute")
    config.set("static.html.folder", str(tmp_path / "elsewhere") + "/")
    assert resolve_folder(config) == str(tmp_path / "elsewhere")
    # a classpath has no meaning in a pack - the default stands
    config.set("static.html.folder", "classpath:/public")
    assert resolve_folder(config) == str(tmp_path / "resources" / "public")
    # an empty value turns the service off
    config.set("static.html.folder", "")
    assert resolve_folder(config) is None


def test_a_missing_folder_serves_nothing(tmp_path: Path):
    assert not StaticContent(str(tmp_path / "absent")).enabled
    assert not StaticContent(None).enabled
    assert StaticContent(None).locate("/index.html") is None


# --- through the host ---------------------------------------------------------------------------


async def test_the_folders_index_page_replaces_the_landing_page(app_dir: Path, serve: Serve):
    async with serve(FunctionRegistry()) as base:
        status, headers, body = await fetch(f"{base}/")
        assert status == 200
        assert body == b"<h1>bundle</h1>"
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert headers["cache-control"] == "no-cache"
        assert headers["etag"]
        # HEAD: the same headers, no body
        status, headers, body = await fetch(f"{base}/", "HEAD")
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert body == b""


async def test_a_file_is_served_with_its_type_and_an_etag(app_dir: Path, serve: Serve):
    async with serve(FunctionRegistry()) as base:
        status, headers, body = await fetch(f"{base}/app.js")
        assert (status, body) == (200, b"console.log('bundle')")
        assert headers["content-type"] == "text/javascript; charset=utf-8"
        assert "cache-control" not in headers  # a browser revalidates by the ETag
        etag = headers["etag"]
        status, _, body = await fetch(f"{base}/app.js", headers={"If-None-Match": etag})
        assert (status, body) == (304, b"")
        status, headers, _ = await fetch(f"{base}/style.css")
        assert (status, headers["content-type"]) == (200, "text/css; charset=utf-8")
        status, headers, _ = await fetch(f"{base}/assets/logo.svg")
        assert (status, headers["content-type"]) == (200, "image/svg+xml")


async def test_a_folder_serves_its_index_page(app_dir: Path, serve: Serve):
    async with serve(FunctionRegistry()) as base:
        for path in ("/sub", "/sub/"):
            status, _, body = await fetch(f"{base}{path}")
            assert (status, body) == (200, b"<h1>sub</h1>"), path
        # a folder without one is not found, in the engines' error shape
        assert await fetch_json(f"{base}/plain") == (404, NOT_FOUND)


async def test_what_is_never_found(app_dir: Path, serve: Serve):
    async with serve(FunctionRegistry()) as base:
        for path in (
            "/missing.js",                       # no such file
            "/.secret",                          # a hidden entry
            "/link.txt",                         # a symbolic link out of the folder
            "/%2e%2e/outside.txt",               # a traversal, percent-encoded
            "/sub/%2e%2e/%2e%2e/outside.txt",
            "/assets/",                          # a folder without an index page
        ):
            status, payload = await fetch_json(URL(f"{base}{path}", encoded=True))
            assert (status, payload) == (404, NOT_FOUND), path
        # the folder itself is never listed, and another method is not a file request
        status, payload = await fetch_json(f"{base}/assets")
        assert (status, payload) == (404, NOT_FOUND)
        status, _, body = await fetch(f"{base}/app.js", "POST")
        assert status == 404
        assert b"Resource not found" in body


async def test_the_hosts_paths_and_an_extensions_routes_win_over_a_file(app_dir: Path):
    def mount(app: web.Application) -> None:
        async def hello(_request: web.Request) -> web.Response:
            return web.Response(text="the route")
        app.router.add_get("/hello", hello)

    async with serve_with_routes(FunctionRegistry(), mount) as base:
        status, payload = await fetch_json(f"{base}/info")
        assert status == 200
        assert isinstance(payload, dict) and "app" in payload  # the actuator, not public/info
        status, _, body = await fetch(f"{base}/hello")
        assert (status, body) == (200, b"the route")  # the extension, not public/hello


async def test_the_key_names_another_folder(tmp_path: Path, serve: Serve):
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "index.html", "<h1>elsewhere</h1>")
    load_config(app_dir=str(tmp_path / "app"))
    try:
        app_config().set("static.html.folder", str(elsewhere))
        async with serve(FunctionRegistry()) as base:
            status, _, body = await fetch(f"{base}/")
            assert (status, body) == (200, b"<h1>elsewhere</h1>")
    finally:
        load_config()


async def test_without_a_folder_the_landing_page_stays(tmp_path: Path, serve: Serve):
    load_config(app_dir=str(tmp_path / "app"))  # no resources/public
    try:
        async with serve(FunctionRegistry()) as base:
            status, headers, body = await fetch(f"{base}/")
            assert status == 200
            assert headers["content-type"].startswith("text/html")
            assert b"INFO endpoint" in body
            assert await fetch_json(f"{base}/app.js") == (404, NOT_FOUND)
    finally:
        load_config()
