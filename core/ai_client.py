import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class AIUnavailableError(Exception):
    """Raised when GROQ_API_KEY isn't configured, the Groq call fails, or
    the model didn't return parseable JSON — callers turn this into a
    clean 503 rather than a raw 500."""


def generate_json(system_prompt, user_prompt, *, timeout=20):
    """One-shot structured-output call to Groq's chat completions API.
    Returns the parsed JSON object the model was instructed to produce."""
    if not settings.GROQ_API_KEY:
        raise AIUnavailableError("AI features are not configured yet (GROQ_API_KEY unset).")

    from groq import Groq

    client = Groq(api_key=settings.GROQ_API_KEY, timeout=timeout)
    try:
        completion = client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
    except Exception as e:
        logger.exception("Groq API call failed")
        raise AIUnavailableError(f"AI request failed: {e}") from e

    raw = completion.choices[0].message.content
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.error("Groq returned non-JSON content: %r", raw)
        raise AIUnavailableError("AI returned an unparseable response.") from e


def generate_reply(system_prompt, messages, *, timeout=20):
    """Plain conversational completion for AI Chat — unlike generate_json,
    this returns free-form text (no JSON mode), since a chat reply is meant
    to be read directly, not parsed. `messages` is the conversation history
    as [{"role": "user"|"assistant", "content": ...}, ...], oldest first."""
    if not settings.GROQ_API_KEY:
        raise AIUnavailableError("AI features are not configured yet (GROQ_API_KEY unset).")

    from groq import Groq

    client = Groq(api_key=settings.GROQ_API_KEY, timeout=timeout)
    try:
        completion = client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[{"role": "system", "content": system_prompt}, *messages],
            temperature=0.4,
        )
    except Exception as e:
        logger.exception("Groq API call failed")
        raise AIUnavailableError(f"AI request failed: {e}") from e

    return completion.choices[0].message.content or ""
