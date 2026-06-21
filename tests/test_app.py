import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-test-import.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"
SAMPLE_INSIGHT = {
    "data": {
        "record_type": "event",
        "event_type": "ai_assistant.post_call_insight",
        "id": "evt_ai_001",
        "occurred_at": "2026-06-18T12:05:00Z",
        "payload": {
            "call_control_id": "cc_789",
            "call_session_id": "cs_789",
            "conversation_id": "conv_123",
            "assistant_id": "assistant_123",
            "from": {"phone_number": "+12485550100"},
            "to": {"phone_number": "+12485550101"},
            "summary": "Caller asked to book a private dining room next Friday and requested pricing details.",
            "sentiment": "positive",
            "intent": "reservation booking",
        },
    },
    "meta": {"attempt": 1},
}


def configure_tmp_paths(tmp_path: Path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app.init_db()


def test_telnyx_insights_stores_sqlite_record(tmp_path):
    configure_tmp_paths(tmp_path)
    client = TestClient(webhook_app.app)

    response = client.post("/telnyx/insights", json=SAMPLE_INSIGHT, headers={"x-webhook-secret": SECRET})

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["id"]

    listed = client.get("/telnyx/insights", headers={"x-webhook-secret": SECRET})
    assert listed.status_code == 200
    records = listed.json()["insights"]
    assert len(records) == 1
    assert records[0]["id"] == body["id"]
    assert records[0]["payload"]["data"]["event_type"] == "ai_assistant.post_call_insight"


def test_auth_required_for_insights(tmp_path):
    configure_tmp_paths(tmp_path)
    client = TestClient(webhook_app.app)

    response = client.post("/telnyx/insights", json=SAMPLE_INSIGHT)

    assert response.status_code == 401


def test_assistant_init_returns_scoped_memory_and_metadata(tmp_path):
    configure_tmp_paths(tmp_path)
    client = TestClient(webhook_app.app)
    payload = {
        "data": {
            "record_type": "event",
            "id": "evt_init_001",
            "event_type": "assistant.initialization",
            "occurred_at": "2026-06-21T12:00:00Z",
            "payload": {
                "telnyx_conversation_channel": "phone_call",
                "telnyx_agent_target": "+12485550100",
                "telnyx_end_user_target": "248-555-0199",
                "telnyx_end_user_target_verified": True,
            },
        }
    }

    response = client.post(
        "/telnyx/assistant/init?assistant_id=assistant-test&customer_id=acme&environment=test",
        json=payload,
        headers={"x-webhook-secret": SECRET},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dynamic_variables"]["caller_phone"] == "+12485550199"
    assert body["dynamic_variables"]["caller_verified"] == "true"
    assert body["conversation"]["metadata"]["customer_id"] == "acme"
    assert body["conversation"]["metadata"]["environment"] == "test"
    query = body["memory"]["conversation_query"]
    assert "metadata->telnyx_end_user_target=eq.%2B12485550199" in query
    assert "metadata->customer_id=eq.acme" in query
    assert "limit=5" in query


def test_assistant_init_without_caller_omits_memory(tmp_path):
    configure_tmp_paths(tmp_path)
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telnyx/assistant/init?assistant_id=assistant-test&customer_id=acme",
        json={"data": {"payload": {"telnyx_conversation_channel": "sms_chat"}}},
        headers={"x-webhook-secret": SECRET},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dynamic_variables"]["memory_enabled"] == "false"
    assert "memory" not in body
