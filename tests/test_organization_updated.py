"""organization_updated (2026-10-08, per Karwin): every staff app of a
restaurant hears, live, when its plan, feature flags or status change -
by a Super Admin or by the subscription system - instead of polling /me."""
import json
from unittest.mock import patch

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from rest_framework.test import APIClient

from apps.platform.models import PlatformAdmin


def _platform_client():
    PlatformAdmin.objects.create_user(email="super@orgupdated.test", password="Test@1234")
    client = APIClient()
    login = client.post("/platform/auth/login/", {"email": "super@orgupdated.test", "password": "Test@1234"}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": "super@orgupdated.test", "code": login.data["code"]}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


@pytest.fixture
def sent():
    with patch("apps.restaurant.realtime._send") as send:
        yield send


@pytest.mark.django_db
@pytest.mark.parametrize("path, body", [
    ("feature-flags/", {"kitchen_enabled": False}),
    ("status/", {"status": "PAYMENT_DUE"}),
    ("plan/", {"plan_tier": "STARTER"}),
    ("", {"name": "Renamed Kitchen"}),
])
def test_super_admin_changes_reach_every_staff_app(admin_client, restaurant, sent, django_capture_on_commit_callbacks, path, body):
    _, admin = admin_client
    platform = _platform_client()

    with django_capture_on_commit_callbacks(execute=True):
        response = platform.patch(f"/platform/tenants/{restaurant.id}/{path}", body, format="json")

    assert response.status_code == 200, response.data
    sent.assert_called_once()
    restaurant_id, data = sent.call_args.args
    me = admin.get("/v1/auth/me/").data["restaurant"]
    assert restaurant_id == restaurant.id
    assert data == {**me, "organization_status": response.data["status"]}  # the /me object, whole


@pytest.mark.django_db
def test_nothing_is_sent_when_nothing_changed(restaurant, sent, django_capture_on_commit_callbacks):
    platform = _platform_client()

    with django_capture_on_commit_callbacks(execute=True):
        platform.patch(f"/platform/tenants/{restaurant.id}/feature-flags/", {"kitchen_enabled": restaurant.kitchen_enabled}, format="json")

    sent.assert_not_called()


@pytest.mark.django_db
def test_subscription_status_changes_are_sent(restaurant, sent, django_capture_on_commit_callbacks):
    from apps.subscriptions.models import Subscription

    restaurant.status = "ACTIVE"
    restaurant.save(update_fields=["status"])
    sub = Subscription.objects.create(restaurant=restaurant, plan_tier=restaurant.plan_tier, amount="999.00", razorpay_plan_id="plan_x",
                                      razorpay_subscription_id="sub_orgupd", status="ACTIVE", plan_applied_at="2026-10-01T00:00:00Z")
    payload = {"event": "subscription.halted", "payload": {"subscription": {"entity": {"id": sub.razorpay_subscription_id}}}}

    with patch("apps.payments.views.verify_webhook_signature", return_value=None), django_capture_on_commit_callbacks(execute=True):
        APIClient().post("/v1/payments/razorpay/webhook/", data=json.dumps(payload), content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="sig")

    sent.assert_called_once()
    assert sent.call_args.args[1]["organization_status"] == "PAYMENT_DUE"


@database_sync_to_async
def _staff_user():
    from apps.authentication.models import User
    from apps.restaurant.models import Restaurant

    restaurant = Restaurant.objects.create(name="Org Upd", slug="org-upd")
    return User.objects.create_user(email="orgupd@test.dineos", password="Pass@1234", name="Server", role="SERVER", restaurant=restaurant)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_staff_socket_receives_the_frame():
    from rest_framework_simplejwt.tokens import AccessToken

    from apps.websockets.consumers import StaffConsumer
    from apps.websockets.middleware import JWTAuthMiddleware

    user = await _staff_user()
    token = await database_sync_to_async(lambda: str(AccessToken.for_user(user)))()
    socket = WebsocketCommunicator(JWTAuthMiddleware(StaffConsumer.as_asgi()), f"/ws/staff/?token={token}")
    assert (await socket.connect())[0] is True
    await socket.receive_from()  # "connected"

    data = {"plan_tier": "STARTER", "organization_status": "ACTIVE", "kitchen_enabled": False}
    await get_channel_layer().group_send(f"staff_all_{user.restaurant_id}", {"type": "organization_updated", "data": data})

    assert json.loads(await socket.receive_from()) == {"type": "organization_updated", "data": data}
    await socket.disconnect()
