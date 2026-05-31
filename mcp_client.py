import logging
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class AsyncMCPClient:
    """Async client that connects to the SQLite MCP server over stdio."""

    def __init__(self, db_path="data.db"):
        self.server_params = StdioServerParameters(
            command="npx",
            args=["-y", "mcp-server-sqlite-npx", db_path],
        )
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
