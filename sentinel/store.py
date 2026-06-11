"""Data access.

Two deliberate paths to the database:
- The LLM reaches data only through guarded MCP tools (see guard.py).
- The system (buttons, modals, RBAC, audit, approvals) uses direct,
  parameterized SQL below — no string concatenation anywhere.
"""

import asyncio
import json
import sqlite3

from sentinel import config, mcp_bridge


def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    """Create Sentinel's control-plane tables (idempotent)."""
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_roles (
                slack_user_id TEXT PRIMARY KEY,
                role TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                requested_by TEXT NOT NULL,
                channel TEXT,
                tool TEXT NOT NULL,
                arguments TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decided_by TEXT,
                decided_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                actor TEXT,
                provider TEXT,
                tool TEXT,
                query TEXT,
                decision TEXT,
                detail TEXT,
                latency_ms INTEGER
            )
            """
        )


# --- Tickets (MCP read path + parameterized write path) ------------------------

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
    """Mark a ticket closed. Structured UI action — parameterized, never via LLM."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET status='closed' WHERE id=?", (int(ticket_id),)
        )


def create_ticket(title, assignee, status="open"):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tickets (title, status, assignee) VALUES (?, ?, ?)",
            (title, status, assignee),
        )
        return cur.lastrowid


# --- Roles ----------------------------------------------------------------------

def get_role_row(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT role FROM user_roles WHERE slack_user_id=?", (user_id,)
        ).fetchone()
    return row["role"] if row else None


def set_role(user_id, role):
    with db() as conn:
        conn.execute(
            "INSERT INTO user_roles (slack_user_id, role) VALUES (?, ?) "
            "ON CONFLICT(slack_user_id) DO UPDATE SET role=excluded.role",
            (user_id, role),
        )


# --- Pending approvals -----------------------------------------------------------

def create_pending(requested_by, channel, tool, arguments):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO pending_actions (requested_by, channel, tool, arguments) "
            "VALUES (?, ?, ?, ?)",
            (requested_by, channel, tool, json.dumps(arguments)),
        )
        return cur.lastrowid


def get_pending(pending_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id=?", (int(pending_id),)
        ).fetchone()
    return dict(row) if row else None


def decide_pending(pending_id, status, decided_by):
    """Atomically settle a pending action. Returns False if already decided."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE pending_actions "
            "SET status=?, decided_by=?, decided_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='pending'",
            (status, decided_by, int(pending_id)),
        )
        return cur.rowcount == 1
