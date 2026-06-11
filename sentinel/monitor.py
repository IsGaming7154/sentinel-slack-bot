import asyncio
import logging
import time

from sentinel import mcp_bridge
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
    like = " OR ".join("lower(title) LIKE '%{}%'".format(k) for k in ERROR_KEYWORDS)
    query = (
        "SELECT id, title FROM tickets "
        "WHERE created_at >= datetime('now', '-{} minutes') AND ({}) "
        "ORDER BY id".format(WINDOW_MINUTES, like)
    )
    result = await mcp_bridge.client.call_tool("read_query", {"query": query})
    return parse_rows(result)


def build_incident_alert(rows):
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
        text = generate_reply(prompt, GuardContext(user_id="system:incident-monitor"))
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
            alert = await asyncio.to_thread(build_incident_alert, rows)
            await asyncio.to_thread(
                app.client.chat_postMessage, channel=ALERTS_CHANNEL, text=alert
            )
            _alerted_ids |= error_ids
            _last_alert_time = now
            logger.info("Incident alert posted to %s.", ALERTS_CHANNEL)
        except Exception:
            logger.exception("incident_monitor cycle failed.")
