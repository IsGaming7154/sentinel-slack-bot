import re

from sentinel.guard import GuardContext
from sentinel.llm.router import generate_reply


def _respond(raw_text, say, ctx):
    user_text = re.sub(r"<@[^>]+>", "", raw_text or "").strip()
    if not user_text:
        return
    say(generate_reply(user_text, ctx))


def register(app):
    def _ctx(user_id, channel):
        return GuardContext(user_id=user_id, channel=channel, client=app.client)

    @app.message("")
    def handle_message(message, say):
        if message.get("subtype") or message.get("bot_id"):
            return
        ctx = _ctx(message.get("user"), message.get("channel"))
        _respond(message.get("text"), say, ctx)

    @app.event("app_mention")
    def handle_mention(event, say):
        ctx = _ctx(event.get("user"), event.get("channel"))
        _respond(event.get("text"), say, ctx)

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
