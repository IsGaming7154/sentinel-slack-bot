import re

from sentinel import memory
from sentinel.guard import GuardContext
from sentinel.handlers.replies import reply_blocks
from sentinel.llm.router import generate_reply


def _respond(raw_text, say, ctx, memory_key):
    user_text = re.sub(r"<@[^>]+>", "", raw_text or "").strip()
    if not user_text:
        return
    reply = generate_reply(user_text, ctx, memory.history(memory_key))
    memory.remember(memory_key, user_text, reply)
    say(text=reply, blocks=reply_blocks(reply, ctx))


def register(app):
    def _ctx(user_id, channel, thread_ts=None):
        return GuardContext(
            user_id=user_id, channel=channel, client=app.client, thread_ts=thread_ts
        )

    @app.message("")
    def handle_message(message, say):
        if message.get("subtype") or message.get("bot_id"):
            return
        if memory.seen_event((message.get("channel"), message.get("ts"))):
            return
        thread_ts = message.get("thread_ts")
        ctx = _ctx(message.get("user"), message.get("channel"), thread_ts)
        _respond(message.get("text"), say, ctx, thread_ts or message.get("channel"))

    @app.event("app_mention")
    def handle_mention(event, say):
        if memory.seen_event((event.get("channel"), event.get("ts"))):
            return
        thread_ts = event.get("thread_ts")
        ctx = _ctx(event.get("user"), event.get("channel"), thread_ts)
        _respond(event.get("text"), say, ctx, thread_ts or event.get("channel"))

    @app.error
    def global_error_handler(error, body, logger):
        # Never log the raw payload: message text and profile data may contain PII.
        event = body.get("event", {}) if isinstance(body, dict) else {}
        logger.exception(error)
        logger.info(
            "Failed event: type=%s channel=%s user=%s (payload redacted)",
            event.get("type") or (body.get("type") if isinstance(body, dict) else "?"),
            event.get("channel"),
            event.get("user"),
        )
