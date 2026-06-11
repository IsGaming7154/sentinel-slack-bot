import logging
import os

from google import genai
from google.genai import types

from sentinel import guard, mcp_bridge
from sentinel.config import GEMINI_MODEL

logger = logging.getLogger(__name__)


def _gemini_tools():
    declarations = [
        types.FunctionDeclaration(
            name=t.name,
            description=t.description or "",
            parameters_json_schema=t.inputSchema,
        )
        for t in mcp_bridge.tools
    ]
    return [types.Tool(function_declarations=declarations)]


def ask_gemini(user_text, ctx, history=None):
    ctx.provider = "gemini"
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(tools=_gemini_tools())
    contents = [
        types.Content(
            role="user" if role == "user" else "model",
            parts=[types.Part.from_text(text=text)],
        )
        for role, text in (history or [])
    ]
    contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
    )

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
            output = guard.execute(fc.name, dict(fc.args), ctx)
            response_parts.append(
                types.Part.from_function_response(
                    name=fc.name, response={"result": output}
                )
            )
        contents.append(types.Content(role="tool", parts=response_parts))
