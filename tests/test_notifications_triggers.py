from decimal import Decimal

import pytest
from django.utils import timezone

from apps.inventory import services as inventory_services
from apps.inventory.models import Ingredient
from apps.notifications.models import Notification
from apps.tables.models import Table

pytestmark = pytest.mark.django_db


def test_notification_list_defaults_to_today_only(admin_client, restaurant):
    admin_user, client = admin_client
    today = Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Today's")
    old = Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Old one")
    old.created_at = timezone.now() - timezone.timedelta(days=5)
    old.save(update_fields=["created_at"])

    response = client.get("/v1/notifications/")

    assert response.status_code == 200
    titles = [n["title"] for n in response.data]
    assert titles == ["Today's"]


def test_notification_list_all_true_returns_everything(admin_client, restaurant):
    admin_user, client = admin_client
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Today's")
    old = Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Old one")
    old.created_at = timezone.now() - timezone.timedelta(days=5)
    old.save(update_fields=["created_at"])

    response = client.get("/v1/notifications/?all=true")

    assert response.status_code == 200
    titles = {n["title"] for n in response.data}
    assert titles == {"Today's", "Old one"}


def test_notification_list_includes_table_number(admin_client, restaurant):
    admin_user, client = admin_client
    table = Table.objects.create(restaurant=restaurant, table_number="7", capacity=4)
    Notification.objects.create(
        recipient=admin_user, type="BILL_REQUESTED", title="Bill requested", table=table,
    )
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="No table on this one")

    response = client.get("/v1/notifications/")

    assert response.status_code == 200
    by_title = {n["title"]: n for n in response.data}
    assert by_title["Bill requested"]["table_number"] == "7"
    assert by_title["No table on this one"]["table_number"] is None


def test_mark_all_notifications_read(admin_client, restaurant):
    admin_user, client = admin_client
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="One")
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Two")
    already_read = Notification.objects.create(
        recipient=admin_user, type="STAFF_ADDED", title="Already read", is_read=True,
    )

    response = client.patch("/v1/notifications/read-all/")

    assert response.status_code == 200
    assert response.data["marked_read"] == 2
    assert not Notification.objects.filter(recipient=admin_user, is_read=False).exists()
    already_read.refresh_from_db()
    assert already_read.is_read is True


def test_mark_all_notifications_read_only_touches_own_notifications(admin_client, manager_client, restaurant):
    admin_user, admin = admin_client
    manager_user, _ = manager_client
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Admin's")
    manager_notification = Notification.objects.create(recipient=manager_user, type="STAFF_ADDED", title="Manager's")

    response = admin.patch("/v1/notifications/read-all/")

    assert response.status_code == 200
    assert response.data["marked_read"] == 1
    manager_notification.refresh_from_db()
    assert manager_notification.is_read is False


def test_record_wastage_notifies_on_newly_low_stock(manager_client, admin_client, restaurant):
    _, manager = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Onions", unit="KG",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("5.00"),
    )

    response = manager.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "6.00", "wastage_reason": "SPOILED"}, format="json",
    )

    assert response.status_code == 200, response.data
    assert Notification.objects.filter(recipient=admin_user, type="LOW_STOCK").exists()


def test_record_wastage_does_not_renotify_when_already_low(manager_client, admin_client, restaurant):
    _, manager = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Garlic", unit="KG",
        current_stock=Decimal("3.00"), minimum_stock_level=Decimal("5.00"),
    )

    manager.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "1.00", "wastage_reason": "SPOILED"}, format="json",
    )

    assert Notification.objects.filter(recipient=admin_user, type="LOW_STOCK", data__ingredient_id=str(ingredient.id)).count() == 0


def test_create_staff_notifies_admins(admin_client, restaurant):
    admin_user, client = admin_client

    response = client.post(
        "/v1/staff/", {"email": "newhire@demo-bistro.demo", "name": "New Hire", "role": "SERVER"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert Notification.objects.filter(recipient=admin_user, type="STAFF_ADDED").exists()


def test_add_portions_notifies_admins_and_managers(manager_client, admin_client, menu_item):
    _, manager = manager_client
    admin_user, _ = admin_client

    response = manager.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 5}, format="json",
    )

    assert response.status_code == 200, response.data
    assert Notification.objects.filter(recipient=admin_user, type="PREP_LOGGED").exists()


def test_dine_in_order_ready_notifies_server_not_cashier(
    django_capture_on_commit_callbacks, cashier_client, server_client, table, menu_item
):
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    cashier_user, _ = cashier_client
    server_user, _ = server_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    order_services.advance_kitchen_status(order.id, "ACCEPTED")
    order_services.advance_kitchen_status(order.id, "PREPARING")
    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_kitchen_status(order.id, "READY")

    assert Notification.objects.filter(recipient=server_user, type="ORDER_READY").exists()
    assert not Notification.objects.filter(recipient=cashier_user, type="ORDER_READY").exists()


def test_takeaway_order_ready_notifies_cashier_not_server(
    django_capture_on_commit_callbacks, cashier_client, server_client, menu_item, branch
):
    """Regression (2026-08-26, Shereena): takeaway has no table/assigned
    server, so "ORDER_READY -> SERVER" used to ping every server about
    something none of them could act on, while the Cashier — who actually
    hands it over / collects payment — got nothing."""
    from apps.orders import services as order_services

    cashier_user, cashier = cashier_client
    server_user, _ = server_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])

    create = cashier.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    order_id = create.data["id"]

    order_services.advance_kitchen_status(order_id, "ACCEPTED")
    order_services.advance_kitchen_status(order_id, "PREPARING")
    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_kitchen_status(order_id, "READY")

    assert Notification.objects.filter(recipient=cashier_user, type="ORDER_READY").exists()
    assert not Notification.objects.filter(recipient=server_user, type="ORDER_READY").exists()


def test_cleanup_notifications_command_purges_only_past_the_cutoff(admin_client):
    from io import StringIO

    from django.core.management import call_command

    admin_user, _ = admin_client
    recent = Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Recent")
    old = Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Ancient")
    old.created_at = timezone.now() - timezone.timedelta(days=45)
    old.save(update_fields=["created_at"])

    out = StringIO()
    call_command("cleanup_notifications", "--days=30", stdout=out)

    assert not Notification.objects.filter(id=old.id).exists()
    assert Notification.objects.filter(id=recent.id).exists()
    assert "Deleted 1 notification" in out.getvalue()
