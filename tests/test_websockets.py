import json

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.kitchen.models import KDSDevice
from apps.tables.models import Table, TableSession
from apps.websockets.consumers import KitchenConsumer, TableConsumer
from apps.websockets.middleware import KDSAuthMiddleware, TableSessionAuthMiddleware

pytestmark = pytest.mark.django_db(transaction=True)


@database_sync_to_async
def _create_table_and_session():
    table = Table.objects.create(table_number="9", capacity=4)
    session = TableSession.objects.create(table=table, status=TableSession.Status.ACTIVE)
    return table, session


@database_sync_to_async
def _close_session(session):
    session.status = TableSession.Status.CLOSED
    session.save(update_fields=["status"])


@database_sync_to_async
def _create_kds_device():
    return KDSDevice.objects.create(label="Test Device")


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
    table, session = await _create_table_and_session()
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
    table, session = await _create_table_and_session()

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
    device = await _create_kds_device()

    app = KDSAuthMiddleware(KitchenConsumer.as_asgi())
    communicator = WebsocketCommunicator(app, f"/ws/kitchen/?kds_key={device.api_key}")
    connected, _ = await communicator.connect()
    assert connected is True

    handshake = await communicator.receive_from()
    assert json.loads(handshake)["type"] == "connected"
    await communicator.disconnect()
