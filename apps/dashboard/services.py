from django.db.models import F
from django.utils import timezone

from .models import ChatMessage

CHAT_HISTORY_LIMIT = 10

CHAT_SYSTEM_PROMPT = (
    "You are the AI assistant embedded in DineOS, a restaurant management app. You are talking "
    "directly to a restaurant Manager or Admin inside the app's chat screen. Answer their "
    "operational questions (stock, restocking, sales, general running-the-restaurant advice) "
    "helpfully and concisely, in plain conversational text (not JSON, no markdown headers). "
    "You are given a snapshot of the restaurant's current stock and today's revenue as context — "
    "ground your answers in those real numbers when relevant, and say so plainly if something "
    "asked about isn't covered by the context you were given, rather than guessing."
)


def build_chat_context(restaurant):
    """Snapshot of current restaurant state handed to Groq as grounding —
    same idea as AI Insights/Prep Forecast: the app supplies real facts,
    the model only ever phrases/reasons over them, never invents numbers.
    """
    from apps.billing.services import daily_collections
    from apps.inventory.models import Ingredient

    low_stock = list(
        Ingredient.objects.filter(restaurant=restaurant, is_active=True)
        .exclude(current_stock__gt=F("minimum_stock_level"))
        .values("name", "current_stock", "unit", "minimum_stock_level")[:20]
    )
    today = daily_collections(restaurant, timezone.localdate())

    return {
        "low_or_critical_stock_ingredients": [
            {
                "name": i["name"], "current_stock": float(i["current_stock"]), "unit": i["unit"],
                "minimum_stock_level": float(i["minimum_stock_level"]),
            }
            for i in low_stock
        ],
        "today": {
            "total_collected": float(today["total_collected"]),
            "vs_yesterday": float(today["vs_yesterday"]),
            "tables_served": today["tables_served"],
        },
    }


def send_chat_message(restaurant, user, content):
    """Persists the user's message, calls Groq with recent history + a
    fresh context snapshot, persists and returns the assistant's reply.
    """
    import json

    from core.ai_client import generate_reply

    user_message = ChatMessage.objects.create(restaurant=restaurant, user=user, role=ChatMessage.Role.USER, content=content)

    history = list(
        ChatMessage.objects.filter(restaurant=restaurant, user=user).order_by("-created_at")[:CHAT_HISTORY_LIMIT]
    )
    history.reverse()

    context = build_chat_context(restaurant)
    messages = [{"role": "system", "content": f"Current restaurant snapshot: {json.dumps(context)}"}]
    messages += [
        {"role": "user" if m.role == ChatMessage.Role.USER else "assistant", "content": m.content}
        for m in history
    ]

    reply_text = generate_reply(CHAT_SYSTEM_PROMPT, messages)

    assistant_message = ChatMessage.objects.create(
        restaurant=restaurant, user=user, role=ChatMessage.Role.ASSISTANT, content=reply_text,
    )
    return user_message, assistant_message
