# Hackathon Project: Enterprise Agentic Slack Bot
**Timeline:** 1 Month (Focus on production-grade depth, not quick hacks).
**Core Stack:** Python, Slack Bolt (Socket Mode), MCP (Model Context Protocol), Anthropic API, Google GenAI SDK (Gemini).

## Project State: What We Have Built So Far (Phases 1-3 COMPLETED)
1. **Slack Foundation:** A working Python app (`app.py`) running asynchronously using Slack Bolt over Socket Mode.
2. **MCP Database Integration:** A local SQLite database (`data.db`) managed by a mock script (`setup_db.py`). We have a custom `AsyncMCPClient` (`mcp_client.py`) that successfully runs the official Node.js SQLite MCP server in the background via `npx` and exposes 5 DB tools (read, write, list, etc.).
3. **Multi-Model Reactive Routing:** The bot listens to Slack messages. It passes the user query and the MCP tool schemas to **Claude 3.5 Sonnet**. If Claude fails, it safely falls back to **Gemini 2.5 Flash**. The LLMs autonomously use the DB tools to answer questions and return formatted text to Slack. 

## Project Future: Where We Are Heading (Phase 4 PENDING)
We are upgrading the bot from **Reactive** (waiting for a message) to **Proactive** (autonomous system monitoring).
- **The Feature:** "Autonomous Cascade Incident Detector."
- **The Goal:** A background worker will continuously poll the database for anomalies (e.g., a sudden spike in related error tickets). When detected, the worker will autonomously package the context, send it to the LLM (Claude/Gemini) for a root-cause diagnosis, and proactively push an alert message into an `#alerts` channel.

## Phase 4: Autonomous Proactive Worker (Execution Instructions)
1. **Background Task:** Implement an `asyncio` loop in `app.py` that runs alongside the Slack Socket Mode handler and the MCP Client loop.
2. **Polling Logic:** Every 15 seconds, use the MCP client to run a `read_query` fetching the latest tickets created in the last X timeframe.
3. **Trigger Threshold:** If it detects 3 or more tickets with "error", "timeout", or "bug" in the title within a short window, trigger the AI Alert Protocol.
4. **AI Alert Protocol:** Programmatically send the data to our existing Anthropic/Gemini failover logic. Ask the LLM to format a "High-Priority Incident Alert" detailing the suspected root cause based on the ticket titles.
5. **Slack Push:** Use `app.client.chat_postMessage` to push the final LLM text to a specific Slack channel without any user having typed a prompt.

## Phase 5: Slack App Home Dashboard & Visual CRUD (Execution Instructions)
We are transforming the bot into a fully visual application by utilizing the Slack App Home tab and Block Kit.
1. **Event Listener:** Add an `@app.event("app_home_opened")` listener in `app.py`.
2. **Data Fetching:** When a user opens the Home tab, use the `MCPClient` to asynchronously fetch all current tickets (`list_tables` / `read_query`).
3. **Block Kit Rendering:** Construct a beautiful Slack Block Kit view containing:
   - A Header: "Enterprise Agentic Control Panel"
   - KPIs: A section summarizing the total number of open vs. closed tickets.
   - Ticket List: Iterate through the fetched tickets and render each as a visual card (Section block) showing ID, Title, Status, and Assignee.
   - Interactive Buttons: Add an "Acknowledge" or "Close" button to the active tickets.
4. **Action Routing:** Add `@app.action("action_id")` listeners to handle button clicks, update the SQLite database via MCP, and immediately refresh the Home tab view.