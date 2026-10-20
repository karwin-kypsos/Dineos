"""Ingredient units (2026-10-20, per Karwin, Telegram msg 1239): the app sends
PIECES and PORTION for countable ingredients (eggs, buns) - both were 400
"is not a valid choice". Accepted now: KG, G, L, ML, PIECES, PORTION; the old
PCS still works and is stored as PIECES."""
import pytest

from apps.inventory.models import Ingredient

pytestmark = pytest.mark.django_db
ACCEPTED = ["KG", "G", "L", "ML", "PIECES", "PORTION"]


@pytest.mark.parametrize("unit", ACCEPTED)
def test_every_unit_is_accepted(admin_client, unit):
    _, client = admin_client

    resp = client.post("/v1/inventory/ingredients/", {"name": f"Item {unit}", "unit": unit}, format="json")

    assert resp.status_code == 201, resp.data
    assert resp.data["unit"] == unit


def test_pcs_still_works_and_reads_back_as_pieces(admin_client):
    _, client = admin_client

    resp = client.post("/v1/inventory/ingredients/", {"name": "Eggs", "unit": "PCS"}, format="json")

    assert resp.status_code == 201, resp.data
    assert resp.data["unit"] == "PIECES"
    assert Ingredient.objects.get(id=resp.data["id"]).unit == "PIECES"


def test_unit_can_be_changed_to_portion(admin_client):
    _, client = admin_client
    created = client.post("/v1/inventory/ingredients/", {"name": "Buns", "unit": "KG"}, format="json").data

    resp = client.patch(f"/v1/inventory/ingredients/{created['id']}/", {"unit": "PORTION"}, format="json")

    assert resp.status_code == 200 and resp.data["unit"] == "PORTION"


@pytest.mark.parametrize("unit", ["BOX", "pieces", "", "PIECE"])
def test_anything_else_is_refused_with_the_valid_choice_message(admin_client, unit):
    _, client = admin_client

    resp = client.post("/v1/inventory/ingredients/", {"name": "Odd", "unit": unit}, format="json")

    assert resp.status_code == 400 and "unit" in resp.data


def test_unit_is_still_required(admin_client):
    _, client = admin_client
    resp = client.post("/v1/inventory/ingredients/", {"name": "No unit"}, format="json")
    assert resp.status_code == 400 and resp.data["unit"] == ["This field is required."]
