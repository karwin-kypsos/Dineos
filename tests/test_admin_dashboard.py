from decimal import Decimal

import pytest

from apps.billing import services as billing_services
from apps.inventory.models import Ingredient, PurchaseOrder
from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def test_server_cannot_view_dashboard(server_client):
    _, client = server_client
    response = client.get("/v1/admin/dashboard/")
    assert response.status_code == 403


def test_dashboard_counts_dine_in_order_and_revenue(admin_client, table, menu_item):
    _, client = admin_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    billing_services.pay_bill(session.id, "CASH", None)

    response = client.get("/v1/admin/dashboard/")

    assert response.status_code == 200
    assert response.data["today_orders_count"] == 1
    assert response.data["today_bills_count"] == 1
    # Restaurant fixture's default 5% GST is added on top of the item price.
    expected_total = (menu_item.price * Decimal("1.05")).quantize(Decimal("0.01"))
    assert Decimal(str(response.data["today_revenue"])) == expected_total


def test_dashboard_counts_takeaway_order_and_revenue(admin_client, manager_client, branch, menu_item):
    _, client = admin_client
    manager_user, _ = manager_client
    manager_user.branch = branch
    manager_user.save(update_fields=["branch"])

    order = order_services.place_takeaway_order(
        manager_user.restaurant, branch, [{"menu_item_id": menu_item.id, "quantity": 2}], placed_by=manager_user,
    )
    billing_services.pay_takeaway_bill(order.id, "UPI", manager_user)

    response = client.get("/v1/admin/dashboard/")

    assert response.status_code == 200
    assert response.data["today_orders_count"] == 1
    assert response.data["today_bills_count"] == 1


def test_dashboard_low_stock_and_pending_po_counts(admin_client, restaurant):
    _, client = admin_client
    Ingredient.objects.create(
        restaurant=restaurant, name="Salt", unit="KG", current_stock=Decimal("1.00"), minimum_stock_level=Decimal("5.00"),
    )
    Ingredient.objects.create(
        restaurant=restaurant, name="Sugar", unit="KG", current_stock=Decimal("20.00"), minimum_stock_level=Decimal("5.00"),
    )
    PurchaseOrder.objects.create(restaurant=restaurant, status=PurchaseOrder.Status.PENDING)
    PurchaseOrder.objects.create(restaurant=restaurant, status=PurchaseOrder.Status.RECEIVED)

    response = client.get("/v1/admin/dashboard/")

    assert response.status_code == 200
    assert response.data["low_stock_count"] == 1
    assert response.data["pending_purchase_orders_count"] == 1


def test_dashboard_branch_filter_excludes_other_branches(admin_client, restaurant, menu_item):
    _, client = admin_client

    from apps.restaurant.models import Branch

    branch_a = Branch.objects.create(restaurant=restaurant, name="Branch A")
    branch_b = Branch.objects.create(restaurant=restaurant, name="Branch B")
    Ingredient.objects.create(restaurant=restaurant, branch=branch_a, name="A-only", unit="KG",
                               current_stock=Decimal("0"), minimum_stock_level=Decimal("5"))
    Ingredient.objects.create(restaurant=restaurant, branch=branch_b, name="B-only", unit="KG",
                               current_stock=Decimal("0"), minimum_stock_level=Decimal("5"))

    response = client.get(f"/v1/admin/dashboard/?branch={branch_a.id}")

    assert response.status_code == 200
    assert response.data["low_stock_count"] == 1


def test_dashboard_isolated_across_restaurants(admin_client, table, menu_item):
    """Another restaurant's paid orders must never inflate this dashboard."""
    _, client = admin_client

    from apps.menu.models import Category, MenuItem
    from apps.restaurant.models import Restaurant
    from apps.tables.models import Table

    foreign_restaurant = Restaurant.objects.create(name="Foreign Dash", slug="foreign-dash")
    foreign_table = Table.objects.create(restaurant=foreign_restaurant, table_number="1")
    foreign_category = Category.objects.create(restaurant=foreign_restaurant, name="Mains")
    foreign_item = MenuItem.objects.create(category=foreign_category, name="Foreign Dish", price=Decimal("100.00"))
    foreign_session, _ = table_services.get_or_create_active_session(foreign_table.id)
    order_services.place_order(foreign_session.id, [{"menu_item_id": foreign_item.id, "quantity": 1}])
    billing_services.pay_bill(foreign_session.id, "CASH", None)

    response = client.get("/v1/admin/dashboard/")

    assert response.status_code == 200
    assert response.data["today_orders_count"] == 0
    assert response.data["today_bills_count"] == 0
