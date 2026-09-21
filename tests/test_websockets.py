import json

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.kitchen.models import KDSDevice
from apps.restaurant.models import Restaurant
from apps.tables.models import Table, TableSession
from apps.websockets.consumers import KitchenConsumer, TableConsumer
from apps.websockets.middleware import KDSAuthMiddleware, TableSessionAuthMiddleware

pytestmark = pytest.mark.django_db(transaction=True)


@database_sync_to_async
def _create_restaurant():
    return Restaurant.objects.create(name="WS Test Restaurant", slug="ws-test-restaurant")


@database_sync_to_async
def _create_table_and_session(restaurant):
    table = Table.objects.create(restaurant=restaurant, table_number="9", capacity=4)
    session = TableSession.objects.create(table=table, status=TableSession.Status.ACTIVE)
    return table, session


@database_sync_to_async
def _close_session(session):
    session.status = TableSession.Status.CLOSED
    session.save(update_fields=["status"])


@database_sync_to_async
def _create_kds_device(restaurant):
    return KDSDevice.objects.create(restaurant=restaurant, label="Test Device")


@pytest.mark.asyncio
async def test_table_consumer_rejects_unknown_session():
    app = TableSessionAuthMiddleware(TableConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, "/ws/table/00000000-0000-0000-0000-000000000000/")
    communicator.scope["url_route"] = {"kwargs": {"session_id": "00000000-0000-0000-0000-000000000000"}}
    connected, close_code = await communicator.connect()
    assert connected is False
    assert close_code == 4003
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_table_consumer_rejects_closed_session():
    restaurant = await _create_restaurant()
    table, session = await _create_table_and_session(restaurant)
    await _close_session(session)

    app = TableSessionAuthMiddleware(TableConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, f"/ws/table/{session.id}/")
    communicator.scope["url_route"] = {"kwargs": {"session_id": session.id}}
    connected, close_code = await communicator.connect()
    assert connected is False
    assert close_code == 4003
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_table_consumer_accepts_open_session_and_forwards_events():
    restaurant = await _create_restaurant()
    table, session = await _create_table_and_session(restaurant)

    app = TableSessionAuthMiddleware(TableConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, f"/ws/table/{session.id}/")
    communicator.scope["url_route"] = {"kwargs": {"session_id": session.id}}
    connected, _ = await communicator.connect()
    assert connected is True

    handshake = await communicator.receive_from()
    assert json.loads(handshake)["type"] == "connected"

    channel_layer = get_channel_layer()
    await channel_layer.group_send(
        f"table_session_{session.id}", {"type": "order_status_changed", "order_id": "abc", "status": "READY"}
    )
    frame = await communicator.receive_from()
    data = json.loads(frame)
    assert data["status"] == "READY"

    await channel_layer.group_send(f"table_session_{session.id}", {"type": "table_session_closed", "reason": "PAID"})
    frame_2 = await communicator.receive_from()
    assert json.loads(frame_2)["reason"] == "PAID"

    await communicator.disconnect()


@pytest.mark.asyncio
async def test_kitchen_consumer_rejects_missing_key():
    app = KDSAuthMiddleware(KitchenConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, "/ws/kitchen/")
    connected, close_code = await communicator.connect()
    assert connected is False
    assert close_code == 4003
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_kitchen_consumer_accepts_valid_key():
    restaurant = await _create_restaurant()
    device = await _create_kds_device(restaurant)

    app = KDSAuthMiddleware(KitchenConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, f"/ws/kitchen/?kds_key={device.api_key}")
    connected, _ = await communicator.connect()
    assert connected is True

    handshake = await communicator.receive_from()
    assert json.loads(handshake)["type"] == "connected"
    await communicator.disconnect()


@database_sync_to_async
def _create_staff(restaurant, role, email):
    from apps.authentication.models import User

    return User.objects.create_user(
        email=email, password="Pass@1234", name=role.title(), role=role, restaurant=restaurant
    )


async def _staff_socket(user):
    from rest_framework_simplejwt.tokens import AccessToken

    from apps.websockets.consumers import StaffConsumer
    from apps.websockets.middleware import JWTAuthMiddleware

    token = await database_sync_to_async(lambda: str(AccessToken.for_user(user)))()
    app = JWTAuthMiddleware(StaffConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, f"/ws/staff/?token={token}")
    connected, _ = await communicator.connect()
    assert connected is True
    handshake = json.loads(await communicator.receive_from())
    assert handshake["type"] == "connected"
    return communicator


@pytest.mark.asyncio
async def test_broadcast_frames_carry_their_event_name():
    """2026-09-21: the event name used to be stripped before sending, so
    order_new / order_status_changed / order_collected / order_served —
    which all send the identical _order_payload — were indistinguishable
    on the wire. Every frame must now say what it is.
    """
    restaurant = await _create_restaurant()
    server = await _create_staff(restaurant, "SERVER", "ws-server@ws-test.demo")
    socket = await _staff_socket(server)

    channel_layer = get_channel_layer()
    for event_name in ("order_new", "order_status_changed", "order_collected", "order_served"):
        await channel_layer.group_send(
            f"servers_{restaurant.id}", {"type": event_name, "order_id": "abc", "status": "READY"}
        )
        frame = json.loads(await socket.receive_from())
        assert frame["type"] == event_name, frame
        assert frame["order_id"] == "abc"

    await socket.disconnect()


@pytest.mark.asyncio
async def test_manager_receives_one_copy_of_a_cashier_and_manager_event():
    """2026-09-21, seen live in production: a Manager joins servers_,
    cashiers_ AND managers_, so a broadcast naming two of those groups
    delivered the same frame twice — a dashboard counting per event
    double-counted for Managers and Admins only.
    """
    from apps.websockets.groups import staff_groups

    restaurant = await _create_restaurant()
    manager = await _create_staff(restaurant, "MANAGER", "ws-manager@ws-test.demo")
    socket = await _staff_socket(manager)

    channel_layer = get_channel_layer()
    for group in staff_groups(restaurant.id, ["cashiers", "managers"]):
        await channel_layer.group_send(group, {"type": "payment_confirmed", "bill_id": "b1"})

    frame = json.loads(await socket.receive_from())
    assert frame["type"] == "payment_confirmed"
    assert await socket.receive_nothing(timeout=0.5), "manager got the same event twice"

    await socket.disconnect()


@pytest.mark.asyncio
async def test_collapsed_groups_still_reach_every_intended_role():
    """The collapse must never narrow the audience: servers + cashiers has
    to still reach a plain SERVER and a plain CASHIER, once each.
    """
    from apps.websockets.groups import staff_groups

    restaurant = await _create_restaurant()
    server = await _create_staff(restaurant, "SERVER", "ws-server2@ws-test.demo")
    cashier = await _create_staff(restaurant, "CASHIER", "ws-cashier2@ws-test.demo")
    manager = await _create_staff(restaurant, "MANAGER", "ws-manager2@ws-test.demo")
    sockets = {
        "SERVER": await _staff_socket(server),
        "CASHIER": await _staff_socket(cashier),
        "MANAGER": await _staff_socket(manager),
    }

    channel_layer = get_channel_layer()
    for group in staff_groups(restaurant.id, ["servers", "cashiers"]):
        await channel_layer.group_send(group, {"type": "order_status_changed", "order_id": "o1"})

    for role, socket in sockets.items():
        frame = json.loads(await socket.receive_from())
        assert frame["type"] == "order_status_changed", (role, frame)
        assert await socket.receive_nothing(timeout=0.5), f"{role} got it twice"
        await socket.disconnect()
