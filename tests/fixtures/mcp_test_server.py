"""A small MCP server on the official SDK, for the MCP binding's tests.

    python tests/fixtures/mcp_test_server.py              # stdio (the default transport)
    python tests/fixtures/mcp_test_server.py http 8765    # streamable HTTP on 127.0.0.1:8765/mcp

Five tools cover the result shapes the binding maps: ``echo`` answers text (the SDK would otherwise
wrap a plain return as structured content), ``add`` answers structured content, ``fail`` reports a tool error, ``slow`` answers after a delay (a timeout
test), and ``env_present`` says whether a variable reached the subprocess's environment -
its presence only, never its value.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("fixture", version="1.0.0")


@server.tool(description="Echo the text back.", structured_output=False)
def echo(text: str) -> str:
    return text


@server.tool(description="Add two whole numbers.", structured_output=True)
def add(a: int, b: int) -> dict[str, int]:
    return {"sum": a + b}


@server.tool(description="Fail with the reason given.", structured_output=False)
def fail(reason: str) -> str:
    raise ToolError(reason)


@server.tool(description="Answer after a delay, in seconds.", structured_output=False)
async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


@server.tool(description="Whether an environment variable reached this server.", structured_output=True)
def env_present(name: str) -> dict[str, bool]:
    return {"present": name in os.environ}


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "http":
        server.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run()
