import pytest

from apps.dashboard.models import ChatMessage

pytestmark = pytest.mark.django_db


def test_send_message_returns_503_when_groq_unconfigured(manager_client):
    _, client = manager_client

    response = client.post("/v1/admin/ai/chat/messages/", {"content": "What should I restock?"}, format="json")

    assert response.status_code == 503
    # The user's message is still preserved even though no reply came back.
    assert ChatMessage.objects.filter(role="USER", content="What should I restock?").exists()


def test_send_message_persists_both_sides_and_calls_groq_with_context(manager_client, restaurant, monkeypatch):
    from apps.inventory.models import Ingredient

    _, client = manager_client
    Ingredient.objects.create(restaurant=restaurant, name="Chicken", unit="KG", current_stock="0.00", minimum_stock_level="5.00")

    captured = {}

    def fake_generate_reply(system_prompt, messages, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["messages"] = messages
        return "You're out of Chicken — restock at least 5 KG."

    monkeypatch.setattr("core.ai_client.generate_reply", fake_generate_reply)

    response = client.post("/v1/admin/ai/chat/messages/", {"content": "What should I restock?"}, format="json")

    assert response.status_code == 201, response.data
    assert response.data["message"]["role"] == "USER"
    assert response.data["message"]["content"] == "What should I restock?"
    assert response.data["reply"]["role"] == "ASSISTANT"
    assert response.data["reply"]["content"] == "You're out of Chicken — restock at least 5 KG."

    # Context snapshot (system message) includes the real low-stock ingredient.
    context_message = captured["messages"][0]
    assert context_message["role"] == "system"
    assert "Chicken" in context_message["content"]

    # The user's own message is included in the conversation history sent to Groq.
    assert any(m["role"] == "user" and m["content"] == "What should I restock?" for m in captured["messages"])

    assert ChatMessage.objects.filter(role="USER").count() == 1
    assert ChatMessage.objects.filter(role="ASSISTANT").count() == 1


def test_send_message_rejects_empty_content(manager_client):
    _, client = manager_client

    response = client.post("/v1/admin/ai/chat/messages/", {"content": "   "}, format="json")

    assert response.status_code == 400


def test_list_messages_returns_own_history_only(manager_client, restaurant, admin_client):
    manager, manager_c = manager_client
    admin, admin_c = admin_client

    ChatMessage.objects.create(restaurant=restaurant, user=manager, role="USER", content="manager's question")
    ChatMessage.objects.create(restaurant=restaurant, user=admin, role="USER", content="admin's question")

    response = manager_c.get("/v1/admin/ai/chat/messages/")

    assert response.status_code == 200
    contents = [m["content"] for m in response.data]
    assert contents == ["manager's question"]


def test_server_cannot_access_ai_chat(server_client):
    _, client = server_client

    response = client.get("/v1/admin/ai/chat/messages/")

    assert response.status_code == 403
