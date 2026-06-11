import re

from sentinel.llm.router import generate_reply


def _respond(raw_text, say):
    user_text = re.sub(r"<@[^>]+>", "", raw_text or "").strip()
    if not user_text:
        return
    say(generate_reply(user_text))


def register(app):
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
