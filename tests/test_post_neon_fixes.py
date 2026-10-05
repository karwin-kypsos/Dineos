"""Fixes from the post-Neon live check (2026-10-05, per Karwin: "fix everything").

- Lists ordered by name ignore case, on any database (Neon sorts byte-wise).
- Bad input answers 400/404 instead of 500.
- Deleting an organization with order/purchase history works.
- No shared, public reset token or 2FA code; forgot-password never hands
  the reset token to the requester outside DEBUG.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.authentication.models import PasswordResetToken, User
from apps.inventory import services as inventory_services
from apps.inventory.models import Ingredient
from apps.kitchen.models import KDSDevice
from apps.menu.models import Category, MenuItem
from apps.orders import services as order_services
from apps.platform.models import PlatformAdmin, PlatformLoginCode
from apps.restaurant.models import Branch, Restaurant
from apps.tables import services as table_services
from apps.tables.models import Table

pytestmark = pytest.mark.django_db


def _platform_client():
    PlatformAdmin.objects.create_user(email="super@fixes.test", password="Test@1234")
    client = APIClient()
    login = client.post("/platform/auth/login/", {"email": "super@fixes.test", "password": "Test@1234"}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": "super@fixes.test", "code": login.data["code"]}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


def _rows(data):
    return data["results"] if isinstance(data, dict) and "results" in data else data


# ---- case-insensitive name ordering ------------------------------------------------

def test_branch_lists_ignore_case(admin_client, restaurant):
    _, client = admin_client
    for name in ("Zeta", "alpha", "Mango"):
        Branch.objects.create(restaurant=restaurant, name=name)

    listed = [b["name"] for b in _rows(client.get("/v1/branches/").data)]
    dashboard = [b["name"] for b in client.get("/v1/admin/dashboard/").data["branches"]]
    me = client.get("/v1/auth/me/").data

    assert listed == dashboard == ["alpha", "Mango", "Zeta"]
    assert [b["name"] for b in me["available_branches"]] == ["alpha", "Mango", "Zeta"]
    assert me["branch"]["name"] == "alpha"  # the default is the first A-Z


def test_stock_menu_and_devices_ignore_case(admin_client, restaurant):
    _, client = admin_client
    for name in ("Zucchini", "apple", "Mint"):
        Ingredient.objects.create(restaurant=restaurant, name=name, unit="KG")
    category = Category.objects.create(restaurant=restaurant, name="Drinks", sort_order=1)
    for name in ("Zesty Lime", "apple juice", "Mojito"):
        MenuItem.objects.create(category=category, name=name, price=Decimal("10"), sort_order=1)
    for label in ("Zeta tab", "alpha tab"):
        KDSDevice.objects.create(restaurant=restaurant, label=label)

    assert [i["name"] for i in _rows(client.get("/v1/inventory/ingredients/").data)] == ["apple", "Mint", "Zucchini"]
    assert [i["name"] for i in _rows(client.get("/v1/menu/all/").data)] == ["apple juice", "Mojito", "Zesty Lime"]
    assert [d["label"] for d in _rows(client.get("/v1/kitchen/devices/").data)] == ["alpha tab", "Zeta tab"]


# ---- 400 instead of 500 -----------------------------------------------------------

def test_branch_managers_duplicate_ingredient_and_category_are_400(manager_client, restaurant, branch):
    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    first = client.post("/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG"}, format="json")
    dup_ingredient = client.post("/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG"}, format="json")
    client.post("/v1/menu/categories/", {"name": "Mains", "sort_order": 1}, format="json")
    dup_category = client.post("/v1/menu/categories/", {"name": "Mains", "sort_order": 2}, format="json")

    assert first.status_code == 201
    assert dup_ingredient.status_code == 400
    assert dup_category.status_code == 400


@pytest.mark.parametrize("path", [
    "/v1/bills/?date=2026-13-45",
    "/v1/bills/?date_from=2026-02-30",
    "/v1/cashier/collections/daily/?date=nope",
    "/v1/cashier/collections/daily/?date_from=2026-01-01&date_to=2026-99-01",
    "/v1/cashier/collections/by-cashier/?date=2026-13-45",
    "/v1/orders/takeaway/?date_from=garbage",
])
def test_impossible_dates_are_400(admin_client, path):
    _, client = admin_client

    response = client.get(path)

    assert response.status_code == 400, response.content[:200]


def test_unknown_ids_are_404(admin_client, api_client):
    _, client = admin_client

    order = client.get("/v1/orders/00000000-0000-0000-0000-000000000000/")
    notification = client.patch("/v1/notifications/999999/read/")
    customer_order = api_client.post(
        "/v1/orders/", {"session_id": "00000000-0000-0000-0000-000000000000", "items": [{"menu_item": 1, "quantity": 1}]},
        format="json",
    )

    assert order.status_code == 404
    assert notification.status_code == 404
    assert customer_order.status_code in (400, 404)  # an unknown dish id may be caught first


def test_order_for_an_unknown_session_is_404(api_client, menu_item):
    response = api_client.post(
        "/v1/orders/", {"session_id": "00000000-0000-0000-0000-000000000000", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 404


# ---- deleting an organization with history ----------------------------------------

def test_delete_organization_with_orders_and_purchase_orders(restaurant, table, menu_item):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    ingredient = Ingredient.objects.create(restaurant=restaurant, name="Salt", unit="KG")
    inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None, lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("1")}],
    )
    platform = _platform_client()

    response = platform.delete(f"/platform/tenants/{restaurant.id}/?confirm=true")

    assert response.status_code == 204
    assert not Restaurant.objects.filter(id=restaurant.id).exists()
    assert not MenuItem.objects.filter(id=menu_item.id).exists()
    assert not Table.objects.filter(id=table.id).exists()


# ---- no shared secrets ------------------------------------------------------------

def test_forgot_password_never_returns_the_token_and_tokens_are_random(api_client, restaurant, settings):
    settings.DEBUG = False  # as in production (dineos.settings.production)
    user =User.objects.create_user(email="victim@test.dineos", password="Test@1234", role="SERVER", name="V", restaurant=restaurant)

    first = api_client.post("/v1/auth/forgot-password/", {"email": user.email}, format="json")
    second = api_client.post("/v1/auth/forgot-password/", {"email": user.email}, format="json")

    assert first.status_code == second.status_code == 200
    assert "token" not in first.data
    tokens = list(PasswordResetToken.objects.filter(user=user).values_list("token", flat=True))
    assert len(set(tokens)) == 2 and all(len(t) >= 32 and t != "COMMON-TEST-TOKEN" for t in tokens)
    # The real (emailed) token still works.
    reset = api_client.post("/v1/auth/reset-password/", {"token": tokens[0], "new_password": "Brand-new-9"}, format="json")
    assert reset.status_code == 200
    assert api_client.post("/v1/auth/reset-password/", {"token": "COMMON-TEST-TOKEN", "new_password": "Hijack-123"}, format="json").status_code == 400


def test_super_admin_2fa_codes_are_random_once_email_delivery_is_on(settings):
    settings.EMAIL_DELIVERY_ENABLED = True
    admin = PlatformAdmin.objects.create_user(email="random@fixes.test", password="Test@1234")

    codes = {PlatformLoginCode.issue(admin).code for _ in range(5)}

    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 1  # five fixed "123456"s would collapse to one


def test_super_admin_2fa_code_is_123456_while_email_delivery_is_off(settings):
    """Put back the same day, per Karwin: with email off the login response
    echoes the code anyway, and the Super Admin screen doesn't show it."""
    settings.EMAIL_DELIVERY_ENABLED = False
    admin = PlatformAdmin.objects.create_user(email="fixed@fixes.test", password="Test@1234")

    assert PlatformLoginCode.issue(admin).code == "123456"
