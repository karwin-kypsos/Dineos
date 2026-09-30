"""customer_ordering_enabled and server_staff_enabled (2026-09-30, per Shereena):
two more per-restaurant flags beside billing_enabled, controlled from Super
Admin, both off in the Starter plan."""
import pytest
from rest_framework.test import APIClient

from apps.authentication.models import User
from apps.orders import services as order_services
from apps.platform.models import PlatformAdmin
from apps.tables import services as table_services
from core.permissions import CUSTOMER_ORDERING_OFF

pytestmark = pytest.mark.django_db

NEW_FLAGS = ("customer_ordering_enabled", "server_staff_enabled")


def _platform_client():
    PlatformAdmin.objects.create_user(email="super@flags.test", password="Test@1234")
    client = APIClient()
    login = client.post("/platform/auth/login/", {"email": "super@flags.test", "password": "Test@1234"}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": "super@flags.test", "code": login.data["code"]}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


def _switch_off(restaurant, *flags):
    for flag in flags:
        setattr(restaurant, flag, False)
    restaurant.save(update_fields=list(flags))


# ---- plans, presets, where the flags are read -------------------------------------

def test_starter_plan_turns_both_off_and_growth_keeps_them():
    client = _platform_client()

    starter = client.post("/platform/tenants/", {"name": "Starter Co", "slug": "starter-co", "plan_tier": "STARTER"}, format="json")
    growth = client.post("/platform/tenants/", {"name": "Growth Co", "slug": "growth-co", "plan_tier": "GROWTH"}, format="json")

    assert starter.status_code == growth.status_code == 201
    assert all(starter.data[f] is False for f in NEW_FLAGS)
    assert all(growth.data[f] is True for f in NEW_FLAGS)


def test_plans_list_describes_both_flags(api_client):
    plans = {p["id"]: p for p in api_client.get("/v1/auth/plans/").data}

    for tier, included in (("STARTER", False), ("GROWTH", True), ("ENTERPRISE", True)):
        features = {f["key"]: f for f in plans[tier]["features"]}
        for flag in NEW_FLAGS:
            assert features[flag]["included"] is included
            assert features[flag]["label"] and features[flag]["description"]


def test_existing_restaurants_keep_both_on(admin_client):
    _, client = admin_client

    restaurant = client.get("/v1/auth/me/").data["restaurant"]

    assert all(restaurant[f] is True for f in NEW_FLAGS)


def test_super_admin_toggles_both_and_me_reports_it(admin_client, restaurant):
    _, admin = admin_client
    platform = _platform_client()

    response = platform.patch(
        f"/platform/tenants/{restaurant.id}/feature-flags/",
        {"customer_ordering_enabled": False, "server_staff_enabled": False}, format="json",
    )

    assert response.status_code == 200, response.data
    me = admin.get("/v1/auth/me/").data["restaurant"]
    assert me["customer_ordering_enabled"] is False
    assert me["server_staff_enabled"] is False


# ---- server_staff_enabled -------------------------------------------------------

def test_server_accounts_refused_when_the_flag_is_off(admin_client, restaurant, branch):
    _, client = admin_client
    existing_server = User.objects.create_user(
        email="old-server@test.dineos", password="Test@1234", role="SERVER", name="Old Server", restaurant=restaurant,
    )
    cashier = User.objects.create_user(
        email="cashier-x@test.dineos", password="Test@1234", role="CASHIER", name="Cashier X", restaurant=restaurant,
    )
    _switch_off(restaurant, "server_staff_enabled")

    new_server = client.post("/v1/staff/", {"email": "s@test.dineos", "role": "SERVER", "name": "S", "branch": str(branch.id)}, format="json")
    new_cashier = client.post("/v1/staff/", {"email": "c@test.dineos", "role": "CASHIER", "name": "C", "branch": str(branch.id)}, format="json")
    promote = client.patch(f"/v1/staff/{cashier.id}/", {"role": "SERVER"}, format="json")
    rename = client.patch(f"/v1/staff/{existing_server.id}/", {"name": "Renamed", "role": "SERVER"}, format="json")

    assert new_server.status_code == 400
    assert new_server.data["role"] == ["Server staff accounts are not enabled for this restaurant."]
    assert not User.objects.filter(email="s@test.dineos").exists()
    assert new_cashier.status_code == 201
    assert promote.status_code == 400
    # An existing Server keeps working and can still be edited.
    assert rename.status_code == 200, rename.data


def test_server_accounts_allowed_when_the_flag_is_on(admin_client, branch):
    _, client = admin_client

    response = client.post("/v1/staff/", {"email": "s2@test.dineos", "role": "SERVER", "name": "S2", "branch": str(branch.id)}, format="json")

    assert response.status_code == 201, response.data


# ---- customer_ordering_enabled ------------------------------------------------------

def test_customer_cannot_seat_or_order_when_the_flag_is_off(api_client, server_client, restaurant, table, menu_item):
    _, staff = server_client
    _switch_off(restaurant, "customer_ordering_enabled")

    seat = api_client.post(f"/v1/tables/{table.id}/session/")
    assert seat.status_code == 403
    assert seat.data["detail"] == CUSTOMER_ORDERING_OFF
    table.refresh_from_db()
    assert table.status == "AVAILABLE"  # a refused customer never occupies the table

    # Staff seat the table and take the order as usual.
    staff_seat = staff.post(f"/v1/tables/{table.id}/session/")
    assert staff_seat.status_code == 201
    session_id = staff_seat.data["id"]
    body = {"session_id": session_id, "items": [{"menu_item": menu_item.id, "quantity": 1}]}

    assert api_client.post("/v1/orders/", body, format="json").status_code == 403
    assert staff.post("/v1/orders/", body, format="json").status_code == 201


def test_customer_orders_as_before_when_the_flag_is_on(api_client, table, menu_item):
    seat = api_client.post(f"/v1/tables/{table.id}/session/")
    order = api_client.post(
        "/v1/orders/", {"session_id": seat.data["id"], "items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )

    assert seat.status_code == 201
    assert order.status_code == 201, order.data


def test_customer_can_still_read_the_menu_and_see_the_flags(api_client, restaurant, table, menu_item):
    _switch_off(restaurant, "customer_ordering_enabled")

    landing = api_client.get(f"/v1/tables/qr/{restaurant.slug}/{table.table_number}/")
    menu = api_client.get(f"/v1/menu/customer/{table.id}/")

    assert landing.status_code == 200
    assert landing.data["features"]["customer_ordering_enabled"] is False
    assert set(landing.data["features"]) >= {"billing_enabled", "kitchen_enabled", *NEW_FLAGS}
    assert menu.status_code == 200


def test_existing_customer_session_can_still_ask_for_the_bill(api_client, restaurant, table, menu_item):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    _switch_off(restaurant, "customer_ordering_enabled")

    response = api_client.post(f"/v1/tables/{table.id}/bill-request/")

    # Only ordering is locked: a table staff seated can still ask for the bill.
    assert response.status_code == 200
