from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.orders.models import Order, OrderItem
from apps.tables.models import TableSession

pytestmark = pytest.mark.django_db


def _order_on(table, menu_item, quantity, when):
    # DINE_IN orders require a session (DB CheckConstraint), and a table
    # can only have one ACTIVE/BILL_REQUESTED session at a time — close
    # each one immediately so the next historical occurrence on the same
    # table doesn't collide with it.
    session = TableSession.objects.create(table=table, status=TableSession.Status.CLOSED)
    order = Order.objects.create(table=table, session=session, order_type="DINE_IN", status="SERVED")
    Order.objects.filter(id=order.id).update(placed_at=when)
    OrderItem.objects.create(order=order, menu_item=menu_item, quantity=quantity, unit_price=menu_item.price)
    return order


def _last_n_weekday_dates(target_date, weekday_offset_weeks):
    return target_date - timedelta(weeks=weekday_offset_weeks)


def test_forecast_returns_503_when_groq_unconfigured(manager_client, table, menu_item):
    _, client = manager_client
    today = timezone.localdate()
    _order_on(table, menu_item, 20, timezone.make_aware(timezone.datetime.combine(today - timedelta(weeks=1), timezone.datetime.min.time())))

    response = client.get("/v1/prepared-dishes/prep-forecast/")

    assert response.status_code == 503


def test_forecast_only_includes_dishes_with_history(manager_client, table, menu_item, monkeypatch):
    _, client = manager_client
    today = timezone.localdate()

    for weeks_ago in (1, 2, 3, 4):
        when = timezone.make_aware(timezone.datetime.combine(today - timedelta(weeks=weeks_ago), timezone.datetime.min.time()))
        _order_on(table, menu_item, 10 + weeks_ago, when)

    def fake_generate_json(system_prompt, user_prompt, **kwargs):
        import json

        dishes = json.loads(user_prompt)["dishes"]
        assert len(dishes) == 1
        assert dishes[0]["menu_item_name"] == "Chicken Biryani"
        assert dishes[0]["quantities_oldest_to_newest"] == [14, 13, 12, 11]
        return {
            "forecasts": [
                {
                    "menu_item_id": dishes[0]["menu_item_id"],
                    "headline": "Based on the last 4 occurrences, Chicken Biryani has been trending down.",
                    "reasoning": "Quantities dropped steadily from 14 to 11 over the last four weeks.",
                }
            ]
        }

    monkeypatch.setattr("core.ai_client.generate_json", fake_generate_json)

    response = client.get("/v1/prepared-dishes/prep-forecast/")

    assert response.status_code == 200, response.data
    assert response.data["weekday"] == today.strftime("%A")
    forecasts = response.data["forecasts"]
    assert len(forecasts) == 1
    assert forecasts[0]["menu_item_name"] == "Chicken Biryani"
    assert forecasts[0]["suggested_prep_quantity"] == 12  # avg(11,12,13,14) = 12.5 -> round to 12
    assert forecasts[0]["headline"]


def test_forecast_empty_when_no_history(manager_client, table, menu_item, monkeypatch):
    _, client = manager_client
    monkeypatch.setattr("core.ai_client.generate_json", lambda *a, **k: pytest.fail("should not call Groq"))

    response = client.get("/v1/prepared-dishes/prep-forecast/")

    assert response.status_code == 200
    assert response.data["forecasts"] == []


def test_forecast_respects_custom_date_and_lookback(manager_client, table, menu_item, monkeypatch):
    _, client = manager_client
    target_date = timezone.localdate() + timedelta(days=10)

    when = timezone.make_aware(timezone.datetime.combine(target_date - timedelta(weeks=1), timezone.datetime.min.time()))
    _order_on(table, menu_item, 5, when)

    def fake_generate_json(system_prompt, user_prompt, **kwargs):
        import json

        dishes = json.loads(user_prompt)["dishes"]
        assert len(dishes) == 1
        assert len(dishes[0]["quantities_oldest_to_newest"]) == 1
        return {"forecasts": [{"menu_item_id": dishes[0]["menu_item_id"], "headline": "x", "reasoning": "y"}]}

    monkeypatch.setattr("core.ai_client.generate_json", fake_generate_json)

    response = client.get(f"/v1/prepared-dishes/prep-forecast/?date={target_date.isoformat()}&lookback=1")

    assert response.status_code == 200, response.data
    assert response.data["target_date"] == target_date.isoformat()
    assert response.data["forecasts"][0]["suggested_prep_quantity"] == 5


def test_server_cannot_access_prep_forecast(server_client):
    _, client = server_client

    response = client.get("/v1/prepared-dishes/prep-forecast/")

    assert response.status_code == 403


def test_forecast_rejects_bad_date_format(manager_client):
    _, client = manager_client

    response = client.get("/v1/prepared-dishes/prep-forecast/?date=not-a-date")

    assert response.status_code == 400
