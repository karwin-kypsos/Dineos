from decimal import Decimal

import pytest
from django.utils import timezone

from apps.inventory.models import Ingredient, PurchaseOrder, PurchaseOrderLine
from apps.menu.models import PreparedPortion
from apps.orders import services as order_services
from apps.restaurant.models import Branch
from apps.tables import services as table_services
from apps.tables.models import Table

pytestmark = pytest.mark.django_db


def _manager_on_branch(manager_client, branch):
    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])
    return user, client


def test_manager_without_branch_gets_400(manager_client):
    _, client = manager_client
    response = client.get("/v1/manager/dashboard/")
    assert response.status_code == 400


def test_admin_cannot_use_manager_dashboard(admin_client):
    _, client = admin_client
    response = client.get("/v1/manager/dashboard/")
    assert response.status_code == 403


def test_manager_dashboard_scoped_to_own_branch_only(restaurant, manager_client, branch, menu_item):
    """The headline requirement: a Manager must never see another
    branch's data, and the response must be their own branch's numbers,
    not the whole restaurant's."""
    _, client = _manager_on_branch(manager_client, branch)

    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    table_a = Table.objects.create(restaurant=restaurant, branch=branch, table_number="A1", capacity=4)
    Table.objects.create(restaurant=restaurant, branch=other_branch, table_number="B1", capacity=4)

    session, _ = table_services.get_or_create_active_session(table_a.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.get("/v1/manager/dashboard/")

    assert response.status_code == 200
    assert response.data["branch_id"] == str(branch.id)
    assert response.data["today_orders_count"] == 1
    assert response.data["table_overview"]["total"] == 1  # only this branch's table


def test_manager_dashboard_stock_status_breakdown(restaurant, manager_client, branch):
    _, client = _manager_on_branch(manager_client, branch)

    Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Chicken", unit="KG",
        current_stock=Decimal("0"), minimum_stock_level=Decimal("5"),
    )
    Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Rice", unit="KG",
        current_stock=Decimal("3"), minimum_stock_level=Decimal("10"),
    )
    Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Oil", unit="L",
        current_stock=Decimal("20"), minimum_stock_level=Decimal("5"),
    )

    response = client.get("/v1/manager/dashboard/")

    assert response.status_code == 200
    assert response.data["stock_status"] == {"critical": 1, "low": 1, "healthy": 1}
    restocking_names = {row["name"] for row in response.data["needs_restocking"]}
    assert restocking_names == {"Chicken", "Rice"}
    chicken_row = next(r for r in response.data["needs_restocking"] if r["name"] == "Chicken")
    assert Decimal(str(chicken_row["restock_quantity_needed"])) == Decimal("5")


def test_manager_dashboard_excludes_other_branch_ingredients(restaurant, manager_client, branch):
    _, client = _manager_on_branch(manager_client, branch)
    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    Ingredient.objects.create(
        restaurant=restaurant, branch=other_branch, name="Other Branch Item", unit="KG",
        current_stock=Decimal("0"), minimum_stock_level=Decimal("5"),
    )

    response = client.get("/v1/manager/dashboard/")

    assert response.data["stock_status"] == {"critical": 0, "low": 0, "healthy": 0}
    assert response.data["needs_restocking"] == []


def test_manager_dashboard_prepared_dishes_needing_attention(restaurant, manager_client, branch, menu_item):
    _, client = _manager_on_branch(manager_client, branch)
    menu_item.category.branch = branch
    menu_item.category.save(update_fields=["branch"])

    PreparedPortion.objects.filter(menu_item=menu_item, date=timezone.localdate()).update(
        portions_initial=20, portions_remaining=4,
    )

    response = client.get("/v1/manager/dashboard/")

    assert response.status_code == 200
    names = {row["menu_item_name"] for row in response.data["prepared_dishes_needing_attention"]}
    assert menu_item.name in names
    row = next(r for r in response.data["prepared_dishes_needing_attention"] if r["menu_item_name"] == menu_item.name)
    assert row["portions_remaining"] == 4
    assert row["portions_initial"] == 20


def test_manager_dashboard_prepared_dishes_healthy_not_included(restaurant, manager_client, branch, menu_item):
    _, client = _manager_on_branch(manager_client, branch)
    menu_item.category.branch = branch
    menu_item.category.save(update_fields=["branch"])

    PreparedPortion.objects.filter(menu_item=menu_item, date=timezone.localdate()).update(
        portions_initial=20, portions_remaining=18,
    )

    response = client.get("/v1/manager/dashboard/")

    names = {row["menu_item_name"] for row in response.data["prepared_dishes_needing_attention"]}
    assert menu_item.name not in names


def test_manager_dashboard_only_shows_approved_purchase_orders(restaurant, manager_client, branch):
    _, client = _manager_on_branch(manager_client, branch)
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Chicken", unit="KG",
        current_stock=Decimal("2"), minimum_stock_level=Decimal("5"),
    )

    pending_po = PurchaseOrder.objects.create(restaurant=restaurant, branch=branch, status="PENDING")
    PurchaseOrderLine.objects.create(purchase_order=pending_po, ingredient=ingredient, quantity_ordered=Decimal("5"))

    approved_po = PurchaseOrder.objects.create(restaurant=restaurant, branch=branch, status="APPROVED")
    PurchaseOrderLine.objects.create(purchase_order=approved_po, ingredient=ingredient, quantity_ordered=Decimal("10"))

    response = client.get("/v1/manager/dashboard/")

    ids = {po["id"] for po in response.data["purchase_orders_approved"]}
    assert str(approved_po.id) in ids
    assert str(pending_po.id) not in ids
