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


def test_unlimited_item_deducts_recipe_ingredients_when_sold(table, menu_item, chicken, rice, recipe):
    """2026-09-14, per Karwin: an Unlimited item's ingredients were never
    deducted anywhere - it skips the Prep Log by design, and nothing
    deducted at point of sale either. Now deducts per serving sold."""
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    menu_item.tracks_daily_portions = False
    menu_item.save(update_fields=["tracks_daily_portions"])
    menu_item.prepared_portions.all().delete()

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 4}])

    chicken.refresh_from_db()
    rice.refresh_from_db()
    assert chicken.current_stock == Decimal("49.00")  # 50 - (0.250 * 4)
    assert rice.current_stock == Decimal("49.20")  # 50 - (0.200 * 4)
    assert StockMovement.objects.filter(
        ingredient=chicken, movement_type=StockMovement.MovementType.USAGE,
    ).count() == 1


def test_batch_tracked_item_does_not_double_deduct_on_sale(table, menu_item, chicken, rice, recipe):
    """The other half of the same fix: Batch-tracked items already deduct
    upfront via the Prep Log, so selling one must NOT deduct again."""
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    menu_item.tracks_daily_portions = True
    menu_item.save(update_fields=["tracks_daily_portions"])

    stock_before = chicken.current_stock

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 4}])

    chicken.refresh_from_db()
    assert chicken.current_stock == stock_before  # untouched - already deducted at prep time
    assert not StockMovement.objects.filter(
        ingredient=chicken, movement_type=StockMovement.MovementType.USAGE,
    ).exists()


def test_prepared_dishes_today_scoped_to_own_branch(manager_client, restaurant, branch):
    """2026-09-14 fix, per Karwin/Shereena: GET /v1/prepared-dishes/today/
    had no branch scoping at all - a Manager pinned to one branch saw every
    branch's prepared dishes. Also dropped the branch-less-is-shared
    fallback entirely (Shereena's re-test found it was letting other
    branches' menu content leak through even with a real branch filter
    applied) - a branch-less item's portions no longer show for ANY
    specific branch."""
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

    branchless_category = Category.objects.create(restaurant=restaurant, branch=None, name="Branchless", sort_order=3)
    branchless_item = MenuItem.objects.create(category=branchless_category, name="Branchless Dish", price=Decimal("100.00"))
    PreparedPortion.objects.create(menu_item=branchless_item, date=timezone.localdate(), portions_initial=10, portions_remaining=10)

    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    response = client.get("/v1/prepared-dishes/today/")

    assert response.status_code == 200
    names = {row["menu_item_name"] for row in response.data}
    assert names == {own_item.name}
    assert other_item.name not in names
    assert branchless_item.name not in names


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


def test_add_portions_lets_stock_go_negative_and_restock_self_corrects(
    manager_client, menu_item, chicken, recipe
):
    """2026-09-21, per Shereena and Karwin - the direct reversal of the
    2026-08-31 clamp.

    That clamp was added because a negative balance looked like a
    phantom debt. But restock is ADDITIVE, so flooring at zero silently
    throws the overdraft away and the balance never catches up: use 25
    against 5 on hand, floor to 0, restock 10, and the books say 10 when
    only 10 - 20 = -10 worth of it is really free. Allowing the negative
    makes the arithmetic self-correcting, which is what this asserts.
    """
    _, client = manager_client
    chicken.current_stock = Decimal("5.00")
    chicken.save(update_fields=["current_stock"])

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 100}, format="json",
    )

    assert response.status_code == 200, response.data
    chicken.refresh_from_db()
    # 0.25 * 100 = 25 used against 5 on hand.
    assert chicken.current_stock == Decimal("-20.00")

    # And the restock corrects itself rather than starting from a lie:
    # -20 + 30 = 10, which is genuinely what is on the shelf.
    from apps.inventory.services import add_stock

    add_stock(chicken.id, Decimal("30.00"))
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("10.00")


def test_add_portions_reports_what_it_pushed_under(manager_client, menu_item, chicken, recipe):
    """stock_warnings lets the app raise the over-used banner straight
    from the save response, with no second call per ingredient."""
    _, client = manager_client
    chicken.current_stock = Decimal("2.00")
    chicken.save(update_fields=["current_stock"])

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 40}, format="json",
    )

    assert response.status_code == 200, response.data
    warnings = response.data["stock_warnings"]
    assert len(warnings) == 1, warnings
    w = warnings[0]
    assert w["ingredient_id"] == str(chicken.id)
    assert w["ingredient_name"] == chicken.name
    assert w["requested"] == "10.000"          # 0.25 * 40
    assert w["available_before"] == "2.00"
    assert w["current_stock"] == "-8.00"


def test_add_portions_reports_no_warnings_when_stock_is_sufficient(
    manager_client, menu_item, chicken, recipe
):
    """Empty array in the normal case - never null, never absent."""
    _, client = manager_client
    chicken.current_stock = Decimal("500.00")
    chicken.save(update_fields=["current_stock"])

    response = client.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 4}, format="json",
    )

    assert response.status_code == 200
    assert response.data["stock_warnings"] == []


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
        # deduct_for_usage returns (movement, warning) since 2026-09-21,
        # so the successful first call has to hand back a 2-tuple or
        # add_portions fails unpacking it and this test passes for
        # entirely the wrong reason.
        side_effect=[(None, None), RuntimeError("simulated failure on second ingredient")],
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
    # 1 - (0.25 * 20) = -4. Negative since 2026-09-21; the point of this
    # test is that the prep was NOT blocked, which still holds.
    assert chicken.current_stock == Decimal("-4.00")
    assert StockMovement.objects.filter(ingredient=chicken, movement_type="USAGE", quantity=Decimal("5.00")).exists()


def test_dish_is_auto_86ed_when_a_recipe_ingredient_hits_zero(
    manager_client, menu_item, chicken, recipe
):
    """2026-09-21, per Shereena: a dish whose recipe needs an ingredient
    at or below zero cannot be made, so it must stop being orderable on
    every client at once - computed server-side from live stock rather
    than each app deciding for itself."""
    _, client = manager_client

    chicken.current_stock = Decimal("100.00")
    chicken.save(update_fields=["current_stock"])
    listing = client.get("/v1/menu/all/")
    row = [i for i in listing.data["results"] if i["id"] == menu_item.id][0]
    assert row["is_available"] is True
    assert row["unavailable_reason"] == ""

    chicken.current_stock = Decimal("0.00")
    chicken.save(update_fields=["current_stock"])

    listing = client.get("/v1/menu/all/")
    row = [i for i in listing.data["results"] if i["id"] == menu_item.id][0]
    assert row["is_available"] is False
    assert chicken.name in row["unavailable_reason"]


def test_auto_86ed_dish_disappears_from_the_orderable_menu(
    manager_client, menu_item, chicken, recipe
):
    """The queryset that feeds the customer QR menu and the order-taking
    screen - i.e. every path that can put the dish on a bill."""
    _, client = manager_client

    chicken.current_stock = Decimal("100.00")
    chicken.save(update_fields=["current_stock"])
    available = client.get("/v1/menu/")
    assert any(i["id"] == menu_item.id for i in available.data)

    chicken.current_stock = Decimal("-1.00")
    chicken.save(update_fields=["current_stock"])

    available = client.get("/v1/menu/")
    assert not any(i["id"] == menu_item.id for i in available.data)


def test_restocking_brings_an_auto_86ed_dish_straight_back(
    manager_client, menu_item, chicken, recipe
):
    """Self-healing is the whole reason this is computed rather than
    stored - there is no flag to reset and no job to run."""
    from apps.inventory.services import add_stock

    _, client = manager_client
    chicken.current_stock = Decimal("-5.00")
    chicken.save(update_fields=["current_stock"])
    assert not any(i["id"] == menu_item.id for i in client.get("/v1/menu/").data)

    add_stock(chicken.id, Decimal("20.00"))

    assert any(i["id"] == menu_item.id for i in client.get("/v1/menu/").data)


def test_a_dish_with_no_recipe_is_never_auto_86ed(manager_client, menu_item, chicken):
    """Nothing is known about what it consumes, so claiming it is out of
    stock would be a guess. No `recipe` fixture here on purpose."""
    _, client = manager_client
    chicken.current_stock = Decimal("-50.00")
    chicken.save(update_fields=["current_stock"])

    listing = client.get("/v1/menu/all/")
    row = [i for i in listing.data["results"] if i["id"] == menu_item.id][0]
    assert row["is_available"] is True


def test_manual_switch_still_wins_and_says_so(manager_client, menu_item, chicken, recipe):
    """The stored toggle is still the manager's own override, and the
    reason has to distinguish it from an out-of-stock 86."""
    _, client = manager_client
    chicken.current_stock = Decimal("100.00")
    chicken.save(update_fields=["current_stock"])
    menu_item.is_available = False
    menu_item.save(update_fields=["is_available"])

    listing = client.get("/v1/menu/all/")
    row = [i for i in listing.data["results"] if i["id"] == menu_item.id][0]
    assert row["is_available"] is False
    assert row["unavailable_reason"] == "Turned off manually"


def test_ordering_an_auto_86ed_dish_is_accepted_but_flagged(api_client, table, menu_item, chicken, recipe):
    """2026-09-23, option (c) per Karwin. The menu hides an 86'd dish, but
    order placement never refused one - an app whose menu loaded before
    the ingredient ran out could still get it through.

    Refusing outright was considered and rejected: stock counts drift
    from reality, so a hard block can turn away an order the kitchen
    could actually cook. The order goes through and carries a flag
    instead, so a human decides.
    """
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    chicken.current_stock = Decimal("0.00")
    chicken.save(update_fields=["current_stock"])
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 201, response.data
    flagged = response.data["unavailable_items"]
    assert len(flagged) == 1, flagged
    assert flagged[0]["menu_item"] == menu_item.id
    assert flagged[0]["menu_item_name"] == menu_item.name
    assert chicken.name in flagged[0]["reason"]
    assert flagged[0]["out_of_stock_ingredients"] == [chicken.name]


def test_ordering_an_available_dish_flags_nothing(api_client, table, menu_item, chicken, recipe):
    """Empty list in the normal case - never null, never absent."""
    from apps.tables import services as table_services

    chicken.current_stock = Decimal("500.00")
    chicken.save(update_fields=["current_stock"])
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["unavailable_items"] == []


def test_taking_the_last_portion_does_not_flag_itself(api_client, table, menu_item, chicken, recipe):
    """The flag is evaluated BEFORE this order's own deduction. Otherwise
    whoever takes the last portion always trips the warning, which trains
    people to ignore it."""
    from apps.tables import services as table_services

    # Exactly enough for one serving (recipe is 0.25 per portion).
    chicken.current_stock = Decimal("0.25")
    chicken.save(update_fields=["current_stock"])
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["unavailable_items"] == [], (
        "the dish was still sellable when they ordered - it ran out because of this order"
    )
    chicken.refresh_from_db()
    assert chicken.current_stock == Decimal("0.00")
