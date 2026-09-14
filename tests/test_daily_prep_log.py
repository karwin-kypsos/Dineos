from decimal import Decimal

import pytest

from apps.inventory.models import Ingredient, RecipeItem, StockMovement

pytestmark = pytest.mark.django_db


@pytest.fixture
def chicken(restaurant):
    return Ingredient.objects.create(
        restaurant=restaurant, name="Chicken", unit="KG", current_stock=Decimal("50.00"),
    )


@pytest.fixture
def rice(restaurant):
    return Ingredient.objects.create(
        restaurant=restaurant, name="Rice", unit="KG", current_stock=Decimal("50.00"),
    )


@pytest.fixture
def recipe(menu_item, chicken, rice):
    RecipeItem.objects.create(menu_item=menu_item, ingredient=chicken, quantity_per_serving=Decimal("0.250"))
    RecipeItem.objects.create(menu_item=menu_item, ingredient=rice, quantity_per_serving=Decimal("0.200"))


def test_prepared_dishes_today_scoped_to_own_branch(manager_client, restaurant, branch):
    """2026-09-14 fix, per Karwin: GET /v1/prepared-dishes/today/ had no
    branch scoping at all - a Manager pinned to one branch saw every
    branch's prepared dishes. A shared (no-branch) item's portions should
    still show everywhere, matching how the menu itself is scoped."""
    from django.utils import timezone

    from apps.menu.models import Category, MenuItem, PreparedPortion
    from apps.restaurant.models import Branch

    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")

    own_category = Category.objects.create(restaurant=restaurant, branch=branch, name="Own", sort_order=1)
    own_item = MenuItem.objects.create(category=own_category, name="Own Dish", price=Decimal("100.00"))
    PreparedPortion.objects.create(menu_item=own_item, date=timezone.localdate(), portions_initial=10, portions_remaining=10)

    other_category = Category.objects.create(restaurant=restaurant, branch=other_branch, name="Other", sort_order=2)
    other_item = MenuItem.objects.create(category=other_category, name="Other Dish", price=Decimal("100.00"))
    PreparedPortion.objects.create(menu_item=other_item, date=timezone.localdate(), portions_initial=10, portions_remaining=10)

    shared_category = Category.objects.create(restaurant=restaurant, branch=None, name="Shared", sort_order=3)
    shared_item = MenuItem.objects.create(category=shared_category, name="Shared Dish", price=Decimal("100.00"))
    PreparedPortion.objects.create(menu_item=shared_item, date=timezone.localdate(), portions_initial=10, portions_remaining=10)

    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    response = client.get("/v1/prepared-dishes/today/")

    assert response.status_code == 200
    names = {row["menu_item_name"] for row in response.data}
    assert own_item.name in names
    assert shared_item.name in names
    assert other_item.name not in names


def test_add_portions_deducts_recipe_ingredients(manager_client, menu_item, chicken, rice, recipe):
    _, client = manager_client

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 20}, format="json",
    )

    assert response.status_code == 200, response.data
    chicken.refresh_from_db()
    rice.refresh_from_db()
    assert chicken.current_stock == Decimal("45.00")  # 50 - (0.25 * 20)
    assert rice.current_stock == Decimal("46.00")  # 50 - (0.20 * 20)
    assert StockMovement.objects.filter(ingredient=chicken, movement_type="USAGE", quantity=Decimal("5.00")).exists()


def test_add_portions_floors_stock_at_zero_instead_of_going_negative(manager_client, menu_item, chicken, recipe):
    """2026-08-31, per Shereena's report: deducting more than what's on hand
    used to leave current_stock negative, and a later restock added on top
    of that negative number instead of starting clean from zero."""
    _, client = manager_client
    chicken.current_stock = Decimal("5.00")
    chicken.save(update_fields=["current_stock"])

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 100}, format="json",
    )

    assert response.status_code == 200, response.data
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("0.00")  # would be -20.00 pre-fix (0.25 * 100 = 25)

    # A later restock starts clean from zero, not from the old negative debt.
    from apps.inventory.services import add_stock

    add_stock(chicken.id, Decimal("10.00"))
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("10.00")


def test_add_portions_without_recipe_does_not_touch_stock(manager_client, menu_item, chicken):
    """No RecipeItem linked — behaves exactly like before this feature existed."""
    _, client = manager_client

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 10}, format="json",
    )

    assert response.status_code == 200
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("50.00")


def test_add_portions_atomic_rollback_on_failure(manager_client, menu_item, chicken, rice, recipe):
    """If the transaction fails partway, NEITHER ingredient's stock changes —
    not chicken-deducted-but-rice-not, the exact corruption case the guide
    calls out."""
    _, client = manager_client

    from unittest.mock import patch as mock_patch

    with mock_patch(
        "apps.inventory.services.deduct_for_usage",
        side_effect=[None, RuntimeError("simulated failure on second ingredient")],
    ):
        with pytest.raises(RuntimeError):
            from apps.menu import services as menu_services

            menu_services.add_portions(menu_item.id, 20)

    chicken.refresh_from_db()
    rice.refresh_from_db()
    assert chicken.current_stock == Decimal("50.00")
    assert rice.current_stock == Decimal("50.00")


def test_add_portions_with_deduction_override(manager_client, menu_item, chicken, rice, recipe):
    _, client = manager_client

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/",
        {
            "additional_quantity": 20,
            "deduction_overrides": [{"ingredient_id": str(chicken.id), "quantity": "7.5"}],
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    chicken.refresh_from_db()
    rice.refresh_from_db()
    assert chicken.current_stock == Decimal("42.50")  # override used instead of recipe (5.0)
    assert rice.current_stock == Decimal("50.00")  # rice not touched — override replaces the whole set


def test_add_portions_override_rejects_foreign_ingredient(manager_client, menu_item):
    _, client = manager_client

    from apps.restaurant.models import Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign Prep", slug="foreign-prep")
    foreign_ingredient = Ingredient.objects.create(restaurant=foreign_restaurant, name="Butter", unit="KG")

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/",
        {
            "additional_quantity": 5,
            "deduction_overrides": [{"ingredient_id": str(foreign_ingredient.id), "quantity": "1.0"}],
        },
        format="json",
    )

    assert response.status_code == 404


def test_recipe_deduction_is_not_blocked_by_insufficient_stock(manager_client, menu_item, chicken, recipe):
    """Prep already happened physically — the log must not be blocked by
    insufficient tracked stock, per the service's documented behavior. The
    balance floors at zero rather than going negative (see
    test_add_portions_floors_stock_at_zero_instead_of_going_negative)."""
    _, client = manager_client
    chicken.current_stock = Decimal("1.00")
    chicken.save(update_fields=["current_stock"])

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 20}, format="json",
    )

    assert response.status_code == 200
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("0.00")  # 1 - (0.25 * 20) = -4, floored to 0
    assert StockMovement.objects.filter(ingredient=chicken, movement_type="USAGE", quantity=Decimal("5.00")).exists()
