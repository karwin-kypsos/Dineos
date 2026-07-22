import pytest

pytestmark = pytest.mark.django_db


def test_full_customer_journey(api_client, kds_client, server_client, cashier_client, table, menu_item):
    kds_device, kitchen_client = kds_client
    server_user, server = server_client
    cashier_user, cashier = cashier_client

    # 1. Customer scans the QR code — no active session yet.
    qr_response = api_client.get(f"/v1/tables/qr/{table.table_number}/")
    assert qr_response.status_code == 200
    assert qr_response.data["active_session"] is None

    # 2. Start a session (idempotent — calling twice returns the same session).
    start_1 = api_client.post(f"/v1/tables/{table.id}/session/")
    assert start_1.status_code == 201
    session_id = start_1.data["id"]

    start_2 = api_client.post(f"/v1/tables/{table.id}/session/")
    assert start_2.status_code == 200
    assert start_2.data["id"] == session_id

    # 3. Round 1 — order 2 Chicken Biryani.
    round_1 = api_client.post(
        "/v1/orders/",
        {"session_id": session_id, "items": [{"menu_item": menu_item.id, "quantity": 2}]},
        format="json",
    )
    assert round_1.status_code == 201
    assert round_1.data["round_number"] == 1
    order_1_id = round_1.data["id"]

    menu_item.refresh_from_db()
    portion = menu_item.prepared_portions.get()
    assert portion.portions_remaining == 18

    # 4. Round 2 — order 1 more.
    round_2 = api_client.post(
        "/v1/orders/",
        {"session_id": session_id, "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert round_2.status_code == 201
    assert round_2.data["round_number"] == 2
    order_2_id = round_2.data["id"]

    portion.refresh_from_db()
    assert portion.portions_remaining == 17

    # 5. Kitchen drives both orders through NEW -> ACCEPTED -> PREPARING -> READY.
    for order_id in (order_1_id, order_2_id):
        for target in ("accepted", "preparing", "ready"):
            resp = kitchen_client.patch(f"/v1/orders/{order_id}/status/", {"status": target}, format="json")
            assert resp.status_code == 200, resp.data

    # 6. Server collects and serves both.
    for order_id in (order_1_id, order_2_id):
        assert server.patch(f"/v1/orders/{order_id}/collected/").status_code == 200
        assert server.patch(f"/v1/orders/{order_id}/served/").status_code == 200

    # 7. Cashier reviews the bill (both rounds combined) and confirms payment.
    preview = cashier.get(f"/v1/bills/session/{session_id}/")
    assert preview.status_code == 200
    assert float(preview.data["subtotal"]) == 3 * 220.00

    pay = cashier.post("/v1/bills/payment/", {"session_id": session_id, "payment_method": "CASH"}, format="json")
    assert pay.status_code == 201
    total = pay.data["total_amount"]
    bill_id = pay.data["id"]

    table.refresh_from_db()
    assert table.status == "AVAILABLE"

    # 8. Paying again for the same session is idempotent — same bill, not a duplicate.
    pay_again = cashier.post("/v1/bills/payment/", {"session_id": session_id, "payment_method": "CASH"}, format="json")
    assert pay_again.status_code == 201
    assert pay_again.data["id"] == bill_id
    assert pay_again.data["total_amount"] == total
