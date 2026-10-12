"""Subscription payments (2026-10-05, per Karwin): a restaurant paying
DineOS for its plan through Razorpay Subscriptions. Razorpay itself is
mocked here; the live check runs against Razorpay's test mode."""
import datetime
import json
from itertools import count
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.models import User
from apps.kitchen.models import KDSDevice
from apps.restaurant.models import Restaurant
from apps.subscriptions.models import RazorpayPlan, Subscription, SubscriptionPayment

pytestmark = pytest.mark.django_db

PRICES = {"STARTER": "999", "GROWTH": "2499", "ENTERPRISE": "4999"}
_ids = count(1)


@pytest.fixture
def prices(settings):
    settings.PLAN_PRICES = dict(PRICES)
    return settings.PLAN_PRICES


@pytest.fixture
def razorpay():
    """Fake Razorpay: every created plan/subscription gets a fresh id; calls are recorded."""
    with patch("core.razorpay_client.create_plan", side_effect=lambda name, amount, period="monthly": {"id": f"plan_{next(_ids)}"}) as plan, \
         patch("core.razorpay_client.create_subscription", side_effect=lambda plan_id, total, start_at=None, notes=None: {"id": f"sub_{next(_ids)}"}) as sub, \
         patch("core.razorpay_client.cancel_subscription", return_value={}) as cancel, \
         patch("apps.subscriptions.views.verify_subscription_payment", return_value=None) as verify,          patch("core.razorpay_client.fetch_subscription", return_value={"status": "created"}),          patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}):
        yield {"plan": plan, "subscription": sub, "cancel": cancel, "verify": verify}


@pytest.fixture
def trial_restaurant(restaurant):
    restaurant.status = Restaurant.Status.TRIAL
    restaurant.plan_tier = "STARTER"
    restaurant.trial_ends_at = timezone.now() + datetime.timedelta(days=10)
    restaurant.save(update_fields=["status", "plan_tier", "trial_ends_at"])
    return restaurant


def _webhook(event, sub, payment=None, current_end_days=30):
    now = int(timezone.now().timestamp())
    payload = {"event": event, "payload": {"subscription": {"entity": {
        "id": sub.razorpay_subscription_id, "status": event.split(".")[1],
        "current_start": now, "current_end": now + current_end_days * 86400,
    }}}}
    if payment:
        payload["payload"]["payment"] = {"entity": payment}
    client = APIClient()
    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        return client.post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json",
                           HTTP_X_RAZORPAY_SIGNATURE="sig")


def _checkout_and_verify(client, tier):
    checkout = client.post("/v1/subscriptions/checkout/", {"plan_id": tier}, format="json")
    assert checkout.status_code == 201, checkout.data
    verify = client.post("/v1/subscriptions/verify/", {
        "razorpay_subscription_id": checkout.data["razorpay_subscription_id"], "razorpay_payment_id": f"pay_{next(_ids)}",
        "razorpay_signature": "sig",
    }, format="json")
    assert verify.status_code == 200, verify.data
    return Subscription.objects.get(razorpay_subscription_id=checkout.data["razorpay_subscription_id"])


# ---- prices --------------------------------------------------------------------

def test_plans_show_null_prices_until_set_then_real_ones(api_client, settings):
    settings.PLAN_PRICES = {}
    assert all(p["price"] is None and p["billing_cycle"] is None for p in api_client.get("/v1/auth/plans/").data)

    settings.PLAN_PRICES = dict(PRICES)
    plans = {p["id"]: p for p in api_client.get("/v1/auth/plans/").data}
    assert plans["GROWTH"]["price"] == "2499.00" and plans["GROWTH"]["billing_cycle"] == "MONTHLY"


def test_checkout_refused_while_prices_are_unset(admin_client, trial_restaurant, settings, razorpay):
    settings.PLAN_PRICES = {}
    _, client = admin_client

    response = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert response.status_code == 409
    assert response.data["code"] == "prices_not_set"
    razorpay["subscription"].assert_not_called()


# ---- checkout / verify -----------------------------------------------------------

def test_trial_checkout_starts_billing_at_trial_end_and_reuses_on_double_tap(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client

    first = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")
    again = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert first.status_code == 201, first.data
    assert first.data["amount"] == "2499.00" and first.data["amount_paise"] == 249900
    assert first.data["change"] == "new"
    assert first.data["key_id"] is not None
    sub = Subscription.objects.get(id=first.data["subscription_id"])
    assert sub.start_at == trial_restaurant.trial_ends_at  # nobody is charged during a trial
    assert again.data["subscription_id"] == first.data["subscription_id"]
    assert razorpay["subscription"].call_count == 1
    assert RazorpayPlan.objects.count() == 1


def test_verify_rejects_a_bad_signature(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    checkout = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")
    from core.razorpay_client import RazorpayUnavailableError

    razorpay["verify"].side_effect = RazorpayUnavailableError("bad")
    response = client.post("/v1/subscriptions/verify/", {
        "razorpay_subscription_id": checkout.data["razorpay_subscription_id"], "razorpay_payment_id": "pay_x", "razorpay_signature": "forged",
    }, format="json")

    assert response.status_code == 400
    trial_restaurant.refresh_from_db()
    assert trial_restaurant.plan_tier == "STARTER"


def test_verified_trial_subscription_applies_the_plan_now(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client

    sub = _checkout_and_verify(client, "GROWTH")

    trial_restaurant.refresh_from_db()
    sub.refresh_from_db()
    assert sub.status == "AUTHENTICATED"
    assert trial_restaurant.plan_tier == "GROWTH" and trial_restaurant.max_branches == 5
    assert trial_restaurant.kitchen_enabled is True
    current = client.get("/v1/subscriptions/current/").data
    assert current["plan_id"] == "GROWTH" and current["status"] == "trialing" and current["prices_set"] is True


def test_charged_webhook_activates_and_records_the_invoice(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    sub = _checkout_and_verify(client, "GROWTH")

    response = _webhook("subscription.charged", sub, payment={"id": "pay_charge_1", "amount": 249900, "currency": "INR", "method": "upi", "invoice_id": "inv_1"})
    replay = _webhook("subscription.charged", sub, payment={"id": "pay_charge_1", "amount": 249900, "currency": "INR", "method": "upi", "invoice_id": "inv_1"})

    assert response.status_code == replay.status_code == 200
    sub.refresh_from_db()
    trial_restaurant.refresh_from_db()
    assert sub.status == "ACTIVE" and sub.current_end is not None
    assert trial_restaurant.status == "ACTIVE"
    invoices = client.get("/v1/subscriptions/invoices/").data
    assert invoices["count"] == 1 and invoices["results"][0]["amount"] == "2499.00"
    current = client.get("/v1/subscriptions/current/").data
    assert current["status"] == "active" and current["renews_at"] is not None


# ---- plan changes ----------------------------------------------------------------

def _active_on(client, tier):
    sub = _checkout_and_verify(client, tier)
    _webhook("subscription.charged", sub, payment={"id": f"pay_{next(_ids)}", "amount": 99900, "currency": "INR"})
    sub.refresh_from_db()
    return sub


def test_upgrade_applies_now_and_old_plan_stops_at_period_end(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    old = _active_on(client, "STARTER")

    new = _checkout_and_verify(client, "GROWTH")

    trial_restaurant.refresh_from_db()
    old.refresh_from_db()
    assert new.start_at == old.current_end  # new price from the next cycle
    assert trial_restaurant.plan_tier == "GROWTH"  # features right away
    assert old.cancel_at_period_end is True
    razorpay["cancel"].assert_called_with(old.razorpay_subscription_id, at_cycle_end=True)


def test_downgrade_waits_until_the_paid_period_ends(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    _active_on(client, "GROWTH")

    new = _checkout_and_verify(client, "STARTER")
    trial_restaurant.refresh_from_db()
    assert trial_restaurant.plan_tier == "GROWTH"
    assert client.get("/v1/subscriptions/current/").data["pending_plan_id"] == "STARTER"

    _webhook("subscription.activated", new)
    trial_restaurant.refresh_from_db()
    assert trial_restaurant.plan_tier == "STARTER"


def test_same_plan_checkout_refused(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    _active_on(client, "GROWTH")

    response = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert response.status_code == 409 and response.data["code"] == "already_on_plan"


# ---- cancel / failed renewals / payment due ----------------------------------------

def test_cancel_keeps_access_to_period_end_then_payment_due(admin_client, trial_restaurant, prices, razorpay, branch):
    admin, client = admin_client
    sub = _active_on(client, "GROWTH")
    User.objects.create_user(email="cashier@due.test", password="Test@1234", role="CASHIER", name="C", restaurant=trial_restaurant, branch=branch)
    device = KDSDevice.objects.create(restaurant=trial_restaurant, label="tab")

    cancelled = client.post("/v1/subscriptions/cancel/")
    assert cancelled.status_code == 200 and cancelled.data["cancel_at_period_end"] is True
    razorpay["cancel"].assert_called_with(sub.razorpay_subscription_id, at_cycle_end=True)

    _webhook("subscription.cancelled", sub)  # Razorpay sends it when the period is over
    trial_restaurant.refresh_from_db()
    assert trial_restaurant.status == "PAYMENT_DUE"

    staff_login = APIClient().post("/v1/auth/login/", {"email": "cashier@due.test", "password": "Test@1234"}, format="json")
    admin_login = APIClient().post("/v1/auth/login/", {"email": admin.email, "password": "Test@1234"}, format="json")
    assert staff_login.status_code == 400 and "subscription" in str(staff_login.data).lower()
    assert admin_login.status_code == 200
    assert client.get("/v1/subscriptions/current/").status_code == 200  # the Admin can renew
    blocked = client.get("/v1/staff/")
    assert blocked.status_code == 403 and blocked.json()["code"] == "subscription_required"
    assert APIClient().get("/v1/kitchen/devices/me/", HTTP_X_KDS_API_KEY=device.api_key).status_code == 403

    # Renewing restores access.
    renewed = _checkout_and_verify(client, "STARTER")
    trial_restaurant.refresh_from_db()
    assert renewed.status == "AUTHENTICATED" and trial_restaurant.status == "ACTIVE"


def test_failed_renewals_past_due_then_payment_due(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    sub = _active_on(client, "GROWTH")

    _webhook("subscription.pending", sub, payment={"id": "pay_failed_1", "amount": 249900, "currency": "INR"})
    assert client.get("/v1/subscriptions/current/").data["status"] == "past_due"
    _webhook("subscription.halted", sub)

    trial_restaurant.refresh_from_db()
    assert trial_restaurant.status == "PAYMENT_DUE"
    assert SubscriptionPayment.objects.filter(status="FAILED").count() == 1
    # Halted is still a payment problem to fix, not a cancellation.
    assert client.get("/v1/subscriptions/current/").data["status"] == "past_due"


def test_cancelling_during_a_trial_keeps_the_trial(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    sub = _checkout_and_verify(client, "GROWTH")

    client.post("/v1/subscriptions/cancel/")
    razorpay["cancel"].assert_called_with(sub.razorpay_subscription_id, at_cycle_end=False)
    _webhook("subscription.cancelled", sub)

    trial_restaurant.refresh_from_db()
    assert trial_restaurant.status == "TRIAL"


# ---- webhook routing & access -------------------------------------------------------

def test_payment_captured_for_a_subscription_invoice_is_acknowledged_not_404(api_client):
    payload = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_s", "order_id": "order_from_invoice", "invoice_id": "inv_9"}}}}
    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = api_client.post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig")

    assert response.status_code == 200


def test_unknown_subscription_webhook_is_ignored(api_client):
    payload = {"event": "subscription.charged", "payload": {"subscription": {"entity": {"id": "sub_elsewhere"}}}}
    with patch("apps.payments.views.verify_webhook_signature", return_value=None):
        response = api_client.post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig")

    assert response.status_code == 200


@pytest.mark.parametrize("method, path", [
    ("get", "/v1/subscriptions/current/"), ("post", "/v1/subscriptions/checkout/"), ("post", "/v1/subscriptions/verify/"),
    ("post", "/v1/subscriptions/cancel/"), ("get", "/v1/subscriptions/invoices/"),
])
def test_only_the_admin_manages_the_subscription(manager_client, method, path):
    _, client = manager_client

    assert getattr(client, method)(path).status_code == 403


def test_switching_razorpay_keys_makes_a_new_plan_not_a_test_mode_one(admin_client, trial_restaurant, prices, razorpay, settings):
    _, client = admin_client
    settings.RAZORPAY_KEY_ID = "rzp_test_aaa"
    client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")
    Subscription.objects.all().delete()

    settings.RAZORPAY_KEY_ID = "rzp_live_bbb"
    client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert RazorpayPlan.objects.filter(plan_tier="GROWTH", amount="2499").count() == 2
    assert razorpay["plan"].call_count == 2


def test_razorpay_refusing_subscriptions_gives_a_readable_502(admin_client, trial_restaurant, prices, settings):
    """Live 2026-10-05: with Subscriptions not switched on for the account,
    Razorpay answers {"error": "Unauthorized"} and the SDK raises an empty
    ServerError - the app should still get a reason, not "failed: "."""
    from razorpay.errors import ServerError

    _, client = admin_client
    settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = "rzp_test_aaa", "secret"
    with patch("razorpay.Client") as rz:
        rz.return_value.plan.create.side_effect = ServerError("")
        response = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert response.status_code == 502
    assert "Subscriptions enabled" in response.data["detail"]
    assert not Subscription.objects.exists()


def test_an_ended_subscription_stays_ended(admin_client, trial_restaurant, prices, razorpay, settings):
    """A late or replayed charged/resumed webhook, or a late verify, must
    not revive a cancelled subscription and reopen a PAYMENT_DUE restaurant."""
    _, client = admin_client
    sub = _active_on(client, "GROWTH")
    _webhook("subscription.cancelled", sub)
    trial_restaurant.refresh_from_db()
    assert trial_restaurant.status == "PAYMENT_DUE"

    for event in ("subscription.charged", "subscription.resumed", "subscription.activated", "subscription.pending"):
        response = _webhook(event, sub, payment={"id": f"pay_late_{event}", "amount": 249900, "currency": "INR"})
        assert response.status_code == 200 and "ignored" in response.data["detail"]
    late_verify = client.post("/v1/subscriptions/verify/", {
        "razorpay_subscription_id": sub.razorpay_subscription_id, "razorpay_payment_id": "pay_late", "razorpay_signature": "sig",
    }, format="json")

    sub.refresh_from_db()
    trial_restaurant.refresh_from_db()
    assert late_verify.status_code == 200 and late_verify.data["status"] == "cancelled"
    assert sub.status == "CANCELLED"
    assert trial_restaurant.status == "PAYMENT_DUE"
    assert not SubscriptionPayment.objects.filter(razorpay_payment_id__startswith="pay_late").exists()


def test_resubscribing_to_the_same_plan_after_cancelling_is_a_renewal(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    sub = _active_on(client, "GROWTH")
    client.post("/v1/subscriptions/cancel/")

    again = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert again.status_code == 201, again.data
    assert again.data["change"] == "renew"
    sub.refresh_from_db()
    assert again.data["first_charge_at"] == sub.current_end


# ---- 2026-10-08: plan changed by hand; several live subscriptions ----------------

def test_plan_set_by_hand_is_what_already_on_plan_is_judged_against(admin_client, trial_restaurant, prices, razorpay):
    """Bristo, live: on a Growth subscription, moved to Starter by a Super
    Admin, then checkout of Growth answered 409 already_on_plan."""
    _, client = admin_client
    growth = _active_on(client, "GROWTH")
    trial_restaurant.refresh_from_db()
    trial_restaurant.plan_tier = "STARTER"  # what PATCH /platform/tenants/{id}/plan/ does
    trial_restaurant.save(update_fields=["plan_tier"])

    checkout = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")
    assert checkout.status_code == 201, checkout.data
    assert checkout.data["change"] == "upgrade"
    client.post("/v1/subscriptions/verify/", {"razorpay_subscription_id": checkout.data["razorpay_subscription_id"],
                                             "razorpay_payment_id": "pay_hand", "razorpay_signature": "sig"}, format="json")

    trial_restaurant.refresh_from_db()
    growth.refresh_from_db()
    assert trial_restaurant.plan_tier == "GROWTH"  # applied at verify, an upgrade from Starter
    assert growth.cancel_at_period_end is True


def test_authorising_a_plan_cancels_every_other_live_one(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    _active_on(client, "GROWTH")
    starter = _checkout_and_verify(client, "STARTER")  # a downgrade waiting for the period end
    assert client.get("/v1/subscriptions/current/").data["pending_plan_id"] == "STARTER"

    _checkout_and_verify(client, "ENTERPRISE")

    starter.refresh_from_db()
    assert starter.cancel_at_period_end is True
    razorpay["cancel"].assert_any_call(starter.razorpay_subscription_id, at_cycle_end=False)  # never billed: cancelled now
    current = client.get("/v1/subscriptions/current/").data
    assert current["plan_id"] == "ENTERPRISE" and current["pending_plan_id"] is None



# ---- 2026-10-12: paying again on the same plan (app team's question) -------------

@pytest.mark.parametrize("ending_event", ["subscription.cancelled", "subscription.halted"])
def test_payment_due_can_resubscribe_to_the_same_plan(admin_client, trial_restaurant, prices, razorpay, ending_event):
    _, client = admin_client
    sub = _active_on(client, "GROWTH")
    trial_restaurant.status = "ACTIVE"  # past the trial
    trial_restaurant.save(update_fields=["status"])
    _webhook(ending_event, sub)
    trial_restaurant.refresh_from_db()
    assert trial_restaurant.status == "PAYMENT_DUE" and trial_restaurant.plan_tier == "GROWTH"

    again = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert again.status_code == 201, again.data
    assert again.data["change"] == "new" and again.data["first_charge_at"] is None  # charged now
    renewed = _checkout_and_verify(client, "GROWTH")
    trial_restaurant.refresh_from_db()
    assert renewed.status == "AUTHENTICATED" and trial_restaurant.status == "ACTIVE"


def test_same_plan_is_refused_while_razorpay_is_still_retrying(admin_client, trial_restaurant, prices, razorpay):
    """past_due with the organization still open: Razorpay retries the
    mandate itself; a second subscription for the same plan is not taken."""
    _, client = admin_client
    sub = _active_on(client, "GROWTH")
    _webhook("subscription.pending", sub)

    assert client.get("/v1/subscriptions/current/").data["status"] == "past_due"
    assert client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json").status_code == 409



# ---- 2026-10-12: sync from Razorpay at verify and on payment events ------------

def _rzp_sub(sub_id, status, start=None, end=None):
    now = int(timezone.now().timestamp())
    return {"id": sub_id, "status": status, "current_start": start if start is not None else now, "current_end": end if end is not None else now + 30 * 86400}


def test_verify_takes_period_dates_and_first_charge_from_razorpay(admin_client, trial_restaurant, prices, razorpay):
    """Altaza, live: active, but current_period_* and renews_at null - they
    only ever came from webhooks that weren't ticked."""
    _, client = admin_client
    trial_restaurant.status = "ACTIVE"
    trial_restaurant.save(update_fields=["status"])
    checkout = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")
    sub_id = checkout.data["razorpay_subscription_id"]
    end = int(timezone.now().timestamp()) + 30 * 86400
    with patch("core.razorpay_client.fetch_subscription", return_value=_rzp_sub(sub_id, "active", end=end)), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": [{"status": "paid", "payment_id": "pay_first"}]}), \
         patch("core.razorpay_client.fetch_payment", return_value={"id": "pay_first", "amount": 249900, "currency": "INR", "method": "emandate", "invoice_id": "inv_first"}):
        verified = client.post("/v1/subscriptions/verify/", {"razorpay_subscription_id": sub_id, "razorpay_payment_id": "pay_first",
                                                            "razorpay_signature": "sig"}, format="json")

    assert verified.status_code == 200
    assert verified.data["status"] == "active" and verified.data["current_period_end"] is not None and verified.data["renews_at"] is not None
    assert client.get("/v1/subscriptions/invoices/").data["count"] == 1


def test_verify_still_works_when_razorpay_cannot_be_reached(admin_client, trial_restaurant, prices, razorpay):
    from core.razorpay_client import RazorpayUnavailableError

    _, client = admin_client
    checkout = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")
    with patch("core.razorpay_client.fetch_subscription", side_effect=RazorpayUnavailableError("down")):
        verified = client.post("/v1/subscriptions/verify/", {"razorpay_subscription_id": checkout.data["razorpay_subscription_id"],
                                                            "razorpay_payment_id": "pay_x", "razorpay_signature": "sig"}, format="json")

    assert verified.status_code == 200 and verified.data["plan_id"] == "GROWTH"


def test_payment_captured_for_an_invoice_syncs_the_subscription(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    sub = _checkout_and_verify(client, "GROWTH")
    payment = {"id": "pay_renew", "order_id": "order_inv", "invoice_id": "inv_renew", "amount": 249900, "currency": "INR",
               "method": "card", "status": "captured"}
    payload = {"event": "payment.captured", "payload": {"payment": {"entity": payment}}}
    with patch("apps.payments.views.verify_webhook_signature", return_value=None), \
         patch("core.razorpay_client.fetch_invoice", return_value={"id": "inv_renew", "subscription_id": sub.razorpay_subscription_id}), \
         patch("core.razorpay_client.fetch_subscription", return_value=_rzp_sub(sub.razorpay_subscription_id, "active")), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": [{"status": "paid", "payment_id": "pay_renew"}]}), \
         patch("core.razorpay_client.fetch_payment", return_value=payment):
        response = APIClient().post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig")

    assert response.status_code == 200 and "synced" in response.data["detail"]
    sub.refresh_from_db()
    assert sub.status == "ACTIVE" and sub.current_end is not None
    assert SubscriptionPayment.objects.filter(razorpay_payment_id="pay_renew", status="PAID").exists()


def test_payment_failed_for_an_invoice_records_it_and_marks_past_due(admin_client, trial_restaurant, prices, razorpay):
    _, client = admin_client
    sub = _active_on(client, "GROWTH")
    payment = {"id": "pay_fail", "order_id": "order_inv2", "invoice_id": "inv_fail", "amount": 249900, "currency": "INR", "status": "failed"}
    payload = {"event": "payment.failed", "payload": {"payment": {"entity": payment}}}
    with patch("apps.payments.views.verify_webhook_signature", return_value=None), \
         patch("core.razorpay_client.fetch_invoice", return_value={"id": "inv_fail", "subscription_id": sub.razorpay_subscription_id}), \
         patch("core.razorpay_client.fetch_subscription", return_value=_rzp_sub(sub.razorpay_subscription_id, "pending")), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}):
        APIClient().post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig")

    assert client.get("/v1/subscriptions/current/").data["status"] == "past_due"
    assert SubscriptionPayment.objects.filter(razorpay_payment_id="pay_fail", status="FAILED").exists()


def test_payment_event_for_an_unknown_invoice_is_still_200(api_client):
    payload = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_s", "order_id": "order_x", "invoice_id": "inv_9"}}}}
    with patch("apps.payments.views.verify_webhook_signature", return_value=None), \
         patch("core.razorpay_client.fetch_invoice", return_value={"id": "inv_9", "subscription_id": "sub_not_ours"}):
        response = api_client.post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig")

    assert response.status_code == 200 and "ignored" in response.data["detail"]



def test_opening_the_screen_picks_up_what_razorpay_knows_now(admin_client, trial_restaurant, prices, razorpay):
    """Altaza, live: an e-mandate subscription stays "created" on Razorpay
    until the bank confirms the mandate; when it turns active only a
    subscription.* event says so. Reading current/ syncs it instead."""
    from django.core.cache import cache

    _, client = admin_client
    sub = _checkout_and_verify(client, "GROWTH")
    cache.clear()
    end = int(timezone.now().timestamp()) + 30 * 86400
    with patch("core.razorpay_client.fetch_subscription", return_value=_rzp_sub(sub.razorpay_subscription_id, "active", end=end)) as fetch, \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": [{"status": "paid", "payment_id": "pay_mandate"}]}), \
         patch("core.razorpay_client.fetch_payment", return_value={"id": "pay_mandate", "amount": 249900, "currency": "INR", "method": "emandate"}):
        current = client.get("/v1/subscriptions/current/").data
        client.get("/v1/subscriptions/current/")  # within 10 minutes: no second Razorpay call

    assert current["status"] == "active" and current["current_period_end"] is not None and current["renews_at"] is not None
    assert fetch.call_count == 1
    assert client.get("/v1/subscriptions/invoices/").data["count"] == 1
    cache.clear()


def test_screen_still_loads_when_razorpay_is_down(admin_client, trial_restaurant, prices, razorpay):
    from django.core.cache import cache

    from core.razorpay_client import RazorpayUnavailableError

    _, client = admin_client
    _checkout_and_verify(client, "GROWTH")
    cache.clear()
    with patch("core.razorpay_client.fetch_subscription", side_effect=RazorpayUnavailableError("down")):
        response = client.get("/v1/subscriptions/current/")

    assert response.status_code == 200 and response.data["plan_id"] == "GROWTH"
    cache.clear()
