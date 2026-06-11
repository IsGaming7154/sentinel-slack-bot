"""Audit trail: every guarded tool call and approval decision lands here."""

import logging

from sentinel import store

logger = logging.getLogger(__name__)


def record(actor, provider, tool, query, decision, detail="", latency_ms=None):
    try:
        with store.db() as conn:
            conn.execute(
                "INSERT INTO audit_log "
                "(actor, provider, tool, query, decision, detail, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (actor, provider, tool, query, decision, detail, latency_ms),
            )
    except Exception:
        logger.exception("Failed to write audit record.")


def recent(limit=8):
    with store.db() as conn:
        rows = conn.execute(
            "SELECT ts, actor, provider, tool, query, decision, detail "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]
