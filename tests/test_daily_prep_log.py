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


def test_recipe_deduction_allows_negative_stock(manager_client, menu_item, chicken, recipe):
    """Prep already happened physically — the log must not be blocked by
    insufficient tracked stock, per the service's documented behavior."""
    _, client = manager_client
    chicken.current_stock = Decimal("1.00")
    chicken.save(update_fields=["current_stock"])

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 20}, format="json",
    )

    assert response.status_code == 200
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("-4.00")  # 1 - (0.25 * 20), allowed to go negative
