import json
from datetime import timedelta

from django.db import models as dj_models
from django.db import transaction
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import PreparedPortion


def get_today_portion(menu_item, for_update=False):
    qs = PreparedPortion.objects.filter(menu_item=menu_item, date=timezone.localdate())
    if for_update:
        qs = qs.select_for_update()
    return qs.first()


def decrement_portions(menu_item, quantity):
    """Returns (portion_row_or_None, hit_zero). No-op (untracked/always
    available) if this dish has no PreparedPortion row for today.
    """
    portion = get_today_portion(menu_item, for_update=True)
    if portion is None:
        return None, False

    from core.exceptions import InsufficientPortionsError

    if portion.portions_remaining < quantity:
        raise InsufficientPortionsError(f"Only {portion.portions_remaining} {menu_item.name} remaining.")

    portion.portions_remaining -= quantity
    portion.save(update_fields=["portions_remaining", "updated_at"])
    return portion, portion.portions_remaining == 0


@transaction.atomic
def add_portions(menu_item_id, additional_quantity, recorded_by=None, deduction_overrides=None):
    """Daily Prep Log: 'I prepared N portions of this dish today.' Beyond
    just bumping the portion counter, this deducts each recipe ingredient's
    quantity_per_serving * additional_quantity from raw stock — one atomic
    transaction, so a failure partway through (e.g. one ingredient row
    locked/missing) can never leave stock deducted for some ingredients but
    not others.

    `deduction_overrides`, if given, is a list of {"ingredient_id": ...,
    "quantity": ...} that replaces the recipe-computed deduction entirely
    (e.g. today's batch actually used more/less per portion than the
    recipe says) — the portion counter still increments by
    additional_quantity either way.
    """
    from apps.inventory.models import RecipeItem

    from .models import MenuItem

    menu_item = MenuItem.objects.get(id=menu_item_id)
    portion, created = PreparedPortion.objects.get_or_create(
        menu_item=menu_item,
        date=timezone.localdate(),
        defaults={"portions_initial": additional_quantity, "portions_remaining": additional_quantity},
    )
    if not created:
        portion.portions_initial += additional_quantity
        portion.portions_remaining += additional_quantity
        portion.save(update_fields=["portions_initial", "portions_remaining", "updated_at"])

    from apps.inventory.services import deduct_for_usage

    if deduction_overrides is not None:
        for line in deduction_overrides:
            deduct_for_usage(line["ingredient_id"], line["quantity"], recorded_by=recorded_by)
    else:
        for recipe_item in RecipeItem.objects.filter(menu_item=menu_item).select_related("ingredient"):
            deduct_for_usage(
                recipe_item.ingredient_id,
                recipe_item.quantity_per_serving * additional_quantity,
                recorded_by=recorded_by,
            )

    return portion


def compute_prep_forecast(restaurant, branch=None, target_date=None, lookback_occurrences=4):
    """Pure numeric analysis, no AI involved — for each dish with any sales
    history, sums OrderItem quantity on the last `lookback_occurrences`
    calendar dates that share target_date's weekday (going back in 7-day
    steps: last Tuesday, the Tuesday before that, etc.), then averages them
    into a suggested prep quantity. Dishes with zero orders across the
    whole lookback window are skipped — nothing to forecast for them.
    """
    from apps.orders.models import Order, OrderItem

    from .models import MenuItem

    target_date = target_date or timezone.localdate()
    occurrence_dates = [target_date - timedelta(days=7 * k) for k in range(1, lookback_occurrences + 1)]

    orders_qs = Order.objects.filter(
        dj_models.Q(table__restaurant=restaurant) | dj_models.Q(branch__restaurant=restaurant),
        placed_at__date__in=occurrence_dates,
    ).exclude(status=Order.Status.CANCELLED)
    if branch is not None:
        orders_qs = orders_qs.filter(dj_models.Q(table__branch=branch) | dj_models.Q(branch=branch))

    rows = (
        OrderItem.objects.filter(order__in=orders_qs)
        .annotate(order_date=TruncDate("order__placed_at"))
        .values("menu_item_id", "order_date")
        .annotate(total_qty=dj_models.Sum("quantity"))
    )

    by_item = {}
    for row in rows:
        by_item.setdefault(row["menu_item_id"], {})[row["order_date"]] = row["total_qty"]

    results = []
    for menu_item_id, date_totals in by_item.items():
        occurrence_quantities = [date_totals.get(d, 0) for d in occurrence_dates]
        if not any(occurrence_quantities):
            continue
        menu_item = MenuItem.objects.get(id=menu_item_id)
        average = sum(occurrence_quantities) / len(occurrence_quantities)
        results.append({
            "menu_item": menu_item,
            "occurrence_dates": occurrence_dates,
            "occurrence_quantities": occurrence_quantities,
            "average_quantity": average,
            "suggested_prep_quantity": round(average),
        })
    return results, target_date


PREP_FORECAST_SYSTEM_PROMPT = (
    "You are the kitchen prep-planning analyst for a restaurant management app. Given each "
    "dish's actual order quantities on the last few occurrences of a specific weekday (oldest to "
    "newest), return ONLY a JSON object of the shape {\"forecasts\": [...]}. Each item must have: "
    "menu_item_id (string, copied exactly from the matching input item), headline (one short "
    "sentence citing the actual historical numbers and the weekday name, e.g. how many were "
    "ordered on recent occurrences), reasoning (1-2 sentences noting any trend across the "
    "occurrences — rising, falling, or steady). Never invent numbers not present in the input, "
    "and never state a suggested prep quantity yourself — that number is supplied separately "
    "by the app, not by you."
)


def generate_prep_forecast(restaurant, branch=None, target_date=None, lookback_occurrences=4):
    """Manager Home / Prep Log screens' 'AI Prep Forecast' — phrases the
    pure-math forecast (see compute_prep_forecast) into the 'Based on last
    4 Tuesdays: ...' headline via one batched Groq call covering every
    forecastable dish, not one call per dish."""
    from core.ai_client import generate_json

    computed, target_date = compute_prep_forecast(restaurant, branch, target_date, lookback_occurrences)
    if not computed:
        return [], target_date

    weekday_name = target_date.strftime("%A")
    facts = [
        {
            "menu_item_id": str(c["menu_item"].id),
            "menu_item_name": c["menu_item"].name,
            "weekday": weekday_name,
            "occurrence_dates_oldest_to_newest": [d.isoformat() for d in reversed(c["occurrence_dates"])],
            "quantities_oldest_to_newest": list(reversed(c["occurrence_quantities"])),
            "average_quantity": c["average_quantity"],
        }
        for c in computed
    ]

    result = generate_json(
        PREP_FORECAST_SYSTEM_PROMPT, json.dumps({"target_weekday": weekday_name, "dishes": facts})
    )
    raw_forecasts = result.get("forecasts", []) if isinstance(result, dict) else []
    by_id = {str(c["menu_item"].id): c for c in computed}

    output = []
    for item in raw_forecasts:
        c = by_id.get(item.get("menu_item_id"))
        if c is None:
            continue
        output.append({
            "menu_item_id": str(c["menu_item"].id),
            "menu_item_name": c["menu_item"].name,
            "suggested_prep_quantity": c["suggested_prep_quantity"],
            "average_quantity": c["average_quantity"],
            "occurrence_quantities": c["occurrence_quantities"],
            "headline": item.get("headline", ""),
            "reasoning": item.get("reasoning", ""),
        })
    return output, target_date
