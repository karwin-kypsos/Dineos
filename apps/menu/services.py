from django.db import transaction
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
