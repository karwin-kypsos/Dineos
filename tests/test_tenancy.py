from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.serializers import DineOSTokenObtainPairSerializer
from apps.menu.models import Category, MenuItem, PreparedPortion
from apps.orders import services as order_services
from apps.platform.models import PlatformAdmin
from apps.platform.serializers import issue_platform_access_token
from apps.restaurant.models import Restaurant
from apps.tables import services as table_services
from apps.tables.models import Table

pytestmark = pytest.mark.django_db

User = get_user_model()


def _make_tenant_with_staff(slug):
    restaurant = Restaurant.objects.create(name=slug.title(), slug=slug)
    manager = User.objects.create_user(
        email=f"manager@{slug}.test", password="Test@1234", role="MANAGER", restaurant=restaurant
    )
    client = APIClient()
    token = DineOSTokenObtainPairSerializer.get_token(manager)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return restaurant, manager, client


def _make_menu_item(restaurant, name="Dish"):
    category = Category.objects.create(restaurant=restaurant, name=f"{name} Category")
    item = MenuItem.objects.create(category=category, name=name, price=Decimal("100.00"))
    PreparedPortion.objects.create(menu_item=item, date=timezone.localdate(), portions_initial=10, portions_remaining=10)
    return item


def test_staff_cannot_see_another_restaurants_tables():
    restaurant_a, _, client_a = _make_tenant_with_staff("tenant-a")
    restaurant_b, _, _ = _make_tenant_with_staff("tenant-b")
    Table.objects.create(restaurant=restaurant_a, table_number="1")
    Table.objects.create(restaurant=restaurant_b, table_number="1")  # same number, different tenant — allowed

    response = client_a.get("/v1/tables/")
    results = response.data.get("results", response.data)
    assert len(results) == 1  # only restaurant A's table, despite both existing


def test_staff_cannot_view_another_restaurants_menu_item():
    restaurant_a, _, client_a = _make_tenant_with_staff("tenant-a")
    restaurant_b, _, _ = _make_tenant_with_staff("tenant-b")
    item_b = _make_menu_item(restaurant_b, "Rival Dish")

    response = client_a.get(f"/v1/menu/{item_b.id}/")
    assert response.status_code == 404


def test_manager_cannot_update_another_restaurants_menu_item():
    restaurant_a, _, client_a = _make_tenant_with_staff("tenant-a")
    restaurant_b, _, _ = _make_tenant_with_staff("tenant-b")
    item_b = _make_menu_item(restaurant_b, "Rival Dish")

    response = client_a.put(
        f"/v1/menu/{item_b.id}/",
        {"category": item_b.category_id, "name": "Hijacked", "price": "1.00"},
        format="json",
    )
    assert response.status_code == 404

    item_b.refresh_from_db()
    assert item_b.name == "Rival Dish"


def test_manager_cannot_delete_another_restaurants_menu_item():
    restaurant_a, _, client_a = _make_tenant_with_staff("tenant-a")
    restaurant_b, _, _ = _make_tenant_with_staff("tenant-b")
    item_b = _make_menu_item(restaurant_b)

    response = client_a.delete(f"/v1/menu/{item_b.id}/")
    assert response.status_code == 404
    assert MenuItem.objects.filter(id=item_b.id).exists()


def test_staff_cannot_place_order_into_another_restaurants_session():
    restaurant_a, _, client_a = _make_tenant_with_staff("tenant-a")
    restaurant_b, _, _ = _make_tenant_with_staff("tenant-b")
    table_b = Table.objects.create(restaurant=restaurant_b, table_number="1")
    session_b, _ = table_services.get_or_create_active_session(table_b.id)
    item_b = _make_menu_item(restaurant_b)

    response = client_a.post(
        "/v1/orders/",
        {"session_id": str(session_b.id), "items": [{"menu_item": item_b.id, "quantity": 1}]},
        format="json",
    )
    assert response.status_code == 403


def test_notify_role_only_notifies_the_specified_tenants_staff():
    from apps.notifications.models import Notification
    from apps.notifications.services import notify_role

    restaurant_a, manager_a, _ = _make_tenant_with_staff("tenant-a")
    restaurant_b, manager_b, _ = _make_tenant_with_staff("tenant-b")

    notify_role(["MANAGER"], tenant=restaurant_a, type="ORDER_READY", title="Test")

    assert Notification.objects.filter(recipient=manager_a).count() == 1
    assert Notification.objects.filter(recipient=manager_b).count() == 0


def test_kitchen_disabled_tenant_orders_go_straight_to_served():
    restaurant, _, _ = _make_tenant_with_staff("no-kitchen-tenant")
    restaurant.kitchen_enabled = False
    restaurant.save()
    table = Table.objects.create(restaurant=restaurant, table_number="1")
    session, _ = table_services.get_or_create_active_session(table.id)
    item = _make_menu_item(restaurant)

    order = order_services.place_order(session.id, [{"menu_item_id": item.id, "quantity": 1}])
    assert order.status == "SERVED"


def test_kitchen_enabled_tenant_orders_start_new():
    restaurant, _, _ = _make_tenant_with_staff("with-kitchen-tenant")
    table = Table.objects.create(restaurant=restaurant, table_number="1")
    session, _ = table_services.get_or_create_active_session(table.id)
    item = _make_menu_item(restaurant)

    order = order_services.place_order(session.id, [{"menu_item_id": item.id, "quantity": 1}])
    assert order.status == "NEW"


def test_billing_disabled_tenant_gets_403_on_billing_endpoints():
    restaurant, _, client = _make_tenant_with_staff("no-billing-tenant")
    restaurant.billing_enabled = False
    restaurant.save()
    table = Table.objects.create(restaurant=restaurant, table_number="1")
    session, _ = table_services.get_or_create_active_session(table.id)

    response = client.get(f"/v1/bills/session/{session.id}/")
    assert response.status_code == 403


def test_platform_admin_login_and_tenant_management():
    PlatformAdmin.objects.create_user(email="super@platform.test", password="Test@1234", name="Super")
    client = APIClient()

    login = client.post("/platform/auth/login/", {"email": "super@platform.test", "password": "Test@1234"}, format="json")
    assert login.status_code == 200
    assert "restaurant_id" not in login.data

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
    create = client.post("/platform/tenants/", {"name": "New Client", "slug": "new-client"}, format="json")
    assert create.status_code == 201

    toggle = client.patch(f"/platform/tenants/{create.data['id']}/", {"kitchen_enabled": False}, format="json")
    assert toggle.status_code == 200
    assert toggle.data["kitchen_enabled"] is False


def test_restaurant_staff_token_cannot_access_platform_endpoints():
    _, _, client = _make_tenant_with_staff("regular-tenant")
    response = client.get("/platform/tenants/")
    assert response.status_code in (401, 403)


def test_cashier_cannot_view_or_close_another_restaurants_shift():
    from apps.billing import services as billing_services

    restaurant_a, _, _ = _make_tenant_with_staff("tenant-a")
    restaurant_b, _, _ = _make_tenant_with_staff("tenant-b")

    cashier_a = User.objects.create_user(
        email="cashier@tenant-a.test", password="Test@1234", role="CASHIER", restaurant=restaurant_a
    )
    cashier_b = User.objects.create_user(
        email="cashier@tenant-b.test", password="Test@1234", role="CASHIER", restaurant=restaurant_b
    )
    shift_b = billing_services.open_shift(cashier_b)

    client_a = APIClient()
    token_a = DineOSTokenObtainPairSerializer.get_token(cashier_a)
    client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {token_a.access_token}")

    response = client_a.get(f"/v1/cashier/shifts/{shift_b.id}/reconciliation/")
    assert response.status_code == 404


def test_platform_token_cannot_authenticate_as_restaurant_staff():
    admin = PlatformAdmin.objects.create_user(email="super2@platform.test", password="Test@1234")
    token = issue_platform_access_token(admin)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    response = client.get("/v1/auth/me/")
    assert response.status_code in (401, 403)
