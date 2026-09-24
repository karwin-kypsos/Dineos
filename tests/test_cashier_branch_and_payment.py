"""Cashier permutation-matrix findings, 2026-09-24.

Written after walking every reachable cashier combination (shift state x
order origin x payment method x amount tendered x bill state x branch)
against the live server. Three of those combinations produced a wrong
result; each one has a test here.
"""
import json
from decimal import Decimal

import pytest

from apps.billing.models import Bill
from apps.orders import services as order_services
from apps.tables import services as table_services
from apps.tables.models import Table

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_branches(restaurant):
    from apps.restaurant.models import Branch

    return (
        Branch.objects.create(restaurant=restaurant, name="Koramangala"),
        Branch.objects.create(restaurant=restaurant, name="Indiranagar"),
    )


@pytest.fixture
def kora_table(restaurant, two_branches):
    kora, _ = two_branches
    return Table.objects.create(restaurant=restaurant, branch=kora, table_number="K1", capacity=4)


@pytest.fixture
def indiranagar_cashier(cashier_client, two_branches):
    _, indiranagar = two_branches
    user, client = cashier_client
    user.branch = indiranagar
    user.save(update_fields=["branch"])
    return user, client


def _seated_order(table, menu_item, quantity=1):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": quantity}])
    return session


# --------------------------------------------------------------- branch scope
def test_cashier_cannot_pay_another_branchs_table(indiranagar_cashier, kora_table, menu_item):
    """Proven live on 2026-09-24: the Indiranagar cashier closed a
    Koramangala bill and it was created with branch=Koramangala while
    processed_by was the Indiranagar cashier - another branch's revenue
    landing in this cashier's shift."""
    _, client = indiranagar_cashier
    session = _seated_order(kora_table, menu_item)

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": "1000.00"},
        format="json",
    )

    assert response.status_code == 403
    assert not Bill.objects.filter(session=session).exists()


def test_cashier_can_still_pay_their_own_branchs_table(cashier_client, two_branches, restaurant, menu_item):
    kora, _ = two_branches
    user, client = cashier_client
    user.branch = kora
    user.save(update_fields=["branch"])
    table = Table.objects.create(restaurant=restaurant, branch=kora, table_number="K2", capacity=2)
    session = _seated_order(table, menu_item)

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": "1000.00"},
        format="json",
    )

    assert response.status_code == 201


def test_branchless_admin_can_pay_any_branch(admin_client, kora_table, menu_item):
    """An Admin has no branch of their own and keeps whole-restaurant
    reach, exactly as they do on Tables/Menu/Inventory."""
    user, client = admin_client
    assert user.branch_id is None
    session = _seated_order(kora_table, menu_item)

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": "1000.00"},
        format="json",
    )

    assert response.status_code == 201


def test_cashier_cannot_pay_another_branchs_takeaway(indiranagar_cashier, two_branches, restaurant, menu_item):
    from apps.orders.models import Order

    kora, _ = two_branches
    _, client = indiranagar_cashier
    order = Order.objects.create(
        branch=kora, order_type=Order.OrderType.TAKEAWAY, customer_name="Sam",
    )

    response = client.post(
        "/v1/bills/takeaway-payment/",
        {"order_id": str(order.id), "payment_method": "CASH", "amount_received": "1000.00"},
        format="json",
    )

    assert response.status_code == 403


# -------------------------------------------------------------- underpayment
def test_underpayment_is_rejected(cashier_client, table, menu_item):
    """Live, before the fix: tendering 107.50 against a 157.50 bill gave
    201 with change_given -50.00 and payment_status PAID."""
    _, client = cashier_client
    session = _seated_order(table, menu_item)
    preview = client.get("/v1/bills/session/" + str(session.id) + "/").data
    short = Decimal(str(preview["total_amount"])) - Decimal("50.00")

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": str(short)},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["shortfall"] == "50.00"
    assert not Bill.objects.filter(session=session).exists()


def test_zero_amount_received_on_a_real_bill_is_rejected(cashier_client, table, menu_item):
    _, client = cashier_client
    session = _seated_order(table, menu_item)

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": "0.00"},
        format="json",
    )

    assert response.status_code == 400


def test_exact_and_over_payment_still_work(cashier_client, table, menu_item):
    _, client = cashier_client
    session = _seated_order(table, menu_item)
    total = Decimal(str(client.get("/v1/bills/session/" + str(session.id) + "/").data["total_amount"]))

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": str(total + Decimal("500"))},
        format="json",
    )

    assert response.status_code == 201
    assert Decimal(str(response.data["change_given"])) == Decimal("500.00")


def test_payment_without_amount_received_still_works(cashier_client, table, menu_item):
    """CARD/UPI are routinely taken with no amount_received at all - the
    underpayment guard must not touch that path."""
    _, client = cashier_client
    session = _seated_order(table, menu_item)

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CARD"},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["change_given"] is None


# ------------------------------------------------- reconciliation completeness
def test_reconciliation_breaks_out_every_payment_method(cashier_client, restaurant, menu_item):
    """Live, before the fix: cash 4410 + card 630 + upi 630 against a total
    of 6300 - the NETBANKING/WALLET bills were inside the total but broken
    out nowhere, so 630 was unaccounted for on the cashier's own screen."""
    from apps.billing import services as billing_services

    user, client = cashier_client
    client.post("/v1/cashier/shifts/open/", {}, format="json")

    for index, method in enumerate(("CASH", "CARD", "UPI", "NETBANKING", "WALLET")):
        table = Table.objects.create(restaurant=restaurant, table_number="T" + str(index), capacity=2)
        session = _seated_order(table, menu_item)
        billing_services.pay_bill(session.id, method, user)

    shift_id = client.get("/v1/cashier/shifts/current/").data["shift"]["id"]
    body = json.loads(client.get("/v1/cashier/shifts/" + str(shift_id) + "/reconciliation/").content)

    buckets = ("cash", "card", "upi", "netbanking", "wallet")
    for bucket in buckets:
        assert bucket in body, bucket + " missing from reconciliation"
    assert sum(Decimal(body[b]) for b in buckets) == Decimal(body["total"])
    assert round(sum(body[b + "_percentage"] for b in buckets)) == 100


# ------------------------------------------------------------- money as string
def test_branch_today_revenue_is_a_decimal_string(admin_client, two_branches):
    _, client = admin_client
    body = json.loads(client.get("/v1/branches/").content)
    rows = body["results"] if isinstance(body, dict) and "results" in body else body
    for row in rows:
        assert isinstance(row["today_revenue"], str), "today_revenue floated: " + repr(row["today_revenue"])


def test_dashboard_today_revenue_is_a_decimal_string(admin_client):
    _, client = admin_client
    body = json.loads(client.get("/v1/admin/dashboard/").content)
    assert isinstance(body["today_revenue"], str)


def test_revenue_by_hour_amounts_are_decimal_strings(cashier_client, table, menu_item):
    from apps.billing import services as billing_services

    user, client = cashier_client
    session = _seated_order(table, menu_item)
    billing_services.pay_bill(session.id, "CASH", user)

    body = json.loads(client.get("/v1/cashier/collections/my-sales/").content)
    for row in body["revenue_by_hour"]:
        assert isinstance(row["amount"], str), "revenue_by_hour amount floated: " + repr(row["amount"])


def test_billing_dashboard_money_is_never_a_float(cashier_client, restaurant, menu_item, admin_client):
    """Floor Status, Summary and Payment Split are all hand-built dicts
    handed straight to Response(), so nothing passed through a DRF
    DecimalField. Live on 2026-09-24 they returned amount 157.5,
    total_revenue 5827.5 and cash 3937.5."""
    from apps.billing import services as billing_services

    user, client = cashier_client
    table = Table.objects.create(restaurant=restaurant, table_number="F1", capacity=2)
    session = _seated_order(table, menu_item)
    billing_services.pay_bill(session.id, "CASH", user)

    _, admin = admin_client

    floor = json.loads(admin.get("/v1/billing/floor-status/").content)
    paid = [row for row in floor if row["status"] == "PAID"]
    assert paid, "expected at least one PAID row to check"
    for row in paid:
        assert isinstance(row["amount"], str), "floor-status amount floated: " + repr(row["amount"])

    summary = json.loads(admin.get("/v1/billing/summary/").content)
    for field in ("total_revenue", "avg_bill_amount", "vs_yesterday", "vs_last_week"):
        assert isinstance(summary[field], str), field + " floated: " + repr(summary[field])
    for row in summary["revenue_by_hour"]:
        assert isinstance(row["amount"], str)

    split = json.loads(admin.get("/v1/billing/payment-split/").content)
    for bucket, values in split.items():
        assert isinstance(values["amount"], str), bucket + " amount floated: " + repr(values["amount"])


# --------------------------------------------- oversized text -> 400, not 500
@pytest.mark.parametrize(
    "payload_key,nested",
    [("customer_name", False), ("customer_phone", False), ("notes", False), ("notes", True)],
)
def test_oversized_takeaway_text_is_a_field_error_not_a_crash(
    cashier_client, branch, menu_item, payload_key, nested
):
    """Live on 2026-09-24 every one of these returned HTTP 500: the
    serializer declared no max_length, so the value only failed at the
    Postgres column and surfaced to the cashier as a crash."""
    user, client = cashier_client
    user.branch = branch
    user.save(update_fields=["branch"])

    item = {"menu_item": menu_item.id, "quantity": 1}
    body = {"customer_name": "Sam", "items": [item]}
    if nested:
        item["notes"] = "x" * 500
    else:
        body[payload_key] = "x" * 500

    response = client.post("/v1/orders/takeaway/", body, format="json")

    assert response.status_code == 400, "expected a field error, got " + str(response.status_code)


@pytest.mark.parametrize("nested", [False, True])
def test_oversized_dine_in_notes_is_a_field_error_not_a_crash(server_client, table, menu_item, nested):
    """Reachable from the customer's own 'less spicy' box, not just staff."""
    _, client = server_client
    session, _ = table_services.get_or_create_active_session(table.id)

    item = {"menu_item": menu_item.id, "quantity": 1}
    body = {"session_id": str(session.id), "items": [item]}
    if nested:
        item["notes"] = "x" * 500
    else:
        body["notes"] = "x" * 500

    response = client.post("/v1/orders/", body, format="json")

    assert response.status_code == 400


# ------------------------------------------ bill preview / shift detail money
def test_bill_preview_money_is_never_a_float(cashier_client, table, menu_item):
    """The cashier's own bill screen. Live it returned subtotal 150.0,
    total_amount 157.5 and items[].unit_price 150.0."""
    _, client = cashier_client
    session = _seated_order(table, menu_item)

    body = json.loads(client.get("/v1/bills/session/" + str(session.id) + "/").content)

    for field in ("subtotal", "tax_amount", "service_charge", "total_amount"):
        assert isinstance(body[field], str), field + " floated: " + repr(body[field])
    for line in body["items"]:
        assert isinstance(line["unit_price"], str), "unit_price floated: " + repr(line["unit_price"])
        assert isinstance(line["line_total"], str), "line_total floated: " + repr(line["line_total"])


def test_shift_detail_money_is_never_a_float_and_covers_every_method(cashier_client, restaurant, menu_item):
    """ShiftBillsView's docstring promises its figures match
    ShiftReconciliationView exactly - with payment_split hardcoded to
    cash/card/upi while total_collected summed all five, they did not."""
    from apps.billing import services as billing_services

    user, client = cashier_client
    client.post("/v1/cashier/shifts/open/", {}, format="json")

    for index, method in enumerate(("CASH", "CARD", "UPI", "NETBANKING", "WALLET")):
        tbl = Table.objects.create(restaurant=restaurant, table_number="S" + str(index), capacity=2)
        session = _seated_order(tbl, menu_item)
        billing_services.pay_bill(session.id, method, user)

    shift_id = client.get("/v1/cashier/shifts/current/").data["shift"]["id"]
    body = json.loads(client.get("/v1/cashier/shifts/" + str(shift_id) + "/bills/").content)

    for field in ("total_collected", "expected_cash"):
        assert isinstance(body["shift"][field], str), field + " floated: " + repr(body["shift"][field])

    split = body["payment_split"]
    buckets = ("cash", "card", "upi", "netbanking", "wallet")
    for bucket in buckets:
        assert bucket in split, bucket + " missing from shift payment_split"
        assert isinstance(split[bucket]["amount"], str)
    assert sum(Decimal(split[b]["amount"]) for b in buckets) == Decimal(body["shift"]["total_collected"])


def test_bill_detail_line_items_are_decimal_strings(cashier_client, table, menu_item):
    from apps.billing import services as billing_services

    user, client = cashier_client
    session = _seated_order(table, menu_item)
    bill = billing_services.pay_bill(session.id, "CASH", user)

    body = json.loads(client.get("/v1/bills/" + str(bill.id) + "/").content)

    for line in body["items"]:
        assert isinstance(line["unit_price"], str)
        assert isinstance(line["line_total"], str)
    for rnd in body["rounds"]:
        for line in rnd["items"]:
            assert isinstance(line["line_total"], str)


def test_cashier_detail_counts_every_payment_method(cashier_client, restaurant, menu_item, admin_client):
    """cashier_billing_detail accumulated only cash/card/upi, so its
    grand_total - and therefore total_collected - was SHORT by whatever
    came in on NETBANKING/WALLET. Not a cosmetic split gap: the headline
    figure itself under-reported the cashier's takings."""
    from apps.billing import services as billing_services

    user, client = cashier_client
    client.post("/v1/cashier/shifts/open/", {}, format="json")

    for index, method in enumerate(("CASH", "CARD", "UPI", "NETBANKING", "WALLET")):
        tbl = Table.objects.create(restaurant=restaurant, table_number="D" + str(index), capacity=2)
        session = _seated_order(tbl, menu_item)
        billing_services.pay_bill(session.id, method, user)

    _, admin = admin_client
    body = json.loads(admin.get("/v1/billing/cashiers/" + str(user.id) + "/").content)

    buckets = ("cash", "card", "upi", "netbanking", "wallet")
    split = body["payment_split"]
    for bucket in buckets:
        assert bucket in split, bucket + " missing from cashier detail split"
        assert isinstance(split[bucket]["amount"], str)

    assert sum(Decimal(split[b]["amount"]) for b in buckets) == Decimal(body["total_collected"])
    # Five bills, one per method - none of them may be missing from the total.
    assert Decimal(body["total_collected"]) == Decimal(split["cash"]["amount"]) * 5

    for field in ("expected_cash", "actual_cash", "difference"):
        assert isinstance(body["cash_reconciliation"][field], str)


def test_billing_cashiers_row_money_is_a_decimal_string(cashier_client, restaurant, menu_item, admin_client):
    from apps.billing import services as billing_services

    user, client = cashier_client
    client.post("/v1/cashier/shifts/open/", {}, format="json")
    tbl = Table.objects.create(restaurant=restaurant, table_number="C1", capacity=2)
    session = _seated_order(tbl, menu_item)
    billing_services.pay_bill(session.id, "CASH", user)

    _, admin = admin_client
    body = json.loads(admin.get("/v1/billing/cashiers/").content)

    assert body, "expected at least one cashier row"
    for row in body:
        for field in ("total_collected", "expected_cash", "counted_cash", "discrepancy_amount"):
            # null is a legitimate value while the shift is still open -
            # what must never come back is a float.
            assert row[field] is None or isinstance(row[field], str), (
                field + " floated: " + repr(row[field])
            )


def test_branch_list_total_revenue_is_a_decimal_string(admin_client, two_branches):
    _, client = admin_client
    body = json.loads(client.get("/v1/branches/").content)
    assert isinstance(body["total_revenue"], str), "total_revenue floated: " + repr(body["total_revenue"])
