import logging

from sentinel.config import BOTH_LLMS_DOWN
from sentinel.llm.claude import ask_claude
from sentinel.llm.gemini import ask_gemini

logger = logging.getLogger(__name__)


def generate_reply(user_text, ctx, history=None):
    try:
        return ask_claude(user_text, ctx, history)
    except Exception as primary_error:
        logger.warning(
            "Anthropic call failed (%s). Falling back to Gemini.", primary_error
        )
        try:
            return ask_gemini(user_text, ctx, history)
        except Exception:
            logger.exception("Gemini fallback also failed.")
            return BOTH_LLMS_DOWN
