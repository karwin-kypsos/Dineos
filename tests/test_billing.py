from datetime import timedelta
from decimal import Decimal

import pytest

from apps.billing import services as billing_services
from apps.billing.models import Bill
from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def test_pay_bill_is_idempotent(cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 2}])

    bill_1 = billing_services.pay_bill(session.id, "CASH", cashier_user)
    bill_2 = billing_services.pay_bill(session.id, "CASH", cashier_user)

    assert bill_1.id == bill_2.id
    assert Bill.objects.filter(session=session).count() == 1


def test_payment_does_not_touch_portions(cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 2}])

    portion = menu_item.prepared_portions.get()
    remaining_before_payment = portion.portions_remaining

    billing_services.pay_bill(session.id, "CASH", cashier_user)

    portion.refresh_from_db()
    assert portion.portions_remaining == remaining_before_payment


def test_bill_total_sums_all_rounds(cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 2}])

    bill = billing_services.pay_bill(session.id, "CASH", cashier_user)
    assert bill.subtotal == menu_item.price * 3


def test_bill_preview_includes_items_and_table_number(cashier_client, table, menu_item):
    _, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 2}])

    response = client.get(f"/v1/bills/session/{session.id}/")

    assert response.status_code == 200
    assert response.data["table_number"] == table.table_number
    assert len(response.data["items"]) == 1
    assert response.data["items"][0]["menu_item_name"] == menu_item.name
    assert response.data["items"][0]["quantity"] == 2


def test_receipt_includes_items_branch_info_and_cashier_name(cashier_client, restaurant, menu_item):
    from apps.restaurant.models import Branch
    from apps.tables.models import Table

    cashier_user, client = cashier_client
    branch = Branch.objects.create(restaurant=restaurant, name="Kochi", address="123 MG Road", phone="9876543210")
    table_in_branch = Table.objects.create(restaurant=restaurant, branch=branch, table_number="9", capacity=4)
    session, _ = table_services.get_or_create_active_session(table_in_branch.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["restaurant_name"] == restaurant.name
    assert response.data["branch_name"] == "Kochi"
    assert response.data["branch_address"] == "123 MG Road"
    assert response.data["branch_phone"] == "9876543210"
    assert response.data["table_number"] == "9"
    assert response.data["processed_by_name"] == cashier_user.name
    assert len(response.data["items"]) == 1
    assert response.data["items"][0]["menu_item_name"] == menu_item.name


def test_pay_bill_records_amount_received_and_change(cashier_client, table, menu_item):
    _, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    tax = (menu_item.price * Decimal("5.00") / Decimal("100")).quantize(Decimal("0.01"))
    expected_total = menu_item.price + tax  # 5% GST from the restaurant fixture default
    tendered = expected_total + Decimal("10.00")

    response = client.post(
        "/v1/bills/payment/",
        {"session_id": str(session.id), "payment_method": "CASH", "amount_received": str(tendered)},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert Decimal(str(response.data["amount_received"])) == tendered
    assert Decimal(str(response.data["change_given"])) == Decimal("10.00")


def test_get_receipt_by_bill_id_after_payment(cashier_client, table, menu_item):
    _, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    pay = client.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )
    bill_id = pay.data["id"]

    response = client.get(f"/v1/bills/{bill_id}/")

    assert response.status_code == 200
    assert response.data["id"] == bill_id
    assert len(response.data["items"]) == 1
    assert response.data["items"][0]["menu_item_name"] == menu_item.name
    assert Decimal(str(response.data["gst_percentage"])) == Decimal("5.00")  # restaurant fixture default


def test_get_receipt_for_takeaway_bill(cashier_client, branch, menu_item):
    cashier_user, client = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])
    create = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    pay = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": create.data["id"], "payment_method": "CASH"}, format="json",
    )
    bill_id = pay.data["id"]

    response = client.get(f"/v1/bills/{bill_id}/")

    assert response.status_code == 200
    assert response.data["id"] == bill_id
    assert response.data["table_number"] is None


def test_get_receipt_from_another_restaurant_is_not_found(cashier_client, restaurant):
    from apps.restaurant.models import Branch, Restaurant
    from apps.billing.models import Bill

    _, client = cashier_client
    foreign_restaurant = Restaurant.objects.create(name="Foreign Billing", slug="foreign-billing")
    foreign_branch = Branch.objects.create(restaurant=foreign_restaurant, name="Foreign Branch")
    from apps.orders.models import Order

    foreign_order = Order.objects.create(order_type="TAKEAWAY", branch=foreign_branch, round_number=1)
    foreign_bill = Bill.objects.create(
        order=foreign_order, branch=foreign_branch, subtotal="10.00", total_amount="10.00", payment_method="CASH",
    )

    response = client.get(f"/v1/bills/{foreign_bill.id}/")

    assert response.status_code == 404


def test_pay_bill_without_amount_received_leaves_it_null(cashier_client, table, menu_item):
    _, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CARD"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["amount_received"] is None
    assert response.data["change_given"] is None


def test_list_bills_includes_dine_in_and_takeaway_across_cashiers(admin_client, cashier_client, branch, table, menu_item):
    _, cashier = cashier_client
    cashier_user, _ = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    dine_in_pay = cashier.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )
    takeaway_create = cashier.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    takeaway_pay = cashier.post(
        "/v1/bills/takeaway-payment/",
        {"order_id": takeaway_create.data["id"], "payment_method": "UPI"}, format="json",
    )

    _, admin = admin_client
    response = admin.get("/v1/bills/")

    assert response.status_code == 200
    bill_ids = {b["id"] for b in response.data}
    assert dine_in_pay.data["id"] in bill_ids
    assert takeaway_pay.data["id"] in bill_ids


def test_list_bills_filters_by_payment_method(admin_client, cashier_client, table, menu_item):
    _, cashier = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    pay = cashier.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )

    _, admin = admin_client
    response = admin.get("/v1/bills/?payment_method=CASH")

    assert response.status_code == 200
    assert pay.data["id"] in {b["id"] for b in response.data}
    assert all(b["payment_method"] == "CASH" for b in response.data)


def test_list_bills_filters_by_cashier(admin_client, cashier_client, table, menu_item):
    cashier_user, cashier = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    pay = cashier.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )

    _, admin = admin_client
    response = admin.get(f"/v1/bills/?cashier={cashier_user.id}")

    assert response.status_code == 200
    assert pay.data["id"] in {b["id"] for b in response.data}
    assert all(b["processed_by"] == cashier_user.id for b in response.data)


def test_list_bills_search_matches_takeaway_customer_name(admin_client, cashier_client, branch, menu_item):
    cashier_user, cashier = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])
    create = cashier.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Priya Search Target", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    pay = cashier.post(
        "/v1/bills/takeaway-payment/", {"order_id": create.data["id"], "payment_method": "CASH"}, format="json",
    )

    _, admin = admin_client
    response = admin.get("/v1/bills/?search=Priya")

    assert response.status_code == 200
    assert pay.data["id"] in {b["id"] for b in response.data}


def test_list_bills_date_today_excludes_other_days(admin_client, cashier_client, table, menu_item):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    _, cashier = cashier_client
    pay = cashier.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )
    bill = Bill.objects.get(id=pay.data["id"])
    bill.paid_at = bill.paid_at - timedelta(days=2)
    bill.save(update_fields=["paid_at"])

    _, admin = admin_client
    response = admin.get("/v1/bills/?date=today")

    assert response.status_code == 200
    assert bill.id not in {b["id"] for b in response.data}
