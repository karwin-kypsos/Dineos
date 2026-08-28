import pytest
from django.utils import timezone

from apps.billing import services as billing_services
from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def _pay(cashier_user, table, menu_item, quantity=1, method="CASH"):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": quantity}])
    return billing_services.pay_bill(session.id, method, cashier_user)


@pytest.fixture
def branch_a(restaurant):
    from apps.restaurant.models import Branch

    return Branch.objects.create(restaurant=restaurant, name="Branch A")


@pytest.fixture
def branch_b(restaurant):
    from apps.restaurant.models import Branch

    return Branch.objects.create(restaurant=restaurant, name="Branch B")


def test_billing_summary_returns_totals_and_revenue_by_hour(manager_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response = manager.get("/v1/billing/summary/")

    assert response.status_code == 200
    assert response.data["total_orders"] == 1
    assert float(response.data["total_revenue"]) > 0
    assert len(response.data["revenue_by_hour"]) == 24
    assert sum(float(h["amount"]) for h in response.data["revenue_by_hour"]) == float(response.data["total_revenue"])


def test_billing_summary_scoped_to_branch(manager_client, cashier_client, restaurant, menu_item, branch_a, branch_b):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    table_a = Table.objects.create(restaurant=restaurant, branch=branch_a, table_number="A1", capacity=4)
    table_b = Table.objects.create(restaurant=restaurant, branch=branch_b, table_number="B1", capacity=4)
    _pay(cashier_user, table_a, menu_item, quantity=1, method="CASH")
    _pay(cashier_user, table_b, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response_a = manager.get(f"/v1/billing/summary/?branch={branch_a.id}")
    response_b = manager.get(f"/v1/billing/summary/?branch={branch_b.id}")

    assert response_a.data["total_orders"] == 1
    assert response_b.data["total_orders"] == 1
    assert response_a.data["total_revenue"] == response_b.data["total_revenue"]


def test_billing_summary_date_range(manager_client, cashier_client, table, menu_item):
    from datetime import timedelta

    cashier_user, _ = cashier_client
    old_bill = _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    old_bill.paid_at = old_bill.paid_at - timedelta(days=10)
    old_bill.save(update_fields=["paid_at"])

    _, manager = manager_client
    today = timezone.localdate()
    response = manager.get(
        f"/v1/billing/summary/?from_date={(today - timedelta(days=1)).isoformat()}&to_date={today.isoformat()}"
    )
    assert response.status_code == 200
    assert response.data["total_orders"] == 0

    wide_response = manager.get(
        f"/v1/billing/summary/?from_date={(today - timedelta(days=15)).isoformat()}&to_date={today.isoformat()}"
    )
    assert wide_response.data["total_orders"] == 1


def test_billing_floor_status_shows_active_paid_and_free(manager_client, cashier_client, restaurant, menu_item):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    active_table = Table.objects.create(restaurant=restaurant, table_number="FS-Active", capacity=4)
    paid_table = Table.objects.create(restaurant=restaurant, table_number="FS-Paid", capacity=4)
    free_table = Table.objects.create(restaurant=restaurant, table_number="FS-Free", capacity=4)

    table_services.get_or_create_active_session(active_table.id)
    _pay(cashier_user, paid_table, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response = manager.get("/v1/billing/floor-status/")

    assert response.status_code == 200
    by_name = {row["table_name"]: row for row in response.data}
    assert by_name["FS-Active"]["status"] == "ACTIVE"
    assert by_name["FS-Paid"]["status"] == "PAID"
    assert by_name["FS-Paid"]["amount"] is not None
    assert by_name["FS-Free"]["status"] == "FREE"


def test_billing_payment_split_matches_breakdown(manager_client, cashier_client, restaurant, menu_item):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    table1 = Table.objects.create(restaurant=restaurant, table_number="PS1", capacity=4)
    table2 = Table.objects.create(restaurant=restaurant, table_number="PS2", capacity=4)
    _pay(cashier_user, table1, menu_item, quantity=1, method="CASH")
    _pay(cashier_user, table2, menu_item, quantity=1, method="CARD")

    _, manager = manager_client
    response = manager.get("/v1/billing/payment-split/")

    assert response.status_code == 200
    assert response.data["cash"]["percentage"] == 50.0
    assert response.data["card"]["percentage"] == 50.0
    assert response.data["upi"]["percentage"] == 0.0


def test_billing_cashiers_list_reshapes_fields(manager_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response = manager.get("/v1/billing/cashiers/")

    assert response.status_code == 200
    assert len(response.data) == 1
    row = response.data[0]
    assert row["user_id"] == cashier_user.id
    assert row["user_name"] == cashier_user.name
    assert row["role"] == "Cashier"
    assert row["status"] == "Not submitted"
    assert row["tables_served"] == 1


def test_billing_cashier_detail_aggregates_across_shifts(manager_client, cashier_client, table, menu_item):
    cashier_user, client = cashier_client

    shift1 = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    expected1 = billing_services.shift_totals_by_method(shift1)["cash"]
    client.post(f"/v1/cashier/shifts/{shift1.id}/close/", {"counted_cash": str(expected1)}, format="json")

    from apps.tables.models import Table

    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="CD2")
    shift2 = billing_services.open_shift(cashier_user)
    _pay(cashier_user, other_table, menu_item, quantity=1, method="CARD")

    _, manager = manager_client
    response = manager.get(f"/v1/billing/cashiers/{cashier_user.id}/")

    assert response.status_code == 200
    assert response.data["cashier"]["id"] == cashier_user.id
    assert response.data["tables_served"] == 2
    assert float(response.data["payment_split"]["cash"]["amount"]) > 0
    assert float(response.data["payment_split"]["card"]["amount"]) > 0
    # shift1 closed+matched, shift2 still open -> "Pending", not a clean match
    assert response.data["cash_reconciliation"]["status"] == "Pending"


def test_billing_cashier_bills_paginated(manager_client, cashier_client, restaurant, menu_item):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    for i in range(12):
        t = Table.objects.create(restaurant=restaurant, table_number=f"PG{i}", capacity=4)
        _pay(cashier_user, t, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    page1 = manager.get(f"/v1/billing/cashiers/{cashier_user.id}/bills/")

    assert page1.status_code == 200
    assert page1.data["count"] == 12
    assert len(page1.data["results"]) == 10
    assert page1.data["next"] is not None
    assert page1.data["previous"] is None

    page2 = manager.get(f"/v1/billing/cashiers/{cashier_user.id}/bills/?page=2")
    assert len(page2.data["results"]) == 2
    assert page2.data["next"] is None


def test_billing_cashier_bills_search_by_amount(manager_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    bill = _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response = manager.get(f"/v1/billing/cashiers/{cashier_user.id}/bills/?search={int(bill.total_amount)}")

    assert response.status_code == 200
    ids = {b["id"] for b in response.data["results"]}
    assert str(bill.id) in ids


def test_billing_endpoints_require_staff_auth(api_client):
    response = api_client.get("/v1/billing/summary/")
    assert response.status_code == 401
