import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.models import Bill
from apps.orders import services as order_services
from apps.payments.models import PaymentAttempt
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def _webhook_payload(razorpay_order_id):
    return json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"order_id": razorpay_order_id, "status": "captured"}}},
    })


def test_create_order_fails_cleanly_when_restaurant_not_onboarded(cashier_client, table, menu_item):
    """restaurant.razorpay_account_id is blank by default (2026-09-09) -
    Razorpay collection is off for every restaurant until they explicitly
    link an account, so this must fail with a clear message, not a crash,
    and must not create a PaymentAttempt row."""
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.post(
        "/v1/payments/razorpay/create-order/",
        {"session_id": str(session.id), "payment_method": "UPI"}, format="json",
    )

    assert response.status_code == 400
    assert "not set up for this restaurant" in response.data["detail"]
    assert PaymentAttempt.objects.count() == 0


def test_create_order_fails_cleanly_when_platform_not_configured(cashier_client, table, menu_item, restaurant):
    """restaurant HAS linked an account, but RAZORPAY_KEY_ID/SECRET are
    unset (the real default in every environment until the platform itself
    is onboarded) - exercises core.razorpay_client.create_order's own
    real guard, no mocking needed, proving the settings-level gate works
    end to end."""
    cashier_user, client = cashier_client
    restaurant.razorpay_account_id = "acc_fake123"
    restaurant.save(update_fields=["razorpay_account_id"])
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.post(
        "/v1/payments/razorpay/create-order/",
        {"session_id": str(session.id), "payment_method": "UPI"}, format="json",
    )

    assert response.status_code == 502
    assert "not configured on this platform" in response.data["detail"]
    assert PaymentAttempt.objects.count() == 0


def test_create_order_succeeds_when_fully_configured(cashier_client, table, menu_item, restaurant, settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    restaurant.razorpay_account_id = "acc_fake123"
    restaurant.save(update_fields=["razorpay_account_id"])
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    with patch("apps.payments.views.create_order", return_value={"id": "order_fake123"}) as mock_create:
        response = client.post(
            "/v1/payments/razorpay/create-order/",
            {"session_id": str(session.id), "payment_method": "UPI"}, format="json",
        )

    assert response.status_code == 201, response.data
    assert response.data["razorpay_order_id"] == "order_fake123"
    assert response.data["key_id"] == "rzp_test_fake"
    mock_create.assert_called_once()

    attempt = PaymentAttempt.objects.get()
    assert attempt.session_id == session.id
    assert attempt.status == PaymentAttempt.Status.CREATED
    assert attempt.payment_method == "UPI"
    expected_total = (menu_item.price * Decimal("1.05")).quantize(Decimal("0.01"))
    assert attempt.amount == expected_total


def test_create_order_rejects_already_paid_bill(cashier_client, table, menu_item, restaurant, settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    restaurant.razorpay_account_id = "acc_fake123"
    restaurant.save(update_fields=["razorpay_account_id"])
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    client.post("/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json")

    response = client.post(
        "/v1/payments/razorpay/create-order/",
        {"session_id": str(session.id), "payment_method": "UPI"}, format="json",
    )

    assert response.status_code == 409
    assert PaymentAttempt.objects.count() == 0


def test_webhook_confirms_payment_creates_bill_and_is_idempotent(cashier_client, table, menu_item, restaurant):
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    attempt = PaymentAttempt.objects.create(
        session_id=session.id, restaurant=restaurant, razorpay_order_id="order_fake123",
        payment_method="UPI", amount=Decimal("231.00"), initiated_by=cashier_user,
    )

    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = client.post(
            "/v1/payments/razorpay/webhook/", data=_webhook_payload("order_fake123"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )

    assert response.status_code == 200, response.data
    bill = Bill.objects.get(session=session)
    assert bill.payment_method == "UPI"
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.Status.PAID
    assert attempt.resolved_at is not None

    # idempotent replay - second delivery of the same webhook must not error
    # or create a second Bill
    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        replay = client.post(
            "/v1/payments/razorpay/webhook/", data=_webhook_payload("order_fake123"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )
    assert replay.status_code == 200
    assert Bill.objects.filter(session=session).count() == 1


def test_webhook_rejects_invalid_signature(cashier_client, table, menu_item, restaurant):
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    PaymentAttempt.objects.create(
        session_id=session.id, restaurant=restaurant, razorpay_order_id="order_fake123",
        payment_method="UPI", amount=Decimal("231.00"), initiated_by=cashier_user,
    )

    from core.razorpay_client import RazorpayUnavailableError

    with patch("apps.payments.views.verify_webhook_signature", side_effect=RazorpayUnavailableError("bad sig")):
        response = client.post(
            "/v1/payments/razorpay/webhook/", data=_webhook_payload("order_fake123"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="wrong",
        )

    assert response.status_code == 400
    assert Bill.objects.filter(session=session).count() == 0


def test_existing_cash_payment_flow_is_completely_unaffected(cashier_client, table, menu_item):
    """Guard rail (2026-09-09): the Razorpay integration must never touch
    the pre-existing manual payment flow - same request/response as
    before this integration existed."""
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = client.post(
        "/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json",
    )

    assert response.status_code == 201
    assert response.data["payment_method"] == "CASH"
    assert PaymentAttempt.objects.count() == 0
