import pytest
from django.db import IntegrityError, transaction

from apps.tables import services
from apps.tables.models import Table, TableSession

pytestmark = pytest.mark.django_db


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
