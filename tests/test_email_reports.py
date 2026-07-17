import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-email-report-test.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"
SUMMARY_ID = "cfcc865c-d3d4-4823-8a4b-f0df57d9f56f"
RESOLUTION_ID = "e0398bdc-55c1-4a32-a430-1bd3b625afb2"
CATEGORY_ID = "b5182c7c-1ec3-46ed-bb6e-e43c33d2fbb0"


def configure_tmp_db(tmp_path: Path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.ALLOW_LOCAL_ASSISTANT_NAME_FALLBACKS = True
    webhook_app.ASSISTANT_NAMES_JSON = json.dumps({
        "assistant-a": "Sagebrush Catering",
        "assistant-b": "Legacy 925 Sam",
        "assistant-c": "Unselected Assistant",
    })
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app._assistant_names_cache = {}
    webhook_app._assistant_names_cache_at = 0.0
    webhook_app.init_db()


def login(client: TestClient):
    response = client.post("/admin/login", data={"secret": SECRET}, follow_redirects=False)
    assert response.status_code == 303


def insight_payload(assistant_id: str, conversation_id: str, summary: str, *, called_tools=None, caller="+12485550101"):
    return {
        "data": {
            "event_type": "conversation_insight_result",
            "payload": {
                "assistant_id": assistant_id,
                "conversation_id": conversation_id,
                "metadata": {
                    "assistant_id": assistant_id,
                    "telnyx_conversation_channel": "phone_call",
                    "telnyx_end_user_target": caller,
                    "called_tools": called_tools or [],
                },
                "results": [
                    {"insight_id": SUMMARY_ID, "result": summary},
                    {"insight_id": RESOLUTION_ID, "result": json.dumps({"resolution_status": "unresolved"})},
                    {"insight_id": CATEGORY_ID, "result": json.dumps({"primary_category": "Catering Lead"})},
                ],
            },
        }
    }


def seed(client: TestClient, payload: dict) -> str:
    response = client.post("/telnyx/insights", json=payload, headers={"x-webhook-secret": SECRET})
    assert response.status_code == 200
    return response.json()["id"]


def test_email_report_page_lists_all_assistants_with_multi_select(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    login(client)
    seed(client, insight_payload("assistant-a", "conv-a", "A catering lead wants a callback."))
    seed(client, insight_payload("assistant-b", "conv-b", "Legacy caller asked for the operator."))

    page = client.get("/admin/reports/email")

    assert page.status_code == 200
    assert "Email Reports" in page.text
    assert "Sagebrush Catering" in page.text
    assert "Legacy 925 Sam" in page.text
    assert 'type="checkbox"' in page.text
    assert 'name="assistant_ids"' in page.text
    assert "Preview report" in page.text
    assert "Send email" in page.text


def test_email_report_preview_filters_multiple_assistants_and_flags_sms(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    login(client)
    seed(client, insight_payload("assistant-a", "conv-a", "A catering lead wants a callback.", called_tools=["send_catering_menu_sms", "send_catering_menu_sms"]))
    seed(client, insight_payload("assistant-b", "conv-b", "Legacy caller asked for the operator."))
    seed(client, insight_payload("assistant-c", "conv-c", "This should not appear."))

    response = client.post(
        "/admin/api/email-report/preview",
        json={"assistant_ids": ["assistant-a", "assistant-b"], "hours": 24, "recipient": "ops@example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["duplicate_sms_flags"] == 1
    assert "A catering lead wants a callback" in body["markdown"]
    assert "Legacy caller asked for the operator" in body["markdown"]
    assert "This should not appear" not in body["markdown"]
    assert body["subject"].startswith("AI Insights Report")


def test_email_report_send_uses_smtp_env_and_selected_assistants(tmp_path, monkeypatch):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    login(client)
    seed(client, insight_payload("assistant-a", "conv-a", "A catering lead wants a callback."))

    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def ehlo(self):
            return None
        def starttls(self, context=None):
            return None
        def login(self, username, password):
            assert username == "support@getmyswitch.com"
            assert password == "smtp-secret"
        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setenv("SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "support@getmyswitch.com")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("SMTP_FROM", "support@getmyswitch.com")
    monkeypatch.setattr(webhook_app.smtplib, "SMTP", FakeSMTP)

    response = client.post(
        "/admin/api/email-report/send",
        json={"assistant_ids": ["assistant-a"], "hours": 24, "recipient": "amaynard@gmx.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["recipient"] == "amaynard@gmx.com"
    assert len(sent_messages) == 1
    assert sent_messages[0]["From"] == "support@getmyswitch.com"
    assert sent_messages[0]["To"] == "amaynard@gmx.com"
    rendered = sent_messages[0].as_string()
    assert "A catering lead wants a callback" in rendered


def test_revenue_model_env_overrides_defaults(tmp_path):
    configure_tmp_db(tmp_path)
    webhook_app.REVENUE_MODEL_JSON = json.dumps(
        {
            "default_job_value": "1200.50",
            "default_lead_probability": "1.5",
            "missed_without_ai_probability": "0.4",
            "monthly_assistant_cost": "250",
            "category_values": {"Catering Lead": "2500"},
            "category_probabilities": {"Catering Lead": "0.35"},
            "assistant_overrides": {"assistant-a": {"default_job_value": 900}},
        }
    )
    webhook_app.REVENUE_MODEL_PATH = tmp_path / "missing-revenue-model.json"

    model = webhook_app.load_revenue_model()

    assert model["default_job_value"] == 1200.50
    assert model["default_lead_probability"] == 1.0
    assert model["missed_without_ai_probability"] == 0.4
    assert model["monthly_assistant_cost"] == 250.0
    assert model["category_values"] == {"Catering Lead": "2500"}
    assert model["category_probabilities"] == {"Catering Lead": "0.35"}
    assert model["assistant_overrides"] == {"assistant-a": {"default_job_value": 900}}


def test_revenue_model_invalid_values_fall_back_safely(tmp_path):
    configure_tmp_db(tmp_path)
    webhook_app.REVENUE_MODEL_JSON = json.dumps(
        {
            "default_job_value": "not-a-number",
            "default_lead_probability": -1,
            "missed_without_ai_probability": "bad",
            "monthly_assistant_cost": -50,
            "category_values": [],
            "category_probabilities": "bad",
            "assistant_overrides": "bad",
        }
    )
    webhook_app.REVENUE_MODEL_PATH = tmp_path / "missing-revenue-model.json"

    model = webhook_app.load_revenue_model()

    assert model["default_job_value"] == 500.0
    assert model["default_lead_probability"] == 0.25
    assert model["missed_without_ai_probability"] == 0.60
    assert model["monthly_assistant_cost"] == 95.0
    assert model["category_values"] == {}
    assert model["category_probabilities"] == {}
    assert model["assistant_overrides"] == {}


def test_money_formats_whole_dollar_estimates():
    assert webhook_app.money(1250.4) == "$1,250"
    assert webhook_app.money(0) == "$0"


def test_assistant_cost_for_window_prorates_monthly_cost():
    model = {"monthly_assistant_cost": 95.0}

    assert webhook_app.assistant_cost_for_window(model, hours=730, assistant_count=1) == 95.0
    assert webhook_app.assistant_cost_for_window(model, hours=24, assistant_count=2) == 95.0 * 2 * (24 / 730.0)
    assert webhook_app.assistant_cost_for_window({"monthly_assistant_cost": "bad"}, hours=24, assistant_count=1) > 0
