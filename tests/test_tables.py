import pytest
from django.db import IntegrityError, transaction

from apps.tables import services
from apps.tables.models import Table, TableSession

pytestmark = pytest.mark.django_db


def _make_server(restaurant, branch, email, name):
    from apps.authentication.models import User

    return User.objects.create_user(
        email=email, password="Test@1234", role="SERVER", name=name, restaurant=restaurant, branch=branch,
    )


def test_one_open_session_per_table_enforced_at_db_level(table):
    TableSession.objects.create(table=table, status=TableSession.Status.ACTIVE)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TableSession.objects.create(table=table, status=TableSession.Status.ACTIVE)


def test_get_or_create_is_idempotent(table):
    session_1, created_1 = services.get_or_create_active_session(table.id)
    session_2, created_2 = services.get_or_create_active_session(table.id)
    assert created_1 is True
    assert created_2 is False
    assert session_1.id == session_2.id


def test_manager_override_marks_unpaid_and_closes_session(table, manager_client):
    manager_user, _ = manager_client
    session, _ = services.get_or_create_active_session(table.id)

    services.manager_override_status(table.id, status=Table.Status.AVAILABLE, mark_unpaid=True, manager=manager_user)

    session.refresh_from_db()
    assert session.status == TableSession.Status.CLOSED
    assert session.close_reason == TableSession.CloseReason.MANAGER_OVERRIDE

    table.refresh_from_db()
    assert table.status == Table.Status.AVAILABLE


def test_first_order_round_robins_across_active_servers(restaurant, branch, menu_item):
    from apps.orders import services as order_services

    server_a = _make_server(restaurant, branch, "srv-a@demo-bistro.demo", "Server A")
    server_b = _make_server(restaurant, branch, "srv-b@demo-bistro.demo", "Server B")

    table_1 = Table.objects.create(restaurant=restaurant, branch=branch, table_number="T1", capacity=4)
    table_2 = Table.objects.create(restaurant=restaurant, branch=branch, table_number="T2", capacity=4)
    session_1, _ = services.get_or_create_active_session(table_1.id)
    session_2, _ = services.get_or_create_active_session(table_2.id)

    order_services.place_order(session_1.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    order_services.place_order(session_2.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    session_1.refresh_from_db()
    session_2.refresh_from_db()
    assert session_1.assigned_server_id in (server_a.id, server_b.id)
    assert session_2.assigned_server_id in (server_a.id, server_b.id)
    assert session_1.assigned_server_id != session_2.assigned_server_id  # round-robin, not both to the same server


def test_second_order_does_not_reassign_server(restaurant, branch, menu_item):
    from apps.orders import services as order_services

    _make_server(restaurant, branch, "srv-c@demo-bistro.demo", "Server C")
    table = Table.objects.create(restaurant=restaurant, branch=branch, table_number="T3", capacity=4)
    session, _ = services.get_or_create_active_session(table.id)

    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    session.refresh_from_db()
    first_assignment = session.assigned_server_id

    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    session.refresh_from_db()
    assert session.assigned_server_id == first_assignment


def test_table_list_scopes_server_to_own_assigned_and_free_tables(restaurant, branch, menu_item):
    from rest_framework.test import APIClient

    from apps.authentication.serializers import DineOSTokenObtainPairSerializer
    from apps.orders import services as order_services

    server_a = _make_server(restaurant, branch, "srv-d@demo-bistro.demo", "Server D")
    server_b = _make_server(restaurant, branch, "srv-e@demo-bistro.demo", "Server E")

    table_free = Table.objects.create(restaurant=restaurant, branch=branch, table_number="F1", capacity=4)
    table_a = Table.objects.create(restaurant=restaurant, branch=branch, table_number="A1", capacity=4)
    table_b = Table.objects.create(restaurant=restaurant, branch=branch, table_number="B1", capacity=4)

    session_a, _ = services.get_or_create_active_session(table_a.id)
    session_a.assigned_server = server_a
    session_a.save(update_fields=["assigned_server"])

    session_b, _ = services.get_or_create_active_session(table_b.id)
    session_b.assigned_server = server_b
    session_b.save(update_fields=["assigned_server"])

    token = DineOSTokenObtainPairSerializer.get_token(server_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    response = client.get("/v1/tables/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    ids = {t["id"] for t in results}
    assert str(table_free.id) in ids
    assert str(table_a.id) in ids
    assert str(table_b.id) not in ids


def test_table_list_shows_stuck_empty_session_table_to_every_server(restaurant, branch):
    """Regression (2026-08-23, Shereena): a session created without ever
    placing an order has no assigned_server (assign_next_server only runs on
    the first order) — such a table used to match neither the AVAILABLE nor
    the assigned-to-me clause and vanished from every server's table list."""
    from rest_framework.test import APIClient

    from apps.authentication.serializers import DineOSTokenObtainPairSerializer

    server_a = _make_server(restaurant, branch, "srv-g@demo-bistro.demo", "Server G")

    stuck_table = Table.objects.create(restaurant=restaurant, branch=branch, table_number="S1", capacity=4)
    services.get_or_create_active_session(stuck_table.id)  # no order placed -> no assigned_server
    stuck_table.refresh_from_db()
    assert stuck_table.status == Table.Status.OCCUPIED  # confirms it's genuinely "stuck", not still AVAILABLE

    token = DineOSTokenObtainPairSerializer.get_token(server_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    response = client.get("/v1/tables/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    ids = {t["id"] for t in results}
    assert str(stuck_table.id) in ids


def test_table_list_still_hides_another_servers_assigned_empty_session_table(restaurant, branch):
    """The safety net above must not leak visibility into a table that IS
    already assigned to a different server, even if that session happens to
    have zero orders (synthetic here — assignment normally only happens
    alongside the first order, see assign_next_server)."""
    from rest_framework.test import APIClient

    from apps.authentication.serializers import DineOSTokenObtainPairSerializer

    server_a = _make_server(restaurant, branch, "srv-h@demo-bistro.demo", "Server H")
    server_b = _make_server(restaurant, branch, "srv-i@demo-bistro.demo", "Server I")

    table_b = Table.objects.create(restaurant=restaurant, branch=branch, table_number="S2", capacity=4)
    session_b, _ = services.get_or_create_active_session(table_b.id)
    session_b.assigned_server = server_b
    session_b.save(update_fields=["assigned_server"])

    token = DineOSTokenObtainPairSerializer.get_token(server_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    response = client.get("/v1/tables/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    ids = {t["id"] for t in results}
    assert str(table_b.id) not in ids


def test_table_list_excludes_branch_less_legacy_tables_for_server(restaurant, branch):
    from rest_framework.test import APIClient

    from apps.authentication.serializers import DineOSTokenObtainPairSerializer

    server = _make_server(restaurant, branch, "srv-f@demo-bistro.demo", "Server F")
    legacy_table = Table.objects.create(restaurant=restaurant, branch=None, table_number="Legacy1", capacity=4)

    token = DineOSTokenObtainPairSerializer.get_token(server)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    response = client.get("/v1/tables/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    ids = {t["id"] for t in results}
    assert str(legacy_table.id) not in ids


def test_table_list_still_includes_branch_less_tables_for_manager(restaurant, branch, manager_client):
    _, client = manager_client
    legacy_table = Table.objects.create(restaurant=restaurant, branch=None, table_number="Legacy2", capacity=4)

    response = client.get("/v1/tables/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    ids = {t["id"] for t in results}
    assert str(legacy_table.id) in ids
