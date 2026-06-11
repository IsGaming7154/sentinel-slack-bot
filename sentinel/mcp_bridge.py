import asyncio
import logging
import threading
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sentinel.config import DB_PATH

logger = logging.getLogger(__name__)

# Supply-chain hardening: the SQLite MCP server is pinned in package.json and
# verified by package-lock.json. We spawn the locally installed copy with node;
# we never pull "latest" off the registry at runtime.
PINNED_SERVER_VERSION = "0.8.0"
_LOCAL_SERVER_JS = (
    Path(__file__).resolve().parent.parent
    / "node_modules" / "mcp-server-sqlite-npx" / "dist" / "index.js"
)


def _server_params(db_path):
    if _LOCAL_SERVER_JS.exists():
        return StdioServerParameters(
            command="node", args=[str(_LOCAL_SERVER_JS), db_path]
        )
    logger.warning(
        "Local MCP server not found (run `npm ci`). Falling back to pinned npx."
    )
    return StdioServerParameters(
        command="npx",
        args=["-y", "mcp-server-sqlite-npx@{}".format(PINNED_SERVER_VERSION), db_path],
    )


class AsyncMCPClient:
    """Async client that connects to the SQLite MCP server over stdio."""

    def __init__(self, db_path=DB_PATH):
        self.server_params = _server_params(db_path)
        self.session = None
        self._exit_stack = AsyncExitStack()

    async def connect(self):
        """Spawn the MCP server, open a session, and initialize it."""
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(self.server_params)
        )
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()
        logger.info("MCP session initialized.")
        return self.session

    async def list_tools(self):
        """Return the tools exposed by the MCP server."""
        if self.session is None:
            raise RuntimeError("Client not connected. Call connect() first.")
        response = await self.session.list_tools()
        return response.tools

    async def call_tool(self, name, arguments):
        """Invoke a tool on the MCP server and return its result."""
        if self.session is None:
            raise RuntimeError("Client not connected. Call connect() first.")
        return await self.session.call_tool(name, arguments)

    async def close(self):
        """Tear down the session and stop the MCP server subprocess."""
        await self._exit_stack.aclose()
        self.session = None


client = AsyncMCPClient()
loop = None
tools = []  # raw tool objects from session.list_tools()


def start():
    """Run the MCP client on its own asyncio loop in a background thread.

    The loop stays alive for the process lifetime so the stdio session remains
    open and usable from the Slack handler threads (via run_coroutine_threadsafe).
    """
    global loop, tools
    loop = asyncio.new_event_loop()

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, name="mcp-loop", daemon=True).start()

    asyncio.run_coroutine_threadsafe(client.connect(), loop).result()
    tools = asyncio.run_coroutine_threadsafe(client.list_tools(), loop).result()
    logger.info("MCP connected. Available tools: %s", [t.name for t in tools])


def call_tool_sync(name, arguments):
    """Execute an MCP tool from a sync Slack thread via the background loop."""
    result = asyncio.run_coroutine_threadsafe(
        client.call_tool(name, arguments), loop
    ).result()
    texts = [block.text for block in result.content if getattr(block, "text", None)]
    return "\n".join(texts) if texts else str(result.content)
