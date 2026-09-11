import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.models import Bill
from apps.orders import services as order_services
from apps.payments.models import PaymentAttempt
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def _webhook_payload(razorpay_order_id, method=None):
    entity = {"order_id": razorpay_order_id, "status": "captured"}
    if method:
        entity["method"] = method
    return json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": entity}}})


def _qr_webhook_payload(razorpay_qr_code_id, method="upi"):
    return json.dumps({
        "event": "qr_code.credited",
        "payload": {
            "qr_code": {"entity": {"id": razorpay_qr_code_id}},
            "payment": {"entity": {"method": method, "status": "captured"}},
        },
    })


def test_create_order_falls_back_to_shared_account_when_restaurant_not_linked(
    cashier_client, table, menu_item, restaurant, settings,
):
    """restaurant.razorpay_account_id is blank by default (2026-09-09) -
    which is every restaurant right now, since this platform's Razorpay
    account doesn't have Route enabled yet ("Route feature not enabled
    for the merchant", confirmed live against Razorpay's real API). Rather
    than block payment collection entirely until Route gets approved, a
    blank linked account falls back to a plain order with no transfers -
    still fully functional, just settling to the platform's own account
    for now. Routing kicks in automatically the moment an account is
    linked, no code change needed."""
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    with patch("apps.payments.views.create_order", return_value={"id": "order_fake123"}) as mock_create:
        response = client.post(
            "/v1/payments/razorpay/create-order/",
            {"session_id": str(session.id), "payment_method": "UPI"}, format="json",
        )

    assert response.status_code == 201, response.data
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["linked_account_id"] is None


def test_create_order_routes_to_linked_account_when_restaurant_has_one(
    cashier_client, table, menu_item, restaurant, settings,
):
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
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["linked_account_id"] == "acc_fake123"


def test_razorpay_client_omits_transfers_when_no_linked_account(settings):
    """Unit-level proof (no mocking of the SDK itself) that a blank
    linked_account_id produces a plain order payload with no "transfers"
    key, matching what Razorpay's real API actually accepts without Route."""
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    from core.razorpay_client import create_order

    with patch("razorpay.Client") as MockClient:
        MockClient.return_value.order.create.return_value = {"id": "order_x"}
        create_order(Decimal("100.00"), receipt="r1", linked_account_id=None)
        payload = MockClient.return_value.order.create.call_args[0][0]
        assert "transfers" not in payload

    with patch("razorpay.Client") as MockClient:
        MockClient.return_value.order.create.return_value = {"id": "order_y"}
        create_order(Decimal("100.00"), receipt="r1", linked_account_id="acc_fake123")
        payload = MockClient.return_value.order.create.call_args[0][0]
        assert payload["transfers"] == [{"account": "acc_fake123", "amount": 10000, "currency": "INR"}]


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


def test_webhook_noslash_url_is_accepted_without_a_redirect(cashier_client, table, menu_item, restaurant):
    """2026-09-11: confirmed live in production that Razorpay's webhook
    dispatcher POSTs to the URL exactly as saved in their dashboard, and if
    that's missing the trailing slash, Django's APPEND_SLASH 301-redirects it
    - which most webhook clients (Razorpay's included) follow by re-sending
    as GET, silently dropping the signed payload. This left every real
    payment confirmed on Razorpay's side stuck as PENDING on ours, no matter
    how many times it was retried. The no-slash path in urls.py must resolve
    directly, with no redirect, regardless of what's saved on Razorpay's end.
    """
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    PaymentAttempt.objects.create(
        session_id=session.id, restaurant=restaurant, razorpay_order_id="order_fake123",
        payment_method="UPI", amount=Decimal("231.00"), initiated_by=cashier_user,
    )

    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = client.post(
            "/v1/payments/razorpay/webhook", data=_webhook_payload("order_fake123"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )

    assert response.status_code == 200, response.data
    assert Bill.objects.filter(session=session).exists()


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


def test_webhook_uses_razorpays_reported_method_over_the_preselection(cashier_client, table, menu_item, restaurant):
    """Correctness fix (2026-09-09): Checkout can let the payer switch
    methods after a pre-selection, so the webhook's own reported method
    must win - the PaymentAttempt here is pre-set to UPI but the webhook
    reports the customer actually paid by card, and the resulting Bill
    must end up CARD, not UPI."""
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    PaymentAttempt.objects.create(
        session_id=session.id, restaurant=restaurant, razorpay_order_id="order_fake123",
        payment_method="UPI", amount=Decimal("231.00"), initiated_by=cashier_user,
    )

    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = client.post(
            "/v1/payments/razorpay/webhook/", data=_webhook_payload("order_fake123", method="card"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )

    assert response.status_code == 200, response.data
    bill = Bill.objects.get(session=session)
    assert bill.payment_method == "CARD"


# ---- Customer self-checkout (Phase 2, no auth at all) ----

def test_customer_create_order_succeeds_with_no_auth(api_client, table, menu_item, restaurant, settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    with patch("apps.payments.views.create_order", return_value={"id": "order_customer123"}) as mock_create:
        response = api_client.post(
            "/v1/payments/razorpay/customer/create-order/", {"session_id": str(session.id)}, format="json",
        )

    assert response.status_code == 201, response.data
    assert response.data["razorpay_order_id"] == "order_customer123"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["linked_account_id"] is None

    attempt = PaymentAttempt.objects.get()
    assert attempt.session_id == session.id
    assert attempt.initiated_by is None
    assert attempt.restaurant_id == restaurant.id


def test_customer_payment_status_reflects_unpaid_then_paid(api_client, cashier_client, table, menu_item):
    """The customer app's actual confirmation-polling target — unlike
    GET /v1/tables/{id}/session/, this must NOT 404 once the session
    closes; it needs to positively report paid=true with the Bill."""
    cashier_user, staff_client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    before = api_client.get(f"/v1/payments/razorpay/customer/status/{session.id}/")
    assert before.status_code == 200
    assert before.data["paid"] is False
    assert before.data["bill"] is None

    staff_client.post("/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json")

    after = api_client.get(f"/v1/payments/razorpay/customer/status/{session.id}/")
    assert after.status_code == 200
    assert after.data["paid"] is True
    assert after.data["bill"]["payment_method"] == "CASH"


def test_customer_create_order_404s_for_unknown_session(api_client, settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    import uuid

    response = api_client.post(
        "/v1/payments/razorpay/customer/create-order/", {"session_id": str(uuid.uuid4())}, format="json",
    )

    assert response.status_code == 404
    assert PaymentAttempt.objects.count() == 0


def test_customer_create_order_rejects_already_paid_bill(api_client, cashier_client, table, menu_item):
    cashier_user, staff_client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    staff_client.post("/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json")

    response = api_client.post(
        "/v1/payments/razorpay/customer/create-order/", {"session_id": str(session.id)}, format="json",
    )

    assert response.status_code == 409
    assert PaymentAttempt.objects.count() == 0


def test_customer_initiated_webhook_confirms_payment_with_no_processed_by(
    api_client, table, menu_item, restaurant, settings,
):
    """A customer self-checkout PaymentAttempt has initiated_by=None - the
    webhook -> pay_bill path must handle that cleanly (Bill.processed_by
    stays null, no crash), same as PaymentAttempt.initiated_by already
    being nullable for exactly this reason."""
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    with patch("apps.payments.views.create_order", return_value={"id": "order_customer456"}):
        api_client.post(
            "/v1/payments/razorpay/customer/create-order/", {"session_id": str(session.id)}, format="json",
        )

    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = api_client.post(
            "/v1/payments/razorpay/webhook/", data=_webhook_payload("order_customer456", method="upi"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )

    assert response.status_code == 200, response.data
    bill = Bill.objects.get(session=session)
    assert bill.payment_method == "UPI"
    assert bill.processed_by is None


# ---- "Pay by QR" (2026-09-09, per Shereena) ----

def test_create_qr_code_succeeds(cashier_client, table, menu_item, restaurant, settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    with patch(
        "apps.payments.views.create_qr_code",
        return_value={"id": "qr_fake123", "image_url": "https://rzp.io/i/fake123"},
    ) as mock_create:
        response = client.post(
            "/v1/payments/razorpay/qr-code/", {"session_id": str(session.id)}, format="json",
        )

    assert response.status_code == 201, response.data
    assert response.data["razorpay_qr_code_id"] == "qr_fake123"
    assert response.data["image_url"] == "https://rzp.io/i/fake123"
    mock_create.assert_called_once()

    attempt = PaymentAttempt.objects.get()
    assert attempt.razorpay_qr_code_id == "qr_fake123"
    assert attempt.razorpay_order_id is None
    assert attempt.payment_method == "UPI"


def test_create_qr_code_rejects_already_paid_bill(cashier_client, table, menu_item, settings):
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    client.post("/v1/bills/payment/", {"session_id": str(session.id), "payment_method": "CASH"}, format="json")

    response = client.post("/v1/payments/razorpay/qr-code/", {"session_id": str(session.id)}, format="json")

    assert response.status_code == 409
    assert PaymentAttempt.objects.count() == 0


def test_qr_code_webhook_confirms_payment_and_is_idempotent(cashier_client, table, menu_item, restaurant):
    cashier_user, client = cashier_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    attempt = PaymentAttempt.objects.create(
        session_id=session.id, restaurant=restaurant, razorpay_qr_code_id="qr_fake123",
        payment_method="UPI", amount=Decimal("231.00"), initiated_by=cashier_user,
    )

    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = client.post(
            "/v1/payments/razorpay/webhook/", data=_qr_webhook_payload("qr_fake123"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )

    assert response.status_code == 200, response.data
    bill = Bill.objects.get(session=session)
    assert bill.payment_method == "UPI"
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.Status.PAID

    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        replay = client.post(
            "/v1/payments/razorpay/webhook/", data=_qr_webhook_payload("qr_fake123"),
            content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig",
        )
    assert replay.status_code == 200
    assert Bill.objects.filter(session=session).count() == 1


def test_razorpay_client_qr_code_payload_shape(settings):
    """Unit-level proof (no mocking of the SDK's HTTP layer, only the
    Client class itself) that create_qr_code sends the exact fixed-amount,
    single-use payload Razorpay's QR Codes API requires."""
    settings.RAZORPAY_KEY_ID = "rzp_test_fake"
    settings.RAZORPAY_KEY_SECRET = "fake_secret"
    from core.razorpay_client import create_qr_code

    with patch("razorpay.Client") as MockClient:
        MockClient.return_value.qrcode.create.return_value = {"id": "qr_x", "image_url": "https://rzp.io/i/x"}
        create_qr_code(Decimal("150.00"), name="Table 5 bill")
        payload = MockClient.return_value.qrcode.create.call_args[0][0]
        assert payload == {
            "type": "upi_qr", "name": "Table 5 bill", "usage": "single_use",
            "fixed_amount": True, "payment_amount": 15000,
        }
