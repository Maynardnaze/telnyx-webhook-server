import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-waiver-test.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"


def configure_tmp_paths(tmp_path: Path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app.init_db()


def test_send_waiver_sms_requires_secret(tmp_path):
    configure_tmp_paths(tmp_path)
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telnyx/tools/send-waiver-sms",
        json={"from": "+12485550101", "to": "+12485550100", "business": "urban_air"},
    )

    assert response.status_code == 401


def test_send_waiver_sms_sends_urban_air_template(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path)
    sent = {}

    def fake_send_telnyx_sms(*, from_number: str, to_number: str, text: str):
        sent.update({"from": from_number, "to": to_number, "text": text})
        return {"status_code": 200, "response": {"data": {"id": "msg_test_123"}}}

    monkeypatch.setattr(webhook_app, "send_telnyx_sms", fake_send_telnyx_sms)
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telnyx/tools/send-waiver-sms",
        json={"from": "+12485550101", "to": "248-555-0100", "business": "urban_air"},
        headers={"x-webhook-secret": SECRET},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["business"] == "urban_air"
    assert body["message_id"] == "msg_test_123"
    assert sent["to"] == "+12485550100"
    assert sent["from"] == "+12485550101"
    assert "https://store.unleashedbrands.com/urban-air/oxford-mi/waiver" in sent["text"]


def test_send_waiver_sms_rejects_unsupported_business(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path)
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telnyx/tools/send-waiver-sms",
        json={"from": "+12485550101", "to": "+12485550100", "business": "gspizzeria"},
        headers={"x-webhook-secret": SECRET},
    )

    assert response.status_code == 400
    assert "Unsupported waiver business" in response.text


def test_sagebrush_menu_sms_uses_fixed_sender_and_suppresses_duplicate(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path)
    sent = []

    def fake_send_telnyx_sms(*, from_number: str, to_number: str, text: str):
        sent.append({"from": from_number, "to": to_number, "text": text})
        return {"status_code": 200, "response": {"data": {"id": f"msg_test_{len(sent)}"}}}

    monkeypatch.setattr(webhook_app, "send_telnyx_sms", fake_send_telnyx_sms)
    monkeypatch.setattr(webhook_app, "SAGEBRUSH_SMS_FROM_NUMBER", "+12487495537")
    client = TestClient(webhook_app.app)
    payload = {
        "from": "telnyxportal@assistant-test.sip.telnyx.com",
        "to": "248-555-0100",
        "template": "catering_menu",
        "call_session_id": "call-session-1",
    }

    first = client.post(
        "/telnyx/tools/sagebrush/send-menu-sms",
        json=payload,
        headers={"x-webhook-secret": SECRET},
    )
    duplicate = client.post(
        "/telnyx/tools/sagebrush/send-menu-sms",
        json=payload,
        headers={"x-webhook-secret": SECRET},
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert first.json()["sent"] is True
    assert duplicate.json()["duplicate_suppressed"] is True
    assert duplicate.json()["message_id"] == "msg_test_1"
    assert len(sent) == 1
    assert sent[0]["from"].endswith("7495537")
    assert sent[0]["to"].endswith("5550100")
    assert "Sagebrush Cantina catering menu" in sent[0]["text"]


def test_sagebrush_menu_sms_accepts_telnyx_bearer(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path)
    webhook_app.TELNYX_API_KEY = "test-telnyx-api-key"

    def fake_send_telnyx_sms(*, from_number: str, to_number: str, text: str):
        return {"status_code": 200, "response": {"data": {"id": "msg_test_bearer"}}}

    monkeypatch.setattr(webhook_app, "send_telnyx_sms", fake_send_telnyx_sms)
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telnyx/tools/sagebrush/send-menu-sms",
        json={"from": "248-749-5537", "to": "248-555-0100", "template": "regular_menu", "call_session_id": "call-session-2"},
        headers={"Authorization": "Bearer test-telnyx-api-key"},
    )

    assert response.status_code == 200
    assert response.json()["message_id"] == "msg_test_bearer"
