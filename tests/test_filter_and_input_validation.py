"""Shereena's verdicts on the 2026-09-24 open questions, plus the crashes
found while implementing them.

(b) an unrecognised filter on a money screen must be rejected, not
    silently ignored — and in practice these endpoints did not ignore it,
    they answered 500.
(d) takeaway customer name/phone validated server-side, since the API is
    callable directly by other clients, not only the app.
(e) a per-line quantity cap, as a fat-finger guard.
"""
import pytest

from apps.tables.models import Table

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------- (b)
BAD_PARAMS = ["?date=notadate", "?from_date=xx", "?to_date=2026-13-45", "?branch=notauuid"]
DASHBOARDS = [
    "/v1/billing/summary/",
    "/v1/billing/floor-status/",
    "/v1/billing/payment-split/",
    "/v1/billing/cashiers/",
]


@pytest.mark.parametrize("endpoint", DASHBOARDS)
@pytest.mark.parametrize("query", BAD_PARAMS)
def test_billing_dashboard_rejects_malformed_params(manager_client, endpoint, query):
    """All sixteen of these combinations returned HTTP 500 live on
    2026-09-24 — strptime raised and nothing caught it."""
    _, client = manager_client

    response = client.get(endpoint + query)

    assert response.status_code == 400, (
        endpoint + query + " -> " + str(response.status_code)
    )


@pytest.mark.parametrize("endpoint", DASHBOARDS)
def test_billing_dashboard_still_accepts_good_params(manager_client, endpoint):
    _, client = manager_client
    assert client.get(endpoint + "?date=2026-09-24").status_code == 200
    assert client.get(endpoint).status_code == 200


def test_bill_list_rejects_an_unknown_payment_method(manager_client):
    """It used to return EVERY bill, which a manager reads as "these are
    the ones matching my filter"."""
    _, client = manager_client

    response = client.get("/v1/bills/?payment_method=CAHS")

    assert response.status_code == 400
    assert "payment_method" in response.data


def test_bill_list_rejects_a_malformed_date_and_branch(manager_client):
    _, client = manager_client
    assert client.get("/v1/bills/?date_from=notadate").status_code == 400
    assert client.get("/v1/bills/?branch=notauuid").status_code == 400
    assert client.get("/v1/bills/?cashier=notauuid").status_code == 400


def test_bill_list_still_accepts_valid_filters(manager_client):
    _, client = manager_client
    assert client.get("/v1/bills/").status_code == 200
    assert client.get("/v1/bills/?payment_method=CASH").status_code == 200
    assert client.get("/v1/bills/?payment_method=NETBANKING").status_code == 200
    assert client.get("/v1/bills/?date=today").status_code == 200
    assert client.get("/v1/bills/?date_from=2026-09-01&date_to=2026-09-30").status_code == 200


# ----------------------------------------------------------------- (d)
@pytest.fixture
def cashier_at_branch(cashier_client, branch):
    user, client = cashier_client
    user.branch = branch
    user.save(update_fields=["branch"])
    return user, client


def test_takeaway_requires_a_customer_name(cashier_at_branch, menu_item):
    _, client = cashier_at_branch

    # No customer_name at all - that is the whole point of this test.
    response = client.post(
        "/v1/orders/takeaway/",
        {"items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 400
    assert "customer_name" in response.data


def test_a_later_takeaway_round_does_not_need_the_name_again(cashier_at_branch, menu_item):
    """place_takeaway_order carries the name over from the root order, and
    the app sends only existing_order_id + items for round 2."""
    _, client = cashier_at_branch
    first = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Asha", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert first.status_code == 201

    second = client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": first.data["id"],
         "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert second.status_code == 201, second.data
    assert second.data["customer_name"] == "Asha"


@pytest.mark.parametrize("phone", ["abcdefghij", "not a phone", "12", "!!!!!!!!"])
def test_takeaway_rejects_a_phone_that_is_not_a_phone(cashier_at_branch, menu_item, phone):
    _, client = cashier_at_branch

    response = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Asha", "customer_phone": phone,
         "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 400, phone + " was accepted"


@pytest.mark.parametrize("phone", ["", "9876543210", "+91 98765 43210", "080-4333-2222"])
def test_takeaway_accepts_real_phone_shapes(cashier_at_branch, menu_item, phone):
    _, client = cashier_at_branch

    response = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Asha", "customer_phone": phone,
         "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 201, repr(phone) + " -> " + str(response.data)


# ----------------------------------------------------------------- (e)
@pytest.fixture
def unlimited_item(restaurant):
    """A dish with no PreparedPortion row, so nothing but the cap itself
    can reject a large quantity. The shared menu_item fixture tracks 20
    daily portions, which would reject 999 for an unrelated reason and
    make this test pass without the cap existing at all."""
    from decimal import Decimal

    from apps.menu.models import Category, MenuItem

    category = Category.objects.create(restaurant=restaurant, name="Drinks", sort_order=7)
    return MenuItem.objects.create(category=category, name="Bottled Water", price=Decimal("20.00"))


def test_quantity_is_capped(cashier_at_branch, unlimited_item):
    """9999 on one line rang up an order at Rs 14,99,850 live."""
    _, client = cashier_at_branch

    response = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Asha", "items": [{"menu_item": unlimited_item.id, "quantity": 9999}]},
        format="json",
    )

    assert response.status_code == 400
    assert "quantity" in str(response.data), response.data


def test_quantity_at_the_cap_is_allowed(cashier_at_branch, unlimited_item):
    _, client = cashier_at_branch

    response = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Asha", "items": [{"menu_item": unlimited_item.id, "quantity": 999}]},
        format="json",
    )

    assert response.status_code == 201, response.data


def test_dine_in_quantity_is_capped_too(server_client, table, unlimited_item):
    from apps.tables import services as table_services

    _, client = server_client
    session, _ = table_services.get_or_create_active_session(table.id)

    response = client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": unlimited_item.id, "quantity": 5000}]},
        format="json",
    )

    assert response.status_code == 400
    assert "quantity" in str(response.data), response.data


# ----------------------------------------------------------------- (a)
def test_bill_list_is_paginated(manager_client, cashier_client, restaurant, menu_item):
    """2026-09-24, per Karwin. GET /v1/bills/ used to return every bill the
    restaurant had ever taken in one bare array. Shape is now
    {count, next, previous, results} - the breaking part, scheduled
    deliberately rather than shipped quietly."""
    from apps.billing import services as billing_services
    from apps.tables import services as table_services

    cashier_user, _ = cashier_client
    for index in range(7):
        table = Table.objects.create(
            restaurant=restaurant, table_number="PG" + str(index), capacity=2
        )
        session, _ = table_services.get_or_create_active_session(table.id)
        from apps.orders import services as order_services

        order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
        billing_services.pay_bill(session.id, "CASH", cashier_user)

    _, manager = manager_client
    body = manager.get("/v1/bills/?page_size=3").data

    for key in ("count", "next", "previous", "results"):
        assert key in body, key + " missing from the paginated envelope"
    assert body["count"] == 7
    assert len(body["results"]) == 3
    assert body["next"] is not None
    assert body["previous"] is None


def test_bill_list_pages_do_not_repeat_or_drop_rows(
    manager_client, cashier_client, restaurant, menu_item
):
    """paid_at is not unique, so without a tiebreaker two bills sharing a
    timestamp can swap between pages - a row silently appearing twice or
    not at all. Every bill here is created in the same instant on purpose."""
    from apps.billing import services as billing_services
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    cashier_user, _ = cashier_client
    for index in range(9):
        table = Table.objects.create(
            restaurant=restaurant, table_number="PP" + str(index), capacity=2
        )
        session, _ = table_services.get_or_create_active_session(table.id)
        order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
        billing_services.pay_bill(session.id, "CASH", cashier_user)

    _, manager = manager_client
    seen = []
    for page in (1, 2, 3):
        body = manager.get("/v1/bills/?page_size=3&page=" + str(page)).data
        seen.extend(row["id"] for row in body["results"])

    assert len(seen) == 9
    assert len(set(seen)) == 9, "a bill appeared on more than one page"


def test_bill_list_page_size_is_capped(manager_client):
    _, manager = manager_client
    body = manager.get("/v1/bills/?page_size=9999").data
    assert "results" in body
