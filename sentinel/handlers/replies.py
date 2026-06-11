"""Render LLM replies as Block Kit with a transparency footer.

The footer shows which model answered and what the firewall did with every
tool call — users see the guardrails working on every single reply.
"""

from sentinel import guard

MAX_SECTION_CHARS = 2900  # Slack section text caps at 3000
PROVIDER_LABEL = {"claude": "Claude", "gemini": "Gemini"}


def _plural(n, word):
    return "{} {}{}".format(n, word, "s" if n != 1 else "")


def trace_line(ctx):
    parts = []
    label = PROVIDER_LABEL.get(ctx.provider)
    if label:
        parts.append(":zap: {}".format(label))
    reads = sum(1 for _, d in ctx.tool_calls if d == guard.ALLOW)
    queued = sum(1 for _, d in ctx.tool_calls if d == guard.QUEUE)
    blocked = sum(1 for _, d in ctx.tool_calls if d == guard.BLOCK)
    if reads:
        parts.append(":mag: {}".format(_plural(reads, "read")))
    if queued:
        parts.append(":shield: {} queued for approval".format(_plural(queued, "write")))
    if blocked:
        parts.append(":no_entry: {} blocked".format(_plural(blocked, "call")))
    return " · ".join(parts)


def reply_blocks(reply_text, ctx):
    text = (reply_text or "").strip() or "_(no response)_"
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text[i : i + MAX_SECTION_CHARS]},
        }
        for i in range(0, len(text), MAX_SECTION_CHARS)
    ]
    footer = trace_line(ctx)
    if footer:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]}
        )
    return blocks
