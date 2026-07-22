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


def add_portions(menu_item_id, additional_quantity):
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
    return portion
