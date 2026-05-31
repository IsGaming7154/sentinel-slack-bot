# 🤖 Sentinel — A Resilient Agentic Slack Bot

> An agentic Slack assistant that turns plain-English questions into real database actions using the **Model Context Protocol (MCP)** — with a built-in **Anthropic → Gemini multi-model failover** so it keeps answering even when a model provider goes down.

---

## 💡 Inspiration

Agentic bots are only as reliable as the single model they're wired to. The moment that provider rate-limits you, has an outage, or rejects a request, your "intelligent" assistant goes dark — usually in front of the exact people you were trying to impress.

We wanted to build an agent that treats the LLM as a **swappable, fault-tolerant component** rather than a single point of failure. Pair that with MCP — the emerging open standard for giving models real tools — and you get an assistant that is both *capable* (it can actually query live data) and *resilient* (it degrades gracefully instead of dying).

The result is **Sentinel**: a Slack bot that reasons about your request, calls real tools over MCP to fetch live data, and automatically fails over from **Claude 3.5 Sonnet** to **Gemini 2.5 Flash** without the user ever noticing.

---

## ✨ What It Does

Talk to Sentinel in Slack like a coworker:

> **You:** Which support tickets are still open?
> **Sentinel:** There's one open ticket — **#1, "Login page returns 500 error,"** assigned to **alice**.

> **You:** Who's working on dark mode?
> **Sentinel:** Ticket **#2, "Add dark mode to settings,"** is in progress and assigned to **bob**.

Under the hood, Sentinel:
1. Receives the message through the **Slack Bolt** Socket Mode listener.
2. Hands the request — plus a live catalog of **MCP tools** — to **Claude 3.5 Sonnet**.
3. Lets the model decide *which* tool to call (e.g. `read_query` against a SQLite database), executes it over MCP, and feeds the result back so the model can compose a natural-language answer.
4. If Anthropic is unavailable for **any** reason, transparently re-runs the same agentic loop on **Gemini 2.5 Flash**.
5. Returns the final answer to Slack.

---

## 🏛️ Architecture

```
        ┌───────────────────────────────────────────────────────────────────────┐
        │                                Slack                                    │
        │   messages · @mentions · App Home tab · button clicks   (Socket Mode)   │
        └───────┬──────────────────────────┬──────────────────────────┬──────────┘
                │ message / mention         │ app_home_opened          │ outbound
                ▼                           ▼  & button action          │ alert push
   ┌────────────────────────┐   ┌──────────────────────────┐           │
   │  REACTIVE handlers      │   │  VISUAL handlers (Home)   │           │
   │  generate_reply()       │   │  build_home_view() +      │           │
   │                         │   │  views_publish()          │           │
   └───────────┬────────────┘   └────────────┬─────────────┘           │
               │                              │                          │
               │   ┌──────────────────────────┴──────────────┐          │
               │   │  PROACTIVE worker (Phase 4)              │          │
               │   │  incident_monitor() polls every 15s,     │──────────┘
               │   │  detects cascades, asks the LLM, alerts  │
               │   └──────────────────────┬───────────────────┘
               │                          │
               └──────────┬───────────────┘
                          ▼
            ┌──────────────────────────┐      try/except failover
            │  PRIMARY: Claude 3.5     │ ───────────────────────────┐
            │  Sonnet (Anthropic)      │      on ANY exception       │
            │  agentic tool loop       │                             ▼
            └───────────┬──────────────┘      ┌────────────────────────────┐
                        │                      │  FALLBACK: Gemini 2.5 Flash │
                        │                      │  (google-genai) same loop   │
                        │                      └───────────┬────────────────┘
                        └──────────────┬───────────────────┘
                                       │  run_tool / get_tickets / resolve_ticket
                                       │  asyncio.run_coroutine_threadsafe
                                       ▼
                        ┌──────────────────────────────────────────────┐
                        │   Background asyncio loop (daemon thread)    │
                        │           AsyncMCPClient  (mcp)              │
                        └──────────────────────┬───────────────────────┘
                                               │  stdio (JSON-RPC)
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │   SQLite MCP Server  (npx, subprocess)       │
                        │   read_query · write_query · list_tables ... │
                        └──────────────────────┬───────────────────────┘
                                               ▼
                                          data.db (SQLite)
```

---

## 🔭 Highlight 1 — Agentic Orchestration

Sentinel doesn't hard-code "if the user says X, run query Y." It runs a true **agentic tool-use loop**: the model is given the *menu* of tools and decides, on its own, which to call and with what arguments.

The loop (identical in spirit for both providers):

1. Send the user's message **+ the MCP tool schemas** to the model.
2. If the model responds with a **tool call**, execute it via MCP and append the result to the conversation.
3. Repeat — the model can chain multiple tool calls — until it returns a final natural-language answer.

```python
def ask_claude(user_text):
    tools = [{"name": t.name, "description": t.description or "",
              "input_schema": t.inputSchema} for t in mcp_tools]
    messages = [{"role": "user", "content": user_text}]
    while True:
        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=1024, tools=tools, messages=messages)
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")
        # execute every tool the model asked for, feed results back, loop
        ...
```

Because the tool catalog is fetched live from the MCP server at startup, **adding a new capability requires zero changes to the orchestration code** — expose a new MCP tool and the agent can use it immediately.

---

## ⚡ Highlight 2 — Async MCP SQLite Integration

Sentinel speaks the **Model Context Protocol** to a SQLite tool server, giving the LLM five real database tools: `read_query`, `write_query`, `create_table`, `list_tables`, and `describe_table`.

The tricky part: **Slack Bolt handlers are synchronous**, but the MCP Python SDK is **async** and the stdio session must stay alive for the whole process (it owns a long-running subprocess). We solved this cleanly:

- **`AsyncMCPClient`** (`mcp_client.py`) wraps the MCP stdio session, using an `AsyncExitStack` so the session and its subprocess stay open for the process lifetime.
- At startup, the client is launched on a **dedicated asyncio event loop running in a daemon thread** (`run_forever`). This keeps the MCP session warm and reusable.
- From the synchronous Slack handler, tool calls are marshalled onto that loop with **`asyncio.run_coroutine_threadsafe`** — bridging sync ↔ async without blocking Slack's threads or re-spawning the server per request.

```python
def run_tool(name, arguments):
    """Execute an MCP tool from a sync Slack thread via the background loop."""
    result = asyncio.run_coroutine_threadsafe(
        mcp_client.call_tool(name, arguments), mcp_loop
    ).result()
    texts = [b.text for b in result.content if getattr(b, "text", None)]
    return "\n".join(texts) if texts else str(result.content)
```

The SQLite MCP server itself runs as a subprocess over stdio (`npx -y mcp-server-sqlite-npx data.db`), so the bot owns the full lifecycle — no separate service to deploy.

---

## 🛡️ Highlight 3 — Anthropic → Gemini Multi-Model Failover

This is the heart of Sentinel. The agent is **provider-agnostic**: the same request can be served by Claude or Gemini, and the switch is automatic.

```python
def generate_reply(user_text):
    try:
        return ask_claude(user_text)            # PRIMARY: Claude 3.5 Sonnet
    except Exception as primary_error:
        logger.warning("Anthropic call failed (%s). Falling back to Gemini.",
                       primary_error)
        try:
            return ask_gemini(user_text)        # FALLBACK: Gemini 2.5 Flash
        except Exception:
            logger.exception("Gemini fallback also failed.")
            return "Sorry, both Claude and Gemini are unavailable right now."
```

What makes this non-trivial is that **the two providers expect different tool schemas**, so the same MCP tools have to be translated on the fly:

| | Anthropic (Claude) | Google (Gemini) |
|---|---|---|
| Tool wrapper | `{"name", "description", "input_schema"}` | `types.Tool(function_declarations=[...])` |
| Schema field | `input_schema` (raw JSON Schema) | `parameters_json_schema` (raw JSON Schema) |
| Tool-call signal | `stop_reason == "tool_use"` | `response.function_calls` |
| Result format | `{"type": "tool_result", "tool_use_id", "content"}` | `types.Part.from_function_response(name, response)` |

Crucially, **MCP's `inputSchema` is already valid JSON Schema**, which both Gemini's `parameters_json_schema` and Anthropic's `input_schema` accept directly — so the same source of truth feeds both providers with no lossy hand-rolled conversion.

The failover triggers on **any** exception from the primary path — outages, rate limits, auth errors, timeouts — and a final safety net returns a friendly message if *both* providers are down, so the bot never crashes a Slack thread.

---

## 🚨 Highlight 4 — Autonomous Cascade Incident Detector

Sentinel isn't only **reactive** (answering when asked) — it's **proactive**. A background worker watches the database and raises the alarm *before anyone types a message*.

An `asyncio` loop runs alongside the Slack socket handler on the same warm MCP event loop. Every 15 seconds it queries for recent error-keyword tickets, and when it detects a **cascade** — 3+ tickets mentioning `error` / `timeout` / `bug` inside a 10-minute window — it autonomously packages the context, asks the **same Claude → Gemini failover engine** to diagnose a likely root cause, and pushes a formatted **High-Priority Incident Alert** straight into the `#alerts` channel.

```python
async def incident_monitor():
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECS)            # every 15s
        rows = await poll_recent_error_tickets()           # MCP read_query
        if len(rows) < CASCADE_THRESHOLD:                  # need 3+ in window
            continue
        # dedup + cooldown so we alert once per incident, not every poll
        alert = await asyncio.to_thread(build_incident_alert, rows)   # LLM root-cause
        await asyncio.to_thread(
            app.client.chat_postMessage, channel=ALERTS_CHANNEL, text=alert)
```

Two details make it production-grade rather than a noisy demo:
- **Dedup + cooldown** — alerted ticket IDs are tracked and a 5-minute cooldown applies, so a real incident produces *one* alert, not a stream of duplicates every 15 seconds.
- **Non-blocking by design** — DB reads are awaited natively on the MCP loop, while the blocking LLM call and Slack post are offloaded with `asyncio.to_thread`, so the worker never starves the reactive Slack handlers sharing the loop.

It's also **graceful**: if both LLMs are down, it still fires a templated root-cause alert instead of staying silent. Enable it with `INCIDENT_MONITOR=1`.

---

## 🖥️ Highlight 5 — App Home Dashboard & Visual CRUD

Beyond chat, Sentinel ships a full **visual control panel** in the Slack **App Home** tab, built with **Block Kit**. Open the bot and you see a live ticket dashboard — no commands to memorize.

When a user opens the tab, an `app_home_opened` handler fetches every ticket through MCP and renders:
- A header and **KPI summary** (Active vs Closed counts)
- One **card per ticket** — status emoji (🔴 open · 🟡 in progress · ✅ closed), title, ID and assignee
- A **"Mark Resolved" button** on every active ticket

Clicking the button updates SQLite **through the same MCP tools** (a `write_query` UPDATE) and instantly re-publishes the view — a complete read → write → refresh loop, entirely inside Slack.

```python
@app.action("mark_resolved")
def handle_mark_resolved(ack, body, client):
    ack()                                         # ack within Slack's 3s window
    resolve_ticket(body["actions"][0]["value"])   # write_query via MCP bridge
    tickets = get_tickets()                        # read_query via MCP bridge
    client.views_publish(user_id=body["user"]["id"], view=build_home_view(tickets))
```

The interesting part is the **same sync ↔ async bridge** reused once more: Bolt's event and action handlers are synchronous, so `get_tickets()` and `resolve_ticket()` marshal their MCP coroutines onto the background loop with `run_coroutine_threadsafe` — exactly like `run_tool`. The dashboard, the agent, and the incident monitor all share one warm MCP session.

---

## 🛠️ How We Built It

Built in five disciplined phases:

| Phase | What shipped |
|-------|--------------|
| **1 — Foundation** | Slack Bolt app over **Socket Mode** (runs locally, no ngrok), `.env` config, `hello → "System online."` smoke test. |
| **2 — MCP Integration** | `setup_db.py` generates a mock `data.db` (`tickets` table). `AsyncMCPClient` connects to the SQLite MCP server over stdio and lists tools, initialized on a background asyncio loop alongside Slack. |
| **3 — Agentic Orchestration + Failover** | Wired the message listener to Claude's tool-use loop, added the Gemini fallback with on-the-fly schema translation, and the threadsafe MCP tool bridge. |
| **4 — Proactive Incident Detection** | A background `asyncio` worker polls for error-ticket cascades, asks the failover engine for a root cause, and autonomously pushes alerts to `#alerts` — with dedup + cooldown so it's signal, not noise. |
| **5 — App Home Dashboard + Visual CRUD** | A Block Kit control panel in the App Home tab: live KPIs, per-ticket cards, and a "Mark Resolved" button that writes back to SQLite via MCP and refreshes in place. |

**Stack:** Python 3.13 · `slack_bolt` (+ Block Kit, App Home) · `mcp` · `anthropic` · `google-genai` · `python-dotenv` · SQLite.

---

## 🧗 Challenges We Ran Into

- **Sync ↔ async impedance mismatch.** Slack Bolt is synchronous; MCP is async and stateful. We landed on a dedicated background event loop + `run_coroutine_threadsafe` rather than spinning up a new loop (and a new server subprocess) per message.
- **A broken reference package.** The official `@modelcontextprotocol/server-sqlite` npm package no longer exists (the reference SQLite server moved to Python/`uvx` and was archived). Following our "verify the source of truth" rule, we caught the 404 *before* writing code and substituted the drop-in community server `mcp-server-sqlite-npx`, preserving the exact `npx`-over-stdio architecture.
- **Two tool dialects, one tool catalog.** Claude and Gemini disagree on tool schemas and tool-call signaling. We normalized on MCP's JSON Schema as the single source and wrote thin per-provider adapters.
- **Avoiding reply loops.** The bot ignores its own (`bot_id`/`subtype`) messages so it never answers itself.

---

## 🏆 Accomplishments We're Proud Of

- A **genuinely resilient** agent — provider failure is a logged warning, not an outage.
- **Zero-config tool growth** — new MCP tools are picked up automatically by both models.
- A clean **sync/async bridge** that keeps a single long-lived MCP session warm for the whole process.
- End-to-end verified: MCP handshake, both tool-schema builds, the live SQLite query path, and the failover logic were all tested without burning a single paid API call.

---

## 📚 What We Learned

- MCP's choice of plain JSON Schema is what makes multi-provider tool use practical — it's the lingua franca both Anthropic and Google already speak.
- Resilience is an *architecture* decision, not an afterthought: designing the LLM as a swappable component from day one made failover a ~10-line `try/except`.
- "Use the official package" is advice, not a guarantee — verifying against the live registry saved us from shipping a dead command.

---

## 🚀 What's Next

- Add a **third fallback tier** (e.g. a local model) and circuit-breaker/health checks to prefer the fastest healthy provider.
- Expand beyond SQLite — plug in additional MCP servers (filesystem, web search, internal APIs) for richer agentic workflows.
- Stream responses token-by-token into Slack and surface tool-call traces in a thread for transparency.
- Per-user conversation memory and write operations (creating/closing tickets) with confirmation guards.

---

## ⚙️ Setup & Run

**Prerequisites:** Python 3.13, Node.js (for `npx`), a Slack app, and Anthropic + Gemini API keys.

```bash
# 1. Install dependencies
python -m venv venv
./venv/Scripts/Activate.ps1          # Windows PowerShell
pip install -r requirements.txt

# 2. Configure credentials
copy .env.example .env               # then fill in your real values

# 3. Generate the mock database
python setup_db.py                   # creates data.db with a 'tickets' table

# 4. Run the bot
python app.py
```

On startup you'll see `MCP connected. Available tools: [...]`. Then DM the bot (or @mention it in a channel) and ask about your tickets.

### Environment variables (`.env`)

| Variable | Purpose |
|----------|---------|
| `SLACK_BOT_TOKEN` | Bot User OAuth token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | App-level token with `connections:write` (`xapp-…`) — required for Socket Mode |
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `ANTHROPIC_API_KEY` | Primary model (Claude 3.5 Sonnet) |
| `GEMINI_API_KEY` | Fallback model (Gemini 2.5 Flash) |
| `INCIDENT_MONITOR` | Set to `1` to enable the proactive incident monitor (Phase 4). Default off. |
| `ALERTS_CHANNEL` | Channel for proactive alerts, e.g. `#alerts` (bot must be a member). |

### Enable the App Home dashboard (Phase 5)

In your Slack app settings at [api.slack.com/apps](https://api.slack.com/apps):
1. **App Home** → toggle **Home Tab** on.
2. **Event Subscriptions** → **Subscribe to bot events** → add `app_home_opened`, then **Save Changes** (Socket Mode needs no Request URL).
3. Reinstall the app if Slack prompts you.

Then open the bot in Slack and click the **Home** tab to see the live ticket dashboard. Click **Mark Resolved** on any active ticket to write the change back to SQLite via MCP and watch the view refresh in place.

### Test the failover

Put an invalid `ANTHROPIC_API_KEY` in `.env` (keep a valid `GEMINI_API_KEY`) and restart. Ask the same question — the console logs `Anthropic call failed (...). Falling back to Gemini.` and you still get a correct answer, now served by Gemini.

### Demo the proactive incident monitor

Set `INCIDENT_MONITOR=1` in `.env`, make sure the bot is in your `ALERTS_CHANNEL`, and start the bot. Then trigger a cascade:

```bash
python inject_test_tickets.py   # inserts 3 "timeout error" tickets
```

Within ~15 seconds the monitor detects the cascade and posts an autonomous **High-Priority Incident Alert** to `#alerts` — diagnosed by Claude (or Gemini on failover), with no one having typed a message.

---

## 📁 Project Structure

```
.
├── app.py                 # Slack bot: agentic orchestration, failover,
│                          #   proactive incident monitor, App Home dashboard
├── mcp_client.py          # AsyncMCPClient — async MCP stdio client (connect/list_tools/call_tool)
├── setup_db.py            # Generates the mock SQLite database (data.db)
├── inject_test_tickets.py # Injects timeout-error tickets to trigger a cascade alert (demo)
├── requirements.txt       # Pinned dependencies
├── .env.example           # Credential template
└── README.md
```
