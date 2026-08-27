from decimal import Decimal

import pytest

from apps.billing import services as billing_services
from apps.billing.models import CashierShift
from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def _pay(cashier_user, table, menu_item, quantity=1, method="CASH"):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": quantity}])
    return billing_services.pay_bill(session.id, method, cashier_user)


def _with_tax(subtotal):
    # Matches apps.billing.services._compute_totals — restaurant fixture
    # uses the model default 5% GST, 0% service charge.
    return subtotal + (subtotal * Decimal("5.00") / Decimal("100")).quantize(Decimal("0.01"))


def test_open_shift_is_idempotent(cashier_client):
    cashier_user, client = cashier_client

    first = client.post("/v1/cashier/shifts/open/")
    second = client.post("/v1/cashier/shifts/open/")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.data["id"] == second.data["id"]
    assert CashierShift.objects.filter(cashier=cashier_user).count() == 1


def test_current_shift_dashboard_reflects_awaiting_and_active_tables(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    client.post("/v1/cashier/shifts/open/")

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.get("/v1/cashier/shifts/current/")

    assert response.status_code == 200
    assert response.data["active_tables"][0]["table_number"] == table.table_number
    assert response.data["awaiting_payment"] == []
    assert response.data["paid_today_count"] == 0


def test_current_shift_dashboard_includes_item_count_and_elapsed_time(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    client.post("/v1/cashier/shifts/open/")

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 3}])

    response = client.get("/v1/cashier/shifts/current/")

    assert response.status_code == 200
    active_table = response.data["active_tables"][0]
    assert active_table["item_count"] == 3
    assert active_table["elapsed_seconds"] >= 0
    assert active_table["elapsed_formatted"] == "00:00"

    from apps.tables.services import request_bill

    request_bill(session.id)
    response = client.get("/v1/cashier/shifts/current/")
    awaiting_table = response.data["awaiting_payment"][0]
    assert awaiting_table["item_count"] == 3
    assert "elapsed_seconds" in awaiting_table


def test_current_shift_dashboard_scopes_to_cashiers_own_branch(admin_client, cashier_client, branch, restaurant, menu_item):
    from apps.restaurant.models import Branch
    from apps.tables.models import Table

    cashier_user, client = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])
    client.post("/v1/cashier/shifts/open/")

    own_branch_table = Table.objects.create(restaurant=restaurant, branch=branch, table_number="OwnBranch1")
    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    other_branch_table = Table.objects.create(restaurant=restaurant, branch=other_branch, table_number="OtherBranch1")

    session_own, _ = table_services.get_or_create_active_session(own_branch_table.id)
    order_services.place_order(session_own.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    session_other, _ = table_services.get_or_create_active_session(other_branch_table.id)
    order_services.place_order(session_other.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.get("/v1/cashier/shifts/current/")

    assert response.status_code == 200
    table_numbers = {t["table_number"] for t in response.data["active_tables"]}
    assert table_numbers == {"OwnBranch1"}


def test_current_shift_dashboard_shows_collected_today_after_payment(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    client.post("/v1/cashier/shifts/open/")

    _pay(cashier_user, table, menu_item, quantity=2)

    response = client.get("/v1/cashier/shifts/current/")

    assert response.data["paid_today_count"] == 1
    assert Decimal(response.data["collected_today"]) == _with_tax(menu_item.price * 2)


def test_reconciliation_breaks_totals_down_by_payment_method(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)

    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    response = client.get(f"/v1/cashier/shifts/{shift.id}/reconciliation/")

    assert response.status_code == 200
    assert Decimal(response.data["cash"]) == _with_tax(menu_item.price)
    assert Decimal(response.data["card"]) == 0
    assert Decimal(response.data["total"]) == _with_tax(menu_item.price)


def test_close_shift_without_discrepancy_succeeds_directly(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    response = client.post(
        f"/v1/cashier/shifts/{shift.id}/close/", {"counted_cash": str(_with_tax(menu_item.price))}, format="json"
    )

    assert response.status_code == 200
    assert response.data["status"] == "CLOSED"
    assert response.data["discrepancy_acknowledged"] is False
    assert Decimal(response.data["discrepancy_amount"]) == 0
    assert response.data["discrepancy_reason"] == ""


def test_close_shift_with_unacknowledged_discrepancy_is_rejected(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    response = client.post(f"/v1/cashier/shifts/{shift.id}/close/", {"counted_cash": "1.00"}, format="json")

    assert response.status_code == 409
    assert "discrepancy" in response.data

    shift.refresh_from_db()
    assert shift.status == CashierShift.Status.OPEN


def test_close_shift_with_acknowledged_discrepancy_but_no_reason_is_rejected(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    response = client.post(
        f"/v1/cashier/shifts/{shift.id}/close/",
        {"counted_cash": "1.00", "acknowledge_discrepancy": True},
        format="json",
    )

    assert response.status_code == 409
    assert "discrepancy" in response.data

    shift.refresh_from_db()
    assert shift.status == CashierShift.Status.OPEN


def test_close_shift_with_acknowledged_discrepancy_and_reason_succeeds(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    expected_cash = _with_tax(menu_item.price)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    response = client.post(
        f"/v1/cashier/shifts/{shift.id}/close/",
        {"counted_cash": "1.00", "acknowledge_discrepancy": True, "discrepancy_reason": "Miscounted change drawer"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["status"] == "CLOSED"
    assert response.data["discrepancy_acknowledged"] is True
    assert response.data["discrepancy_reason"] == "Miscounted change drawer"
    assert Decimal(response.data["discrepancy_amount"]) == Decimal("1.00") - expected_cash


def test_closing_an_already_closed_shift_returns_409(cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    client.post(
        f"/v1/cashier/shifts/{shift.id}/close/", {"counted_cash": str(_with_tax(menu_item.price))}, format="json"
    )

    response = client.post(
        f"/v1/cashier/shifts/{shift.id}/close/", {"counted_cash": str(_with_tax(menu_item.price))}, format="json"
    )

    assert response.status_code == 409


def test_a_cashier_cannot_close_another_cashiers_shift(cashier_client, restaurant):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    other_cashier = User.objects.create_user(
        email="other-cashier@test.dineos", password="Test@1234", role="CASHIER", restaurant=restaurant
    )
    other_shift = billing_services.open_shift(other_cashier)

    _, client = cashier_client
    response = client.post(f"/v1/cashier/shifts/{other_shift.id}/close/", {"counted_cash": "0.00"}, format="json")

    assert response.status_code == 403


def test_manager_can_view_daily_collections(manager_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/daily/")

    assert response.status_code == 200
    assert response.data["tables_served"] == 1
    assert Decimal(response.data["total_collected"]) == _with_tax(menu_item.price)
    assert Decimal(response.data["payment_breakdown"]["cash"]) == _with_tax(menu_item.price)


def test_daily_collections_tables_count_includes_billed_and_active(manager_client, cashier_client, table, menu_item):
    from apps.tables import services as table_services

    cashier_user, _ = cashier_client
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    from apps.tables.models import Table

    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="ActiveOnly1")
    table_services.get_or_create_active_session(other_table.id)

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/daily/")

    assert response.status_code == 200
    assert response.data["tables_served"] == 1  # billed only
    assert response.data["tables_count"] == 2  # billed (1) + still-active (1)


def test_daily_collections_payment_breakdown_percentages(manager_client, cashier_client, table, menu_item):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="Card1")
    _pay(cashier_user, other_table, menu_item, quantity=1, method="CARD")

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/daily/")

    assert response.status_code == 200
    breakdown = response.data["payment_breakdown"]
    assert breakdown["cash_percentage"] == 50.0
    assert breakdown["card_percentage"] == 50.0
    assert breakdown["upi_percentage"] == 0.0


def test_daily_collections_bills_include_item_count_and_peak_hour(manager_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    _pay(cashier_user, table, menu_item, quantity=3, method="CASH")

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/daily/")

    assert response.status_code == 200
    assert response.data["bills"][0]["item_count"] == 3
    assert response.data["peak_hour"] is not None
    assert " - " in response.data["peak_hour"]


def test_daily_collections_date_range_filters_inclusive(manager_client, cashier_client, table, menu_item):
    """New: date_from/date_to (2026-08-27, per Shereena's Billing screen
    date picker needing a from/to range, same as the Bill History list)."""
    from datetime import timedelta

    from django.utils import timezone

    cashier_user, _ = cashier_client
    bill_in_range = _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    bill_in_range.paid_at = bill_in_range.paid_at - timedelta(days=3)
    bill_in_range.save(update_fields=["paid_at"])

    from apps.tables.models import Table

    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="Range1")
    bill_outside_range = _pay(cashier_user, other_table, menu_item, quantity=1, method="CASH")
    bill_outside_range.paid_at = bill_outside_range.paid_at - timedelta(days=10)
    bill_outside_range.save(update_fields=["paid_at"])

    today = timezone.localdate()
    date_from = (today - timedelta(days=5)).isoformat()
    date_to = today.isoformat()

    _, manager = manager_client
    response = manager.get(f"/v1/cashier/collections/daily/?date_from={date_from}&date_to={date_to}")

    assert response.status_code == 200
    assert response.data["tables_served"] == 1
    assert Decimal(response.data["total_collected"]) == _with_tax(menu_item.price)


def test_cashier_collections_shows_not_submitted_for_open_shift(manager_client, cashier_client, table, menu_item):
    """New: GET /v1/cashier/collections/by-cashier/ (2026-08-27, per
    Shereena's Billing screen 'Cashier Collections' panel)."""
    cashier_user, _ = cashier_client
    billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/by-cashier/")

    assert response.status_code == 200
    assert len(response.data) == 1
    row = response.data[0]
    assert row["cashier_id"] == str(cashier_user.id)
    assert row["status"] == "NOT_SUBMITTED"
    assert row["tables_served"] == 1
    assert Decimal(row["total_collected"]) == _with_tax(menu_item.price)


def test_cashier_collections_shows_matched_for_closed_shift(manager_client, cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    client.post(
        f"/v1/cashier/shifts/{shift.id}/close/", {"counted_cash": str(_with_tax(menu_item.price))}, format="json"
    )

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/by-cashier/")

    assert response.status_code == 200
    assert response.data[0]["status"] == "MATCHED"


def test_cashier_collections_shows_discrepancy_for_mismatched_shift(manager_client, cashier_client, table, menu_item):
    cashier_user, client = cashier_client
    shift = billing_services.open_shift(cashier_user)
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    client.post(
        f"/v1/cashier/shifts/{shift.id}/close/",
        {"counted_cash": "1.00", "acknowledge_discrepancy": True, "discrepancy_reason": "Miscounted"},
        format="json",
    )

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/by-cashier/")

    assert response.status_code == 200
    assert response.data[0]["status"] == "DISCREPANCY"


def test_cashier_collections_date_range_filters_inclusive(manager_client, cashier_client, table, menu_item):
    from datetime import timedelta

    from django.utils import timezone

    cashier_user, _ = cashier_client
    shift_in_range = billing_services.open_shift(cashier_user)
    shift_in_range.opened_at = shift_in_range.opened_at - timedelta(days=3)
    shift_in_range.save(update_fields=["opened_at"])

    _, manager = manager_client
    today = timezone.localdate()
    date_from = (today - timedelta(days=5)).isoformat()
    date_to = today.isoformat()

    response = manager.get(f"/v1/cashier/collections/by-cashier/?date_from={date_from}&date_to={date_to}")

    assert response.status_code == 200
    assert len(response.data) == 1
    assert response.data[0]["shift_id"] == str(shift_in_range.id)

    outside_range_response = manager.get(
        f"/v1/cashier/collections/by-cashier/?date_from={today.isoformat()}&date_to={today.isoformat()}"
    )
    assert outside_range_response.status_code == 200
    assert len(outside_range_response.data) == 0


def test_cashier_collections_requires_admin_or_manager(cashier_client):
    _, cashier = cashier_client
    response = cashier.get("/v1/cashier/collections/by-cashier/")
    assert response.status_code == 403


def test_daily_collections_search_filters_bills_not_totals(manager_client, cashier_client, table, menu_item):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="Findme")
    _pay(cashier_user, other_table, menu_item, quantity=1, method="CARD")

    _, manager = manager_client
    response = manager.get("/v1/cashier/collections/daily/?search=Findme")

    assert response.status_code == 200
    assert len(response.data["bills"]) == 1
    assert response.data["bills"][0]["table_number"] == "Findme"
    # Totals/tiles above the list still reflect the FULL day, not the search.
    assert response.data["tables_served"] == 2


def test_my_sales_scopes_to_current_shift_not_calendar_day(cashier_client, table, menu_item):
    """Regression (2026-08-25, Shereena): a bill paid earlier TODAY but
    BEFORE the current shift opened must not count towards My Sales — the
    screen is meant to mirror Cash Reconciliation's own shift window, not
    the calendar day."""
    from django.utils import timezone

    from apps.billing.models import Bill
    from apps.tables.models import Table

    cashier_user, client = cashier_client

    before_shift_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="BeforeShift1")
    before_bill = _pay(cashier_user, before_shift_table, menu_item, quantity=1, method="CASH")
    Bill.objects.filter(id=before_bill.id).update(paid_at=timezone.now() - timezone.timedelta(hours=2))

    client.post("/v1/cashier/shifts/open/")
    _pay(cashier_user, table, menu_item, quantity=1, method="CARD")

    response = client.get("/v1/cashier/collections/my-sales/")

    assert response.status_code == 200
    assert len(response.data["bills"]) == 1
    assert response.data["bills"][0]["payment_method"] == "CARD"
    assert Decimal(response.data["payment_breakdown"]["cash"]) == 0


def test_my_sales_scopes_to_calling_cashier_only(cashier_client, table, menu_item, restaurant):
    from apps.authentication.models import User
    from apps.tables.models import Table

    cashier_a, client_a = cashier_client
    _pay(cashier_a, table, menu_item, quantity=1, method="CASH")

    cashier_b = User.objects.create_user(
        email="cashier-b@test.dineos", password="Test@1234", role="CASHIER", name="Cashier B", restaurant=restaurant,
    )
    other_table = Table.objects.create(restaurant=restaurant, branch=table.branch, table_number="OtherCashier1")
    _pay(cashier_b, other_table, menu_item, quantity=1, method="CARD")

    response = client_a.get("/v1/cashier/collections/my-sales/")

    assert response.status_code == 200
    assert response.data["tables_served"] == 1
    assert len(response.data["bills"]) == 1
    assert Decimal(response.data["payment_breakdown"]["card"]) == 0


def test_my_sales_payment_method_filter_narrows_list_not_totals(cashier_client, table, menu_item):
    from apps.tables.models import Table

    cashier_user, client = cashier_client
    _pay(cashier_user, table, menu_item, quantity=1, method="CASH")
    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="CardOnly1")
    _pay(cashier_user, other_table, menu_item, quantity=1, method="CARD")

    response = client.get("/v1/cashier/collections/my-sales/?payment_method=CARD")

    assert response.status_code == 200
    assert len(response.data["bills"]) == 1
    assert response.data["bills"][0]["payment_method"] == "CARD"
    assert response.data["tables_served"] == 2  # totals unaffected by the filter


def test_cashier_endpoints_require_billing_enabled(cashier_client, restaurant):
    restaurant.billing_enabled = False
    restaurant.save()

    _, client = cashier_client
    response = client.post("/v1/cashier/shifts/open/")

    assert response.status_code == 403
