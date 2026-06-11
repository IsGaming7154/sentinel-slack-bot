import asyncio
import json

from sentinel import mcp_bridge


def parse_rows(result):
    """Extract the read_query result text and parse it into a list of row dicts."""
    texts = [b.text for b in result.content if getattr(b, "text", None)]
    raw = "\n".join(texts).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("rows") or data.get("result") or []
    return data if isinstance(data, list) else []


async def fetch_all_tickets():
    """Read every ticket via MCP (runs natively on the MCP loop)."""
    result = await mcp_bridge.client.call_tool(
        "read_query",
        {"query": "SELECT id, title, status, assignee FROM tickets ORDER BY id"},
    )
    return parse_rows(result)


def get_tickets():
    """Sync wrapper: fetch all tickets from a Slack worker thread."""
    return asyncio.run_coroutine_threadsafe(
        fetch_all_tickets(), mcp_bridge.loop
    ).result()


def resolve_ticket(ticket_id):
    """Sync wrapper: mark a ticket closed via MCP from a Slack worker thread."""

    async def _do():
        return await mcp_bridge.client.call_tool(
            "write_query",
            {
                "query": "UPDATE tickets SET status='closed' WHERE id={}".format(
                    int(ticket_id)
                )
            },
        )

    return asyncio.run_coroutine_threadsafe(_do(), mcp_bridge.loop).result()
