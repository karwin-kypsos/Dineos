from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.inventory.models import AIInsight, Ingredient, StockMovement

pytestmark = pytest.mark.django_db


@pytest.fixture
def critical_ingredient(restaurant):
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Chicken", unit="KG",
        current_stock=Decimal("1.00"), unit_cost=Decimal("200.00"), minimum_stock_level=Decimal("5.00"),
    )
    # Heavy recent usage so it's flagged both by stock_status and by
    # a fast-approaching stockout projection.
    StockMovement.objects.create(
        ingredient=ingredient, movement_type="USAGE", quantity=Decimal("14.00"),
        recorded_at=timezone.now() - timedelta(days=1),
    )
    return ingredient


@pytest.fixture
def healthy_ingredient(restaurant):
    return Ingredient.objects.create(
        restaurant=restaurant, name="Rice", unit="KG",
        current_stock=Decimal("100.00"), minimum_stock_level=Decimal("5.00"),
    )


def test_generate_returns_503_when_groq_unconfigured(manager_client, critical_ingredient):
    _, client = manager_client

    response = client.post("/v1/inventory/ai-insights/generate/")

    assert response.status_code == 503
    assert AIInsight.objects.count() == 0


def test_generate_creates_insight_for_flagged_ingredient_only(manager_client, critical_ingredient, healthy_ingredient, monkeypatch):
    _, client = manager_client

    def fake_generate_json(system_prompt, user_prompt, **kwargs):
        import json

        facts = json.loads(user_prompt)["ingredients"]
        assert len(facts) == 1
        assert facts[0]["ingredient_name"] == "Chicken"
        return {
            "insights": [
                {
                    "ingredient_id": facts[0]["ingredient_id"],
                    "severity": "CRITICAL",
                    "headline": "Chicken will run out within a day.",
                    "reason_breakdown": "Current stock 1.00 KG against a 14.00 KG/day usage rate.",
                    "recommended_action": "Order at least 20 KG today.",
                }
            ]
        }

    monkeypatch.setattr("core.ai_client.generate_json", fake_generate_json)

    response = client.post("/v1/inventory/ai-insights/generate/")

    assert response.status_code == 201, response.data
    assert len(response.data) == 1
    assert response.data[0]["severity"] == "CRITICAL"
    assert response.data[0]["ingredient_name"] == "Chicken"
    assert AIInsight.objects.filter(ingredient=critical_ingredient, severity="CRITICAL").exists()


def test_generate_returns_empty_list_when_nothing_flagged(manager_client, healthy_ingredient, monkeypatch):
    _, client = manager_client
    monkeypatch.setattr("core.ai_client.generate_json", lambda *a, **k: pytest.fail("should not call Groq"))

    response = client.post("/v1/inventory/ai-insights/generate/")

    assert response.status_code == 201
    assert response.data == []


def test_list_excludes_dismissed_by_default(manager_client, restaurant, critical_ingredient):
    _, client = manager_client
    AIInsight.objects.create(
        restaurant=restaurant, ingredient=critical_ingredient, severity="CRITICAL",
        headline="old", is_dismissed=True,
    )
    active = AIInsight.objects.create(
        restaurant=restaurant, ingredient=critical_ingredient, severity="ALERT", headline="active",
    )

    response = client.get("/v1/inventory/ai-insights/")

    assert response.status_code == 200
    results = response.data["results"] if "results" in response.data else response.data
    ids = {r["id"] for r in results}
    assert str(active.id) in ids
    assert len(ids) == 1


def test_dismiss_marks_insight(manager_client, restaurant, critical_ingredient):
    _, client = manager_client
    insight = AIInsight.objects.create(
        restaurant=restaurant, ingredient=critical_ingredient, severity="ALERT", headline="x",
    )

    response = client.patch(f"/v1/inventory/ai-insights/{insight.id}/dismiss/")

    assert response.status_code == 200
    insight.refresh_from_db()
    assert insight.is_dismissed is True
    assert insight.dismissed_at is not None


def test_server_cannot_access_ai_insights(server_client):
    _, client = server_client

    response = client.get("/v1/inventory/ai-insights/")

    assert response.status_code == 403
