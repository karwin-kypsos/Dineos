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
