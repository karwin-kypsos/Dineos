import pytest

from apps.billing import services as billing_services
from apps.feedback.models import Feedback
from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def _paid_bill(cashier_user, table, menu_item, quantity=1):
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": quantity}])
    return billing_services.pay_bill(session.id, "CASH", cashier_user)


def test_submit_feedback_no_auth(api_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item)

    response = api_client.post(
        "/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 5, "comment": "Great food!"}, format="json",
    )

    assert response.status_code == 201
    assert response.data["rating"] == 5
    assert response.data["comment"] == "Great food!"
    assert Feedback.objects.filter(bill=bill).count() == 1


def test_submit_feedback_resubmission_is_idempotent(api_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item)

    first = api_client.post(
        "/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 4, "comment": "Good"}, format="json",
    )
    assert first.status_code == 201

    second = api_client.post(
        "/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 2, "comment": "Changed my mind"}, format="json",
    )
    assert second.status_code == 200
    assert second.data["id"] == first.data["id"]
    assert second.data["rating"] == 4  # unchanged — the original submission wins, not a silent overwrite
    assert Feedback.objects.filter(bill=bill).count() == 1


def test_submit_feedback_rejects_rating_out_of_range(api_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item)

    response = api_client.post("/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 6}, format="json")
    assert response.status_code == 400

    response = api_client.post("/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 0}, format="json")
    assert response.status_code == 400


def test_submit_feedback_rejects_comment_over_140_chars(api_client, cashier_client, table, menu_item):
    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item)

    response = api_client.post(
        "/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 3, "comment": "x" * 141}, format="json",
    )
    assert response.status_code == 400


def test_submit_feedback_unknown_bill_returns_404(api_client):
    response = api_client.post(
        "/v1/feedback/submit/", {"bill_id": "00000000-0000-0000-0000-000000000000", "rating": 5}, format="json",
    )
    assert response.status_code == 404


def test_submit_feedback_for_takeaway_bill(api_client, cashier_client, branch, menu_item):
    user, client = cashier_client
    user.branch = branch
    user.save(update_fields=["branch"])
    order = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    ).data
    pay = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": order["id"], "payment_method": "CASH"}, format="json",
    )

    response = api_client.post(
        "/v1/feedback/submit/", {"bill_id": pay.data["id"], "rating": 5}, format="json",
    )
    assert response.status_code == 201


def test_list_feedback_staff_only_and_tenant_scoped(admin_client, cashier_client, table, menu_item, api_client):
    from apps.restaurant.models import Restaurant
    from apps.authentication.models import User
    from apps.billing import services as other_billing_services
    from apps.orders import services as other_order_services
    from apps.tables import services as other_table_services
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 5}, format="json")

    from apps.menu.models import Category, MenuItem

    other_restaurant = Restaurant.objects.create(name="Other Restaurant", slug="other-restaurant")
    other_table = Table.objects.create(restaurant=other_restaurant, table_number="1", capacity=4)
    other_cashier = User.objects.create_user(
        email="other-cashier@test.dineos", password="Test@1234", role="CASHIER", restaurant=other_restaurant,
    )
    other_category = Category.objects.create(restaurant=other_restaurant, name="Other Category", sort_order=1)
    other_menu_item = MenuItem.objects.create(category=other_category, name="Other Dish", price=100)
    other_session, _ = other_table_services.get_or_create_active_session(other_table.id)
    other_order_services.place_order(other_session.id, [{"menu_item_id": other_menu_item.id, "quantity": 1}])
    other_bill = other_billing_services.pay_bill(other_session.id, "CASH", other_cashier)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(other_bill.id), "rating": 1}, format="json")

    _, admin = admin_client
    response = admin.get("/v1/feedback/")

    assert response.status_code == 200
    ratings = {f["rating"] for f in response.data}
    assert 5 in ratings
    assert 1 not in ratings  # other restaurant's feedback must never leak in


def test_submit_feedback_response_includes_food_items(api_client, cashier_client, table, menu_item):
    """2026-09-02, per Karwin's report - a comment alone gave no way to
    tell which food item a customer meant."""
    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item, quantity=2)

    response = api_client.post(
        "/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 4, "comment": "Loved it"}, format="json",
    )

    assert response.status_code == 201
    assert len(response.data["items"]) == 1
    assert response.data["items"][0]["menu_item_name"] == menu_item.name
    assert response.data["items"][0]["quantity"] == 2


def test_list_feedback_includes_food_items(admin_client, cashier_client, table, menu_item, api_client):
    cashier_user, _ = cashier_client
    bill = _paid_bill(cashier_user, table, menu_item)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(bill.id), "rating": 5}, format="json")

    _, admin = admin_client
    response = admin.get("/v1/feedback/")

    assert response.status_code == 200
    assert response.data[0]["items"][0]["menu_item_name"] == menu_item.name


def test_list_feedback_defaults_to_managers_own_branch(restaurant, manager_client, cashier_client, branch, menu_item, api_client):
    from apps.restaurant.models import Branch
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])
    table_a = Table.objects.create(restaurant=restaurant, branch=branch, table_number="FBa")
    bill_a = _paid_bill(cashier_user, table_a, menu_item)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(bill_a.id), "rating": 5}, format="json")

    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    table_b = Table.objects.create(restaurant=restaurant, branch=other_branch, table_number="FBb")
    bill_b = _paid_bill(cashier_user, table_b, menu_item)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(bill_b.id), "rating": 1}, format="json")

    manager_user, manager = manager_client
    manager_user.branch = branch
    manager_user.save(update_fields=["branch"])

    response = manager.get("/v1/feedback/")

    assert response.status_code == 200
    ratings = {f["rating"] for f in response.data}
    assert ratings == {5}  # only the manager's own branch, not the other one


def test_list_feedback_filters_by_rating(admin_client, cashier_client, table, menu_item, api_client):
    from apps.tables.models import Table

    cashier_user, _ = cashier_client
    bill_5 = _paid_bill(cashier_user, table, menu_item)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(bill_5.id), "rating": 5}, format="json")

    other_table = Table.objects.create(restaurant=table.restaurant, branch=table.branch, table_number="Fb1")
    bill_2 = _paid_bill(cashier_user, other_table, menu_item)
    api_client.post("/v1/feedback/submit/", {"bill_id": str(bill_2.id), "rating": 2}, format="json")

    _, admin = admin_client
    response = admin.get("/v1/feedback/?rating=5")

    assert response.status_code == 200
    assert all(f["rating"] == 5 for f in response.data)
    assert len(response.data) == 1
