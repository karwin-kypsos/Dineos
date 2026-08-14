from decimal import Decimal

import pytest

from apps.inventory import services as inventory_services
from apps.inventory.models import Ingredient, StockMovement

pytestmark = pytest.mark.django_db


@pytest.fixture
def chicken(restaurant):
    return Ingredient.objects.create(
        restaurant=restaurant, name="Chicken", unit="KG", current_stock=Decimal("10.00"), minimum_stock_level=Decimal("5.00"),
    )


def test_eod_review_requires_admin_or_manager(server_client):
    _, client = server_client
    response = client.get("/v1/admin/eod-review/")
    assert response.status_code == 403


def test_eod_review_rejects_bad_date_format(admin_client):
    _, client = admin_client
    response = client.get("/v1/admin/eod-review/?date=not-a-date")
    assert response.status_code == 400


def test_eod_review_includes_wastage_and_revenue(admin_client, table, menu_item, chicken):
    _, client = admin_client
    from apps.orders import services as order_services
    from apps.billing import services as billing_services
    from apps.tables import services as table_services

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    billing_services.pay_bill(session.id, "CASH", None)

    inventory_services.record_wastage(chicken.id, Decimal("2.00"), "SPOILED", recorded_by=None)

    response = client.get("/v1/admin/eod-review/")

    assert response.status_code == 200
    assert response.data["tables_served"] == 1
    assert response.data["wastage_entries_count"] == 1
    assert Decimal(str(response.data["wastage_total_cost"])) >= Decimal("0")
    assert "bills" not in response.data or response.data.get("bills") is None


def test_eod_review_counts_restocks_and_staff(admin_client, manager_client, chicken):
    admin_user, client = admin_client
    manager_user, _ = manager_client

    inventory_services.add_stock(chicken.id, Decimal("5.00"), recorded_by=manager_user)

    response = client.get("/v1/admin/eod-review/")

    assert response.status_code == 200
    assert response.data["restock_entries_count"] == 1


def test_low_stock_alerts_flags_critical_ingredient(manager_client, chicken):
    _, client = manager_client

    # 2kg/day average usage over the week, only 1kg left -> < 1 day remaining
    for _ in range(7):
        StockMovement.objects.create(
            ingredient=chicken, movement_type="USAGE", quantity=Decimal("2.00"),
        )
    chicken.current_stock = Decimal("1.00")
    chicken.save(update_fields=["current_stock"])

    response = client.get("/v1/admin/ai/low-stock-alerts/")

    assert response.status_code == 200
    alert = next(a for a in response.data if a["ingredient_id"] == str(chicken.id))
    assert alert["severity"] == "CRITICAL"
    assert alert["days_remaining"] < 1


def test_low_stock_alerts_omits_healthy_ingredients(manager_client, restaurant):
    _, client = manager_client
    Ingredient.objects.create(
        restaurant=restaurant, name="Rice", unit="KG", current_stock=Decimal("100.00"), minimum_stock_level=Decimal("5.00"),
    )

    response = client.get("/v1/admin/ai/low-stock-alerts/")

    assert response.status_code == 200
    names = {a["name"] for a in response.data}
    assert "Rice" not in names


def test_low_stock_alerts_isolated_across_restaurants(manager_client):
    _, client = manager_client

    from apps.restaurant.models import Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign AI", slug="foreign-ai")
    Ingredient.objects.create(
        restaurant=foreign_restaurant, name="Foreign Critical", unit="KG",
        current_stock=Decimal("0.00"), minimum_stock_level=Decimal("50.00"),
    )

    response = client.get("/v1/admin/ai/low-stock-alerts/")

    assert response.status_code == 200
    names = {a["name"] for a in response.data}
    assert "Foreign Critical" not in names


def test_ai_eod_report_returns_503_when_groq_unconfigured(admin_client, table, menu_item):
    _, client = admin_client
    from apps.orders import services as order_services
    from apps.billing import services as billing_services
    from apps.tables import services as table_services

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    billing_services.pay_bill(session.id, "CASH", None)

    response = client.get("/v1/admin/ai/eod-report/")

    assert response.status_code == 503


def test_ai_eod_report_combines_today_data_with_ai_summary(admin_client, table, menu_item, chicken, monkeypatch):
    _, client = admin_client
    from apps.orders import services as order_services
    from apps.billing import services as billing_services
    from apps.tables import services as table_services

    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    billing_services.pay_bill(session.id, "CASH", None)
    chicken.current_stock = Decimal("1.00")
    chicken.save(update_fields=["current_stock"])

    captured = {}

    def fake_generate_json(system_prompt, user_prompt, **kwargs):
        import json

        facts = json.loads(user_prompt)
        captured["facts"] = facts
        return {
            "summary": "Today brought in solid revenue with one table served.",
            "recommendations": [
                {"headline": "Restock Chicken before tomorrow.", "reasoning": "Only 1.00 KG left, below the 5.00 KG minimum."}
            ],
        }

    monkeypatch.setattr("core.ai_client.generate_json", fake_generate_json)

    response = client.get("/v1/admin/ai/eod-report/")

    assert response.status_code == 200, response.data
    assert response.data["tables_served"] == 1
    assert response.data["ai_summary"] == "Today brought in solid revenue with one table served."
    assert len(response.data["recommendations_for_tomorrow"]) == 1
    assert "Chicken" in response.data["recommendations_for_tomorrow"][0]["headline"]

    # Facts sent to Groq are grounded in the real low-stock ingredient.
    low_stock_names = [i["name"] for i in captured["facts"]["low_or_critical_stock_ingredients"]]
    assert "Chicken" in low_stock_names


def test_ai_eod_report_rejects_bad_date_format(admin_client):
    _, client = admin_client

    response = client.get("/v1/admin/ai/eod-report/?date=not-a-date")

    assert response.status_code == 400


def test_ai_eod_report_requires_admin_or_manager(server_client):
    _, client = server_client

    response = client.get("/v1/admin/ai/eod-report/")

    assert response.status_code == 403
