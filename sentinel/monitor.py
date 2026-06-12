import asyncio
import logging
import re
import time

from sentinel import audit, guard, mcp_bridge
from sentinel.config import (
    ALERTS_CHANNEL,
    BOTH_LLMS_DOWN,
    CASCADE_THRESHOLD,
    COOLDOWN_SECS,
    ERROR_KEYWORDS,
    POLL_INTERVAL_SECS,
    WINDOW_MINUTES,
)
from sentinel.guard import GuardContext
from sentinel.llm.router import generate_reply
from sentinel.store import parse_rows

logger = logging.getLogger(__name__)

_alerted_ids = set()      # cumulative ticket IDs we've already alerted on (dedup)
_last_alert_time = 0.0    # time.monotonic() of last alert (cooldown)


async def poll_recent_error_tickets():
    """Fetch error-keyword tickets created inside the recent time window via MCP.

    Runs as a native coroutine on the MCP loop, so the MCP session is only ever
    touched from its own thread.
    """
    # The MCP read_query tool takes a single SQL string (no parameter binding),
    # so interpolated values must be restricted to safe characters.
    keywords = [
        k.lower() for k in ERROR_KEYWORDS if re.fullmatch(r"[a-z0-9 _-]+", k.lower())
    ]
    if len(keywords) != len(ERROR_KEYWORDS):
        logger.error("Skipping ERROR_KEYWORDS with unsafe SQL characters.")
    if not keywords:
        return []
    like = " OR ".join("lower(title) LIKE '%{}%'".format(k) for k in keywords)
    query = (
        "SELECT id, title FROM tickets "
        "WHERE created_at >= datetime('now', '-{} minutes') AND ({}) "
        "ORDER BY id".format(int(WINDOW_MINUTES), like)
    )
    # Even our own system queries pass the guard's read validation. We can't go
    # through guard.execute here (it blocks on this very event loop), so we use
    # the same validator directly.
    ok, reason = guard.validate_read_query(query)
    if not ok:
        logger.error("Monitor query failed guard validation (%s); skipping.", reason)
        return []
    result = await mcp_bridge.client.call_tool("read_query", {"query": query})
    return parse_rows(result)


def build_incident_alert(rows, client=None):
    """Ask the LLM (Claude->Gemini) for a root-cause alert; fall back to a template."""
    titles = [r.get("title", "") for r in rows]
    bullets = "\n".join("• {}".format(t) for t in titles)
    prompt = (
        "You are an SRE incident assistant. The monitoring system detected a cascade of "
        "{} error tickets created within the last {} minutes:\n{}\n\n"
        "Write a concise High-Priority Incident Alert for a Slack channel. Include a one-line "
        "summary, the single most likely root cause based on these titles, and 2-3 recommended "
        "next steps. Keep it under 120 words.".format(len(rows), WINDOW_MINUTES, bullets)
    )
    try:
        # Channel + client so that if the LLM ever queues a write here, the
        # approval card lands in the alerts channel instead of being orphaned.
        ctx = GuardContext(
            user_id="system:incident-monitor", channel=ALERTS_CHANNEL, client=client
        )
        text = generate_reply(prompt, ctx)
    except Exception:
        logger.exception("LLM alert generation failed.")
        text = ""

    if not text or text.strip() == BOTH_LLMS_DOWN:
        text = (
            "*Root cause (auto-generated, LLM unavailable):* {} tickets mentioning {} were "
            "created within {} minutes — likely a shared dependency timing out "
            "(gateway / database / payment). Investigate recent deploys and upstream service "
            "health.\n\n{}".format(
                len(rows), "/".join(ERROR_KEYWORDS), WINDOW_MINUTES, bullets
            )
        )
    return ":rotating_light: *High-Priority Incident Alert* :rotating_light:\n" + text


async def incident_monitor(app):
    """Background loop: poll for an error cascade and proactively alert Slack.

    Lives on the MCP loop. DB reads are awaited directly; the blocking LLM call and
    the blocking Slack post are offloaded with asyncio.to_thread so the loop stays
    free to service concurrent Slack-handler tool calls.
    """
    global _alerted_ids, _last_alert_time
    logger.info(
        "Incident monitor started (every %ss, window %smin, threshold %s, channel %s).",
        POLL_INTERVAL_SECS, WINDOW_MINUTES, CASCADE_THRESHOLD, ALERTS_CHANNEL,
    )
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECS)
            rows = await poll_recent_error_tickets()
            if len(rows) < CASCADE_THRESHOLD:
                continue

            error_ids = {r.get("id") for r in rows if r.get("id") is not None}
            now = time.monotonic()
            new_ids = error_ids - _alerted_ids
            if not new_ids or (now - _last_alert_time) < COOLDOWN_SECS:
                continue

            logger.info("Cascade detected: %s error tickets in window. Alerting.", len(rows))
            alert = await asyncio.to_thread(build_incident_alert, rows, app.client)
            await asyncio.to_thread(
                app.client.chat_postMessage, channel=ALERTS_CHANNEL, text=alert
            )
            _alerted_ids |= error_ids
            _last_alert_time = now
            await asyncio.to_thread(
                audit.record,
                actor="system:incident-monitor",
                provider="system",
                tool="incident_alert",
                query="{} error tickets in {}-minute window".format(
                    len(rows), WINDOW_MINUTES
                ),
                decision="allowed",
                detail="alert posted to {}".format(ALERTS_CHANNEL),
            )
            logger.info("Incident alert posted to %s.", ALERTS_CHANNEL)
        except Exception:
            logger.exception("incident_monitor cycle failed.")
