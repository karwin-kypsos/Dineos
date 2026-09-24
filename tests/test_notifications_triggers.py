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


def test_clear_all_notifications(admin_client, restaurant):
    admin_user, client = admin_client
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="One")
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Two")
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Already read", is_read=True)

    response = client.delete("/v1/notifications/clear-all/")

    assert response.status_code == 200
    assert response.data["deleted"] == 3
    assert not Notification.objects.filter(recipient=admin_user).exists()


def test_clear_all_notifications_only_touches_own_notifications(admin_client, manager_client, restaurant):
    admin_user, admin = admin_client
    manager_user, _ = manager_client
    Notification.objects.create(recipient=admin_user, type="STAFF_ADDED", title="Admin's")
    manager_notification = Notification.objects.create(recipient=manager_user, type="STAFF_ADDED", title="Manager's")

    response = admin.delete("/v1/notifications/clear-all/")

    assert response.status_code == 200
    assert response.data["deleted"] == 1
    assert Notification.objects.filter(pk=manager_notification.pk).exists()


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


def test_record_wastage_does_not_notify_when_stock_goes_critical(manager_client, admin_client, restaurant):
    """2026-09-14, per Karwin: notify on `low` only, never on `critical`
    (stock at or below zero). Note the side effect this accepts - a single
    deduction straight from healthy to zero skips the alert entirely,
    since it never passes through `low`."""
    _, manager = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Ginger", unit="KG",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("5.00"),
    )

    response = manager.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "10.00", "wastage_reason": "SPOILED"}, format="json",
    )

    assert response.status_code == 200, response.data
    ingredient.refresh_from_db()
    assert ingredient.stock_status == "critical"
    assert not Notification.objects.filter(
        recipient=admin_user, type="LOW_STOCK", data__ingredient_id=str(ingredient.id),
    ).exists()


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


def test_order_ready_notifies_only_the_assigned_server_not_every_server_on_the_branch(
    django_capture_on_commit_callbacks, server_client, restaurant, branch, table, menu_item
):
    """2026-09-15, per Shereena: a table assigned to Server A was alerting
    other servers too. The 2026-09-08 fix narrowed this from
    restaurant-wide to branch-wide; it needed to go the rest of the way to
    the one server actually looking after that table."""
    from apps.authentication.models import User
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    assigned, _ = server_client
    assigned.branch = branch
    assigned.save(update_fields=["branch"])
    table.branch = branch
    table.save(update_fields=["branch"])

    # A second, equally-active server on the SAME branch who owns no table here.
    other = User.objects.create_user(
        email="other-server@test.dineos", password="Test@1234", role="SERVER",
        name="Other Server", restaurant=restaurant, branch=branch,
    )

    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(
        session.id, [{"menu_item_id": menu_item.id, "quantity": 1}], placed_by=assigned,
    )
    session.refresh_from_db()
    assert session.assigned_server_id == assigned.id

    order_services.advance_kitchen_status(order.id, "ACCEPTED")
    order_services.advance_kitchen_status(order.id, "PREPARING")
    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_kitchen_status(order.id, "READY")

    assert Notification.objects.filter(recipient=assigned, type="ORDER_READY").exists()
    assert not Notification.objects.filter(recipient=other, type="ORDER_READY").exists()


def test_order_ready_falls_back_to_branch_servers_when_no_one_is_assigned(
    django_capture_on_commit_callbacks, server_client, branch, table, menu_item
):
    """Deliberate fallback: a session with no assigned server (legacy row,
    or nobody active to assign to when its first order landed) must still
    alert the branch's servers. A missed 'food is ready' is worse than one
    extra buzz."""
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    server_user, _ = server_client
    server_user.branch = branch
    server_user.save(update_fields=["branch"])
    table.branch = branch
    table.save(update_fields=["branch"])

    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    # Clear whatever round-robin assigned, to simulate an unassigned session.
    session.assigned_server = None
    session.save(update_fields=["assigned_server"])

    order_services.advance_kitchen_status(order.id, "ACCEPTED")
    order_services.advance_kitchen_status(order.id, "PREPARING")
    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_kitchen_status(order.id, "READY")

    assert Notification.objects.filter(recipient=server_user, type="ORDER_READY").exists()


def test_order_ready_does_not_cross_branch_leak_to_other_branch_server(
    django_capture_on_commit_callbacks, restaurant
):
    """Regression (2026-09-08, Shereena): a Branch A order going READY was
    also notifying Branch B's server - notify_role() resolved table/order
    into a branch for the Notification row's own metadata, but never
    actually used it to filter WHO got notified."""
    from apps.authentication.models import User
    from apps.menu.models import Category, MenuItem
    from apps.orders import services as order_services
    from apps.restaurant.models import Branch
    from apps.tables import services as table_services
    from apps.tables.models import Table

    branch_a = Branch.objects.create(restaurant=restaurant, name="Branch A")
    branch_b = Branch.objects.create(restaurant=restaurant, name="Branch B")
    server_a = User.objects.create_user(
        email="server-a@test.dineos", password="Test@1234", role="SERVER", restaurant=restaurant, branch=branch_a,
    )
    server_b = User.objects.create_user(
        email="server-b@test.dineos", password="Test@1234", role="SERVER", restaurant=restaurant, branch=branch_b,
    )
    category = Category.objects.create(restaurant=restaurant, name="Branch A Mains", branch=branch_a, sort_order=1)
    item = MenuItem.objects.create(category=category, name="Branch A Dish", price="100.00")
    table_a = Table.objects.create(restaurant=restaurant, branch=branch_a, table_number="A1", capacity=4)

    session, _ = table_services.get_or_create_active_session(table_a.id)
    order = order_services.place_order(session.id, [{"menu_item_id": item.id, "quantity": 1}])
    order_services.advance_kitchen_status(order.id, "ACCEPTED")
    order_services.advance_kitchen_status(order.id, "PREPARING")
    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_kitchen_status(order.id, "READY")

    assert Notification.objects.filter(recipient=server_a, type="ORDER_READY").exists()
    assert not Notification.objects.filter(recipient=server_b, type="ORDER_READY").exists()


def test_low_stock_notifies_own_branch_manager_and_admin_but_not_other_branch_manager(
    admin_client, restaurant
):
    """Same regression as above, for LOW_STOCK -> ADMIN + MANAGER. Admin has
    no fixed branch and is meant to see every branch's alerts."""
    from apps.authentication.models import User
    from apps.inventory import services as inventory_services
    from apps.inventory.models import Ingredient
    from apps.restaurant.models import Branch

    admin_user, _ = admin_client
    branch_a = Branch.objects.create(restaurant=restaurant, name="Branch A")
    branch_b = Branch.objects.create(restaurant=restaurant, name="Branch B")
    manager_a = User.objects.create_user(
        email="manager-a@test.dineos", password="Test@1234", role="MANAGER", restaurant=restaurant, branch=branch_a,
    )
    manager_b = User.objects.create_user(
        email="manager-b@test.dineos", password="Test@1234", role="MANAGER", restaurant=restaurant, branch=branch_b,
    )
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, branch=branch_a, name="Branch A Onions", unit="KG",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("5.00"),
    )

    inventory_services.record_wastage(ingredient.id, Decimal("6.00"), wastage_reason="SPOILED")

    assert Notification.objects.filter(recipient=manager_a, type="LOW_STOCK").exists()
    assert not Notification.objects.filter(recipient=manager_b, type="LOW_STOCK").exists()
    assert Notification.objects.filter(recipient=admin_user, type="LOW_STOCK").exists()


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
        "/v1/orders/takeaway/", {"customer_name": "Walk-in", "items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
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


def test_purchase_order_raised_notifies_admin(django_capture_on_commit_callbacks, manager_client, admin_client, restaurant):
    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Rice", unit="KG",
        current_stock=Decimal("2.00"), minimum_stock_level=Decimal("5.00"),
    )

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.create_purchase_order(
            restaurant=restaurant, branch=None,
            lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
            requested_by=manager_user,
        )

    assert Notification.objects.filter(recipient=admin_user, type="PURCHASE_ORDER_RAISED").exists()


def test_emergency_purchase_order_does_not_notify_admin(django_capture_on_commit_callbacks, manager_client, admin_client, restaurant):
    """Emergency POs auto-receive immediately (nothing left for Admin to
    approve/reject), so this should NOT fire the same PURCHASE_ORDER_RAISED
    notification a normal PO does."""
    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Salt", unit="KG",
        current_stock=Decimal("2.00"), minimum_stock_level=Decimal("5.00"),
    )

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.create_purchase_order(
            restaurant=restaurant, branch=None,
            lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
            requested_by=manager_user, is_emergency=True,
        )

    assert not Notification.objects.filter(recipient=admin_user, type="PURCHASE_ORDER_RAISED").exists()


def test_purchase_order_approved_notifies_requesting_manager(django_capture_on_commit_callbacks, manager_client, admin_client, restaurant):
    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Flour", unit="KG",
        current_stock=Decimal("2.00"), minimum_stock_level=Decimal("5.00"),
    )
    po = inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None,
        lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
        requested_by=manager_user,
    )

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.approve_purchase_order(po.id, approved_by=admin_user)

    assert Notification.objects.filter(recipient=manager_user, type="PURCHASE_ORDER_APPROVED").exists()


def test_purchase_order_rejected_notifies_requesting_manager(django_capture_on_commit_callbacks, manager_client, admin_client, restaurant):
    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Sugar", unit="KG",
        current_stock=Decimal("2.00"), minimum_stock_level=Decimal("5.00"),
    )
    po = inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None,
        lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
        requested_by=manager_user,
    )

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.reject_purchase_order(po.id, rejected_by=admin_user)

    assert Notification.objects.filter(recipient=manager_user, type="PURCHASE_ORDER_REJECTED").exists()


def test_purchase_order_marked_ordered_notifies_admin(django_capture_on_commit_callbacks, manager_client, admin_client, restaurant):
    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Oil", unit="L",
        current_stock=Decimal("2.00"), minimum_stock_level=Decimal("5.00"),
    )
    po = inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None,
        lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
        requested_by=manager_user,
    )
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.approve_purchase_order(po.id, approved_by=admin_user)

    # 2026-09-21: PURCHASE_ORDER_ORDERED no longer fires, because the
    # ORDERED state it announced no longer exists - the spec's flow goes
    # approve -> receive with no separate "placed with the supplier"
    # step. The requester still hears that their PO was approved, which
    # is the notification that actually mattered to them.
    assert Notification.objects.filter(recipient=manager_user, type="PURCHASE_ORDER_APPROVED").exists()
    assert not Notification.objects.filter(type="PURCHASE_ORDER_ORDERED").exists()


def test_paying_a_bill_clears_the_bill_requested_alert(cashier_client, table, menu_item, branch):
    """2026-09-21, per Shereena: the "Bill requested" alert stayed in the
    Cashier's list after the bill was paid. Once the session is closed the
    alert is stale and should stop demanding attention."""
    from apps.billing import services as billing_services
    from apps.orders import services as order_services
    from apps.tables import services as table_services

    cashier_user, _ = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])
    table.branch = branch
    table.save(update_fields=["branch"])

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    table_services.request_bill(session.id)
    table_services._notify_bill_requested(session, table.restaurant)

    alert = Notification.objects.filter(recipient=cashier_user, type="BILL_REQUESTED").first()
    assert alert is not None and alert.is_read is False

    billing_services.pay_bill(session.id, "CASH", cashier_user)

    alert.refresh_from_db()
    assert alert.is_read is True, "bill-requested alert should be dismissed once paid"


def test_manager_override_close_also_clears_the_bill_requested_alert(
    cashier_client, manager_client, table, menu_item, branch
):
    """Same clearing applies when a Manager force-closes the table instead
    of it being paid - the alert is equally stale either way."""
    from apps.orders import services as order_services
    from apps.tables import services as table_services
    from apps.tables.models import TableSession

    cashier_user, _ = cashier_client
    manager_user, _ = manager_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])
    table.branch = branch
    table.save(update_fields=["branch"])

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    table_services.request_bill(session.id)
    table_services._notify_bill_requested(session, table.restaurant)

    alert = Notification.objects.filter(recipient=cashier_user, type="BILL_REQUESTED").first()
    assert alert is not None and alert.is_read is False

    table_services.close_session(
        session, reason=TableSession.CloseReason.MANAGER_OVERRIDE, closed_by=manager_user,
    )

    alert.refresh_from_db()
    assert alert.is_read is True


def _stock_ingredient(restaurant, start, minimum=Decimal("5.00")):
    return Ingredient.objects.create(
        restaurant=restaurant, name=f"Ing {start}", unit="KG",
        current_stock=start, minimum_stock_level=minimum,
    )


def test_running_out_completely_now_raises_a_critical_alert(restaurant, admin_client, manager_client, django_capture_on_commit_callbacks):
    """2026-09-21, per Karwin. Hitting zero used to produce NO alert at
    all: the 14 Sep change suppressed `critical` to stop double-alerting,
    which left the single most urgent case silent."""
    from apps.inventory import services as inventory_services

    ingredient = _stock_ingredient(restaurant, Decimal("3.00"))  # already low
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(
            ingredient.id, Decimal("3.00"), wastage_reason="SPOILED",
        )

    assert Notification.objects.filter(type="CRITICAL_STOCK").exists()
    note = Notification.objects.filter(type="CRITICAL_STOCK").first()
    assert "Out of stock" in note.title


def test_healthy_straight_to_zero_still_alerts(restaurant, admin_client, manager_client, django_capture_on_commit_callbacks):
    """The gap the old logic left widest: one big deduction from healthy
    past `low` to zero never passed through `low`, so it alerted nothing."""
    from apps.inventory import services as inventory_services

    ingredient = _stock_ingredient(restaurant, Decimal("40.00"))
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(
            ingredient.id, Decimal("40.00"), wastage_reason="SPOILED",
        )

    assert Notification.objects.filter(type="CRITICAL_STOCK").exists()
    assert not Notification.objects.filter(type="LOW_STOCK").exists(), "it never passed through low"


def test_crossing_into_low_still_raises_only_a_low_alert(restaurant, admin_client, manager_client, django_capture_on_commit_callbacks):
    from apps.inventory import services as inventory_services

    ingredient = _stock_ingredient(restaurant, Decimal("40.00"))
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(
            ingredient.id, Decimal("36.00"), wastage_reason="SPOILED",
        )

    assert Notification.objects.filter(type="LOW_STOCK").exists()
    assert not Notification.objects.filter(type="CRITICAL_STOCK").exists()


def test_each_threshold_announces_itself_once(restaurant, admin_client, manager_client, django_capture_on_commit_callbacks):
    """Healthy -> low -> critical must produce exactly one of each, and
    staying critical must not keep alerting."""
    from apps.inventory import services as inventory_services

    ingredient = _stock_ingredient(restaurant, Decimal("40.00"))
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(ingredient.id, Decimal("36.00"), wastage_reason="SPOILED")
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(ingredient.id, Decimal("2.00"), wastage_reason="SPOILED")
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(ingredient.id, Decimal("1.00"), wastage_reason="SPOILED")
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.deduct_for_usage(ingredient.id, Decimal("1.00"))

    low = Notification.objects.filter(type="LOW_STOCK").count()
    critical = Notification.objects.filter(type="CRITICAL_STOCK").count()
    recipients = Notification.objects.filter(type="LOW_STOCK").values_list("recipient_id", flat=True)
    # One per recipient (Admin + Manager), not one per deduction.
    assert low == len(set(recipients)), f"low alert repeated: {low} for {len(set(recipients))} recipients"
    assert critical == len(set(recipients)), f"critical alert repeated: {critical}"


def test_restocking_back_up_does_not_alert(restaurant, admin_client, manager_client, django_capture_on_commit_callbacks):
    """Going the other way is good news. A restock that lifts an
    ingredient from critical back to low must not fire a low alert."""
    from apps.inventory import services as inventory_services

    ingredient = _stock_ingredient(restaurant, Decimal("1.00"))
    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_wastage(ingredient.id, Decimal("1.00"), wastage_reason="SPOILED")
    Notification.objects.all().delete()

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.add_stock(ingredient.id, Decimal("2.00"))  # critical -> low

    assert not Notification.objects.filter(type__in=["LOW_STOCK", "CRITICAL_STOCK"]).exists()


def test_closing_a_short_shipped_po_notifies_admin_with_the_reason(
    restaurant, admin_client, manager_client, django_capture_on_commit_callbacks
):
    """2026-09-22, per Karwin. The reason typed at close time is the whole
    point of this alert - it is the short-shipment explanation nobody
    sees otherwise - so it has to reach the body, not just the title."""
    from apps.inventory import services as inventory_services

    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Saffron", unit="G",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("2.00"),
    )
    po = inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None,
        lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
        supplier_name="Kesar Traders", requested_by=manager_user,
    )
    inventory_services.approve_purchase_order(po.id, approved_by=admin_user)
    line = po.lines.first()
    inventory_services.record_goods_receipt(
        po.id, [{"line": line, "received_quantity": Decimal("4.00")}], received_by=admin_user,
    )

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.close_purchase_order(
            po.id, reason="Supplier discontinued the item", closed_by=admin_user,
        )

    notes = Notification.objects.filter(type="PURCHASE_ORDER_CLOSED")
    assert notes.exists()
    note = notes.filter(recipient=admin_user).first()
    assert note is not None, "the Admin must receive it"
    assert "Kesar Traders" in note.title
    assert note.body == "Supplier discontinued the item"
    assert note.data["purchase_order_id"] == str(po.id)


def test_receiving_goods_never_notifies(
    restaurant, admin_client, manager_client, django_capture_on_commit_callbacks
):
    """Explicitly confirmed with Karwin: PARTIALLY_RECEIVED and
    FULLY_RECEIVED stay silent. A delivery arriving is routine; alerting
    on it would bury the one PO alert that matters."""
    from apps.inventory import services as inventory_services

    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Cardamom", unit="G",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("2.00"),
    )
    po = inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None,
        lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
        requested_by=manager_user,
    )
    inventory_services.approve_purchase_order(po.id, approved_by=admin_user)
    line = po.lines.first()

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_goods_receipt(
            po.id, [{"line": line, "received_quantity": Decimal("4.00")}], received_by=admin_user,
        )
    po.refresh_from_db()
    assert po.status == "PARTIALLY_RECEIVED"
    assert not Notification.objects.filter(type="PURCHASE_ORDER_CLOSED").exists()

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.record_goods_receipt(
            po.id, [{"line": line, "received_quantity": Decimal("6.00")}], received_by=admin_user,
        )
    po.refresh_from_db()
    assert po.status == "FULLY_RECEIVED"
    assert not Notification.objects.filter(type="PURCHASE_ORDER_CLOSED").exists()


def test_close_notification_fires_exactly_once(
    restaurant, admin_client, manager_client, django_capture_on_commit_callbacks
):
    """A second close attempt is refused, so the alert cannot be doubled
    by a double-tap."""
    from apps.inventory import services as inventory_services

    manager_user, _ = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Cloves", unit="G",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("2.00"),
    )
    po = inventory_services.create_purchase_order(
        restaurant=restaurant, branch=None,
        lines=[{"ingredient": ingredient, "quantity_ordered": Decimal("10.00")}],
        requested_by=manager_user,
    )
    inventory_services.approve_purchase_order(po.id, approved_by=admin_user)
    inventory_services.record_goods_receipt(
        po.id, [{"line": po.lines.first(), "received_quantity": Decimal("4.00")}], received_by=admin_user,
    )

    with django_capture_on_commit_callbacks(execute=True):
        inventory_services.close_purchase_order(po.id, reason="first", closed_by=admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ValueError):
            inventory_services.close_purchase_order(po.id, reason="second", closed_by=admin_user)

    assert Notification.objects.filter(type="PURCHASE_ORDER_CLOSED").count() == 1
