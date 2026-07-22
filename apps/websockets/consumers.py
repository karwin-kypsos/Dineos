import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

STAFF_ROLES = ("ADMIN", "MANAGER", "SERVER", "CASHIER")
ROLE_GROUPS = {
    "SERVER": ["servers"],
    "CASHIER": ["cashiers"],
    "MANAGER": ["servers", "cashiers", "managers"],
    "ADMIN": ["servers", "cashiers", "managers"],
}


class _BroadcastConsumer(AsyncWebsocketConsumer):
    """Shared forwarding body — every event handler just relays the event
    dict (minus the Channels "type" key) to the socket as JSON, matching the
    sibling project's dispatch-by-method-name convention.
    """

    async def _forward(self, event):
        payload = {k: v for k, v in event.items() if k != "type"}
        await self.send(text_data=json.dumps(payload))

    # Event name -> handler method name is Channels' standard convention:
    # a group_send with {"type": "order_new"} dispatches to self.order_new().
    order_new = _forward
    order_status_changed = _forward
    order_collected = _forward
    order_served = _forward
    table_bill_requested = _forward
    table_status_changed = _forward
    table_session_closed = _forward
    payment_confirmed = _forward
    portions_updated = _forward
    portions_zero = _forward
    notification_new = _forward


class KitchenConsumer(_BroadcastConsumer):
    async def connect(self):
        device = self.scope.get("kds_device")
        if device is None:
            await self.close(code=4003)
            return

        await self.channel_layer.group_add("kitchen", self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps({"type": "connected", "device": device.label}))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("kitchen", self.channel_name)


class StaffConsumer(_BroadcastConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4001)
            return
        if user.role not in STAFF_ROLES or not user.is_active:
            await self.close(code=4003)
            return

        self._groups = ["staff_all", f"notifications_{user.id}"] + ROLE_GROUPS.get(user.role, [])
        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)

        await self.accept()
        await self.send(text_data=json.dumps({"type": "connected", "role": user.role}))

    async def disconnect(self, close_code):
        for group in getattr(self, "_groups", []):
            await self.channel_layer.group_discard(group, self.channel_name)


class TableConsumer(_BroadcastConsumer):
    async def connect(self):
        session_id = self.scope["url_route"]["kwargs"]["session_id"]
        session = await self._get_open_session(session_id)
        if session is None:
            await self.close(code=4003)
            return

        self._groups = [f"table_session_{session_id}", f"table_{session['table_id']}", "customers_global"]
        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)

        await self.accept()
        await self.send(text_data=json.dumps({"type": "connected", "session_id": str(session_id)}))

    async def disconnect(self, close_code):
        for group in getattr(self, "_groups", []):
            await self.channel_layer.group_discard(group, self.channel_name)

    @database_sync_to_async
    def _get_open_session(self, session_id):
        from apps.tables.models import TableSession

        session = TableSession.objects.filter(id=session_id, status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        if session is None:
            return None
        return {"table_id": str(session.table_id)}
