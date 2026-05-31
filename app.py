import asyncio
import json
import logging
import os
import re
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from anthropic import Anthropic
from google import genai
from google.genai import types

from mcp_client import AsyncMCPClient

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
GEMINI_MODEL = "gemini-2.5-flash"

BOTH_LLMS_DOWN = "Sorry, both Claude and Gemini are unavailable right now."

# --- Phase 4: proactive incident monitor config -------------------------------
INCIDENT_MONITOR = os.environ.get("INCIDENT_MONITOR") == "1"
POLL_INTERVAL_SECS = 15
WINDOW_MINUTES = 10
CASCADE_THRESHOLD = 3
ERROR_KEYWORDS = ["error", "timeout", "bug"]
ALERTS_CHANNEL = os.environ.get("ALERTS_CHANNEL", "#alerts")
COOLDOWN_SECS = 300

_alerted_ids = set()      # cumulative ticket IDs we've already alerted on (dedup)
_last_alert_time = 0.0    # time.monotonic() of last alert (cooldown)

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)

mcp_client = AsyncMCPClient()
mcp_loop = None
mcp_tools = []  # raw tool objects from session.list_tools()

anthropic_client = Anthropic()


# --- MCP tool execution bridge ------------------------------------------------

def run_tool(name, arguments):
    """Execute an MCP tool from a sync Slack thread via the background loop."""
    result = asyncio.run_coroutine_threadsafe(
        mcp_client.call_tool(name, arguments), mcp_loop
    ).result()
    texts = [block.text for block in result.content if getattr(block, "text", None)]
    return "\n".join(texts) if texts else str(result.content)


# --- Phase 5: App Home ticket data bridge -------------------------------------

async def fetch_all_tickets():
    """Read every ticket via MCP (runs natively on mcp_loop)."""
    result = await mcp_client.call_tool(
        "read_query",
        {"query": "SELECT id, title, status, assignee FROM tickets ORDER BY id"},
    )
    return _parse_rows(result)


def get_tickets():
    """Sync wrapper: fetch all tickets from a Slack worker thread."""
    return asyncio.run_coroutine_threadsafe(fetch_all_tickets(), mcp_loop).result()


def resolve_ticket(ticket_id):
    """Sync wrapper: mark a ticket closed via MCP from a Slack worker thread."""
    async def _do():
        return await mcp_client.call_tool(
            "write_query",
            {
                "query": "UPDATE tickets SET status='closed' WHERE id={}".format(
                    int(ticket_id)
                )
            },
        )

    return asyncio.run_coroutine_threadsafe(_do(), mcp_loop).result()


# --- Anthropic (primary) ------------------------------------------------------

def ask_claude(user_text):
    tools = [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]
    messages = [{"role": "user", "content": user_text}]

    while True:
        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                logger.info("[Claude] tool_use: %s %s", block.name, block.input)
                output = run_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
        messages.append({"role": "user", "content": tool_results})


# --- Gemini (fallback) --------------------------------------------------------

def _gemini_tools():
    declarations = [
        types.FunctionDeclaration(
            name=t.name,
            description=t.description or "",
            parameters_json_schema=t.inputSchema,
        )
        for t in mcp_tools
    ]
    return [types.Tool(function_declarations=declarations)]


def ask_gemini(user_text):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(tools=_gemini_tools())
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
    ]

    while True:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )

        if not response.function_calls:
            return response.text

        contents.append(response.candidates[0].content)
        response_parts = []
        for fc in response.function_calls:
            logger.info("[Gemini] function_call: %s %s", fc.name, dict(fc.args))
            output = run_tool(fc.name, dict(fc.args))
            response_parts.append(
                types.Part.from_function_response(
                    name=fc.name, response={"result": output}
                )
            )
        contents.append(types.Content(role="tool", parts=response_parts))


# --- Orchestration with fallback ----------------------------------------------

def generate_reply(user_text):
    try:
        return ask_claude(user_text)
    except Exception as primary_error:
        logger.warning(
            "Anthropic call failed (%s). Falling back to Gemini.", primary_error
        )
        try:
            return ask_gemini(user_text)
        except Exception:
            logger.exception("Gemini fallback also failed.")
            return BOTH_LLMS_DOWN


# --- Slack handlers -----------------------------------------------------------

def _respond(raw_text, say):
    user_text = re.sub(r"<@[^>]+>", "", raw_text or "").strip()
    if not user_text:
        return
    say(generate_reply(user_text))


@app.message("")
def handle_message(message, say):
    if message.get("subtype") or message.get("bot_id"):
        return
    _respond(message.get("text"), say)


@app.event("app_mention")
def handle_mention(event, say):
    _respond(event.get("text"), say)


@app.error
def global_error_handler(error, body, logger):
    logger.exception(error)
    logger.info(body)


# --- Phase 5: App Home dashboard ----------------------------------------------

STATUS_EMOJI = {"open": "🔴", "in_progress": "🟡", "closed": "✅"}
ACTIVE_STATUSES = {"open", "in_progress"}


def _ticket_card(ticket):
    status = (ticket.get("status") or "").lower()
    emoji = STATUS_EMOJI.get(status, "⚪")
    title = ticket.get("title", "(untitled)")
    section = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "{} *{}*\n_ID {} • {} • {}_".format(
                emoji, title, ticket.get("id"), ticket.get("assignee", "?"), status
            ),
        },
    }
    if status in ACTIVE_STATUSES:
        section["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Mark Resolved", "emoji": True},
            "style": "primary",
            "action_id": "mark_resolved",
            "value": str(ticket.get("id")),
        }
    return section


def build_home_view(tickets):
    active = sum(1 for t in tickets if (t.get("status") or "").lower() in ACTIVE_STATUSES)
    closed = len(tickets) - active

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Enterprise Agentic Control Panel", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Active:*\n{}".format(active)},
                {"type": "mrkdwn", "text": "*Closed:*\n{}".format(closed)},
            ],
        },
        {"type": "divider"},
    ]

    if not tickets:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No tickets found._"}}
        )
    else:
        for ticket in tickets:
            blocks.append(_ticket_card(ticket))
            blocks.append({"type": "divider"})

    return {"type": "home", "blocks": blocks}


def _error_view(message):
    return {
        "type": "home",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":warning: {}".format(message)},
            }
        ],
    }


@app.event("app_home_opened")
def handle_home_opened(event, client):
    user_id = event["user"]
    try:
        tickets = get_tickets()
        client.views_publish(user_id=user_id, view=build_home_view(tickets))
    except Exception:
        logger.exception("Failed to render App Home for %s.", user_id)
        client.views_publish(
            user_id=user_id, view=_error_view("Could not load tickets right now.")
        )


@app.action("mark_resolved")
def handle_mark_resolved(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    try:
        ticket_id = body["actions"][0]["value"]
        resolve_ticket(ticket_id)
        tickets = get_tickets()
        client.views_publish(user_id=user_id, view=build_home_view(tickets))
    except Exception:
        logger.exception("Failed to resolve ticket for %s.", user_id)
        client.views_publish(
            user_id=user_id, view=_error_view("Could not update the ticket.")
        )


# --- Phase 4: proactive incident monitor --------------------------------------

def _parse_rows(result):
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


async def poll_recent_error_tickets():
    """Fetch error-keyword tickets created inside the recent time window via MCP.

    Runs as a native coroutine on mcp_loop, so the MCP session is only ever
    touched from its own thread.
    """
    like = " OR ".join("lower(title) LIKE '%{}%'".format(k) for k in ERROR_KEYWORDS)
    query = (
        "SELECT id, title FROM tickets "
        "WHERE created_at >= datetime('now', '-{} minutes') AND ({}) "
        "ORDER BY id".format(WINDOW_MINUTES, like)
    )
    result = await mcp_client.call_tool("read_query", {"query": query})
    return _parse_rows(result)


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
        text = generate_reply(prompt)
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


async def incident_monitor():
    """Background loop: poll for an error cascade and proactively alert Slack.

    Lives on mcp_loop. DB reads are awaited directly; the blocking LLM call and
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


# --- Startup ------------------------------------------------------------------

def start_mcp_client():
    """Run the MCP client on its own asyncio loop in a background thread.

    The loop stays alive for the process lifetime so the stdio session remains
    open and usable from the Slack handler threads (via run_coroutine_threadsafe).
    """
    global mcp_loop, mcp_tools
    mcp_loop = asyncio.new_event_loop()

    def _run_loop():
        asyncio.set_event_loop(mcp_loop)
        mcp_loop.run_forever()

    threading.Thread(target=_run_loop, name="mcp-loop", daemon=True).start()

    asyncio.run_coroutine_threadsafe(mcp_client.connect(), mcp_loop).result()
    mcp_tools = asyncio.run_coroutine_threadsafe(
        mcp_client.list_tools(), mcp_loop
    ).result()
    logger.info("MCP connected. Available tools: %s", [t.name for t in mcp_tools])


if __name__ == "__main__":
    start_mcp_client()
    if INCIDENT_MONITOR:
        asyncio.run_coroutine_threadsafe(incident_monitor(), mcp_loop)
    else:
        logger.info("Incident monitor disabled (set INCIDENT_MONITOR=1 to enable).")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
