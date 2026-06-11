import logging

from anthropic import Anthropic

from sentinel import guard, mcp_bridge
from sentinel.config import ANTHROPIC_MODEL

logger = logging.getLogger(__name__)

anthropic_client = Anthropic()


def ask_claude(user_text, ctx):
    ctx.provider = "claude"
    tools = [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_bridge.tools
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
                output = guard.execute(block.name, block.input, ctx)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
        messages.append({"role": "user", "content": tool_results})
