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
