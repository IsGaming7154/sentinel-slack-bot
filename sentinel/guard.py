"""Sentinel's tool firewall.

Every tool call the LLM makes passes through here. Reads are validated and
executed; writes are never executed directly — they're queued for human
approval; anything else is denied by default.
"""

import logging
import re
import time

from sentinel import audit, mcp_bridge, store

logger = logging.getLogger(__name__)

ALLOW = "allow"
QUEUE = "queue"
BLOCK = "block"

READ_TOOLS = {"read_query", "list_tables", "describe_table"}
WRITE_TOOLS = {"write_query", "create_table"}

_FORBIDDEN = re.compile(
    r"\b(pragma|attach|detach|vacuum|reindex|insert|update|delete|replace|"
    r"drop|alter|create|trigger)\b",
    re.IGNORECASE,
)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class GuardContext:
    """Who is asking, where to post approval cards, and which model is acting."""

    def __init__(self, user_id, channel=None, client=None, provider="?"):
        self.user_id = user_id
        self.channel = channel
        self.client = client
        self.provider = provider


def validate_read_query(query):
    """Return (ok, reason). A read must be exactly one SELECT statement."""
    if not query or not query.strip():
        return False, "empty query"

    stripped = _STRING_LITERAL.sub("''", query)

    if "--" in stripped or "/*" in stripped:
        return False, "SQL comments are not allowed"

    body = stripped.strip().rstrip(";").strip()
    if ";" in body:
        return False, "multiple statements are not allowed"

    first_word = body.split(None, 1)[0].lower() if body else ""
    if first_word not in ("select", "with"):
        return False, "only SELECT statements are allowed on the read path"

    match = _FORBIDDEN.search(body)
    if match:
        return False, "forbidden keyword: {}".format(match.group(1).upper())

    return True, "single SELECT statement"


def evaluate(tool, arguments):
    """Classify a tool call: ALLOW, QUEUE (human approval), or BLOCK."""
    if tool == "read_query":
        ok, reason = validate_read_query((arguments or {}).get("query", ""))
        return (ALLOW, reason) if ok else (BLOCK, reason)
    if tool in READ_TOOLS:
        return ALLOW, "read-only tool"
    if tool in WRITE_TOOLS:
        return QUEUE, "write operations require human approval"
    return BLOCK, "tool '{}' is not on the allowlist".format(tool)


def _query_of(arguments):
    return (arguments or {}).get("query") or str(arguments)


def execute(tool, arguments, ctx):
    """Run a tool call through the firewall and return text for the LLM."""
    decision, reason = evaluate(tool, arguments)
    query = _query_of(arguments)

    if decision == ALLOW:
        start = time.perf_counter()
        output = mcp_bridge.call_tool_sync(tool, arguments)
        audit.record(
            actor=ctx.user_id,
            provider=ctx.provider,
            tool=tool,
            query=query,
            decision="allowed",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return output

    if decision == QUEUE:
        pending_id = store.create_pending(ctx.user_id, ctx.channel, tool, arguments)
        audit.record(
            actor=ctx.user_id,
            provider=ctx.provider,
            tool=tool,
            query=query,
            decision="queued",
            detail="approval #{}".format(pending_id),
        )
        _post_approval_card(ctx, pending_id, tool, query)
        logger.info("Write queued for approval #%s: %s", pending_id, query)
        return (
            "SENTINEL GUARD: this write operation was NOT executed. It requires human "
            "approval and was queued as approval request #{}. An approval card has been "
            "posted; an admin must click Approve before anything changes. Tell the user "
            "exactly that — do not claim the operation succeeded.".format(pending_id)
        )

    audit.record(
        actor=ctx.user_id,
        provider=ctx.provider,
        tool=tool,
        query=query,
        decision="blocked",
        detail=reason,
    )
    logger.warning("Guard blocked %s (%s): %s", tool, reason, query)
    return (
        "SENTINEL GUARD: this operation was blocked ({}) and was not executed. "
        "Tell the user it was denied by the security policy.".format(reason)
    )


def _post_approval_card(ctx, pending_id, tool, query):
    if not (ctx.client and ctx.channel):
        return
    from sentinel.handlers.approvals import build_approval_blocks

    try:
        ctx.client.chat_postMessage(
            channel=ctx.channel,
            text="Sentinel: approval required for a write operation.",
            blocks=build_approval_blocks(pending_id, ctx.user_id, tool, query),
        )
    except Exception:
        logger.exception("Failed to post approval card #%s.", pending_id)
