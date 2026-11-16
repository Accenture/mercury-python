"""Shared fixtures.

``serve`` runs the Event API host of a registry on an ephemeral port through aiohttp's runner
and yields its base URL - the host the client and server tests talk to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from aiohttp import web

from mercury_composable import FunctionRegistry
from mercury_composable.server import EventApiServer


@asynccontextmanager
async def _serve(registry: FunctionRegistry) -> AsyncIterator[str]:
    runner = web.AppRunner(EventApiServer(registry).create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


@pytest.fixture
def serve() -> Callable[[FunctionRegistry], AbstractAsyncContextManager[str]]:
    """The Event API host of a registry, as an async context manager yielding its base URL."""
    return _serve
