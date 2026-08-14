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


def compute_eod_data(restaurant, review_date):
    """Manager's End of Day Review numbers — revenue (via the existing
    apps.billing.services.daily_collections, reused rather than duplicated),
    plus wastage, restocks, low-stock state, and staff activity for the
    day. Shared by both the manual End of Day Review screen and the AI
    End of Day Report below, so they're always looking at the same facts.
    """
    from datetime import datetime
    from decimal import Decimal

    from django.db.models import Q

    from apps.billing.models import Bill
    from apps.billing.services import daily_collections
    from apps.inventory.models import Ingredient, StockMovement
    from apps.orders.models import Order

    collections = daily_collections(restaurant, review_date)
    collections["bills"] = None  # full bill objects aren't JSON-serializable here; counts/totals only

    day_start = timezone.make_aware(datetime.combine(review_date, datetime.min.time()))
    day_end = day_start + timezone.timedelta(days=1)

    orders_qs = Order.objects.filter(
        Q(table__restaurant=restaurant) | Q(branch__restaurant=restaurant),
        placed_at__gte=day_start, placed_at__lt=day_end,
    )
    movements = StockMovement.objects.filter(
        ingredient__restaurant=restaurant, recorded_at__gte=day_start, recorded_at__lt=day_end,
    )
    wastage = movements.filter(movement_type=StockMovement.MovementType.WASTAGE)
    restocks = movements.filter(movement_type=StockMovement.MovementType.RESTOCK)
    wastage_cost = sum((m.quantity * (m.unit_cost_at_time or Decimal("0")) for m in wastage), Decimal("0"))

    staff_ids = set(orders_qs.exclude(placed_by__isnull=True).values_list("placed_by_id", flat=True))
    staff_ids |= set(
        Bill.objects.filter(
            Q(session__table__restaurant=restaurant) | Q(order__branch__restaurant=restaurant),
            paid_at__gte=day_start, paid_at__lt=day_end,
        ).exclude(processed_by__isnull=True).values_list("processed_by_id", flat=True)
    )

    low_stock_count = sum(1 for i in Ingredient.objects.filter(restaurant=restaurant, is_active=True) if i.is_low_stock)

    return {
        **collections,
        "orders_placed": orders_qs.count(),
        "orders_cancelled": orders_qs.filter(status="CANCELLED").count(),
        "wastage_entries_count": wastage.count(),
        "wastage_total_cost": wastage_cost,
        "restock_entries_count": restocks.count(),
        "low_stock_count_at_review": low_stock_count,
        "staff_active_count": len(staff_ids),
    }


EOD_REPORT_SYSTEM_PROMPT = (
    "You are the AI Restaurant Operating System's end-of-day analyst for DineOS. Given a "
    "structured summary of today's numbers, the ingredients currently low/critical on stock, and "
    "tomorrow's prep forecast for each dish, return ONLY a JSON object of the shape "
    "{\"summary\": \"...\", \"recommendations\": [...]}. summary is a short (2-4 sentence) plain-"
    "text recap of how today went, citing the actual numbers given. Each item in recommendations "
    "must have: headline (one short actionable sentence for tomorrow, e.g. a prep quantity or a "
    "restock), reasoning (1 sentence citing the specific fact behind it). Cover both the restock "
    "and prep-forecast facts you're given, one recommendation per notable fact — do not invent "
    "facts, numbers, or dishes/ingredients that are not present in the input, and do not produce "
    "a recommendation with nothing backing it."
)


def generate_eod_report(restaurant, review_date=None):
    """AI End of Day Report — Manager Home / Prep Log screens' 'AI Restaurant
    Operating System' daily briefing: today's numbers (see compute_eod_data)
    phrased into a short summary, plus 'Recommendations for Tomorrow' drawn
    from tomorrow's prep forecast (apps.menu.services.compute_prep_forecast)
    and today's low/critical stock ingredients — one Groq call ties both
    together, same batched-not-per-item pattern as AI Insights/Prep Forecast.
    Stateless: recomputed fresh each call, not persisted.
    """
    import json
    from datetime import timedelta

    from core.ai_client import generate_json

    review_date = review_date or timezone.localdate()
    eod_data = compute_eod_data(restaurant, review_date)

    from apps.inventory.models import Ingredient
    from apps.menu.services import compute_prep_forecast

    low_stock = list(
        Ingredient.objects.filter(restaurant=restaurant, is_active=True)
        .exclude(current_stock__gt=F("minimum_stock_level"))
        .values("name", "current_stock", "unit", "minimum_stock_level")[:20]
    )
    prep_forecast, tomorrow = compute_prep_forecast(restaurant, target_date=review_date + timedelta(days=1))

    facts = {
        "date": str(review_date),
        "today_summary": {
            "total_collected": float(eod_data["total_collected"]),
            "vs_yesterday": float(eod_data["vs_yesterday"]),
            "tables_served": eod_data["tables_served"],
            "orders_placed": eod_data["orders_placed"],
            "orders_cancelled": eod_data["orders_cancelled"],
            "wastage_entries_count": eod_data["wastage_entries_count"],
            "wastage_total_cost": float(eod_data["wastage_total_cost"]),
            "staff_active_count": eod_data["staff_active_count"],
        },
        "low_or_critical_stock_ingredients": [
            {
                "name": i["name"], "current_stock": float(i["current_stock"]), "unit": i["unit"],
                "minimum_stock_level": float(i["minimum_stock_level"]),
            }
            for i in low_stock
        ],
        "tomorrow_prep_forecast": [
            {
                "menu_item_name": c["menu_item"].name,
                "suggested_prep_quantity": c["suggested_prep_quantity"],
                "average_quantity": c["average_quantity"],
            }
            for c in prep_forecast
        ],
    }

    result = generate_json(EOD_REPORT_SYSTEM_PROMPT, json.dumps(facts))
    if not isinstance(result, dict):
        result = {}

    return {
        **eod_data,
        "ai_summary": result.get("summary", ""),
        "recommendations_for_tomorrow": result.get("recommendations", []),
    }
