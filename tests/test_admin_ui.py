import os
import sys
import tempfile
import json
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-admin-test-import.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"
SAMPLE_INSIGHT = {
    "data": {
        "record_type": "event",
        "event_type": "conversation_insight_result",
        "id": "evt_admin_001",
        "occurred_at": "2026-06-23T12:05:00Z",
        "payload": {
            "assistant_id": "assistant-admin-test",
            "conversation_id": "conv-admin-test",
            "from": {"phone_number": "+12485550199"},
            "summary": "Caller asked for service status and requested a callback.",
            "intent": "service status",
        },
    },
    "meta": {"attempt": 1},
}


def configure_tmp_db(tmp_path: Path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app.init_db()


def login(client: TestClient):
    return client.post("/admin/login", data={"secret": SECRET}, follow_redirects=False)


def seed_insight(client: TestClient) -> str:
    response = client.post("/telnyx/insights", json=SAMPLE_INSIGHT, headers={"x-webhook-secret": SECRET})
    assert response.status_code == 200
    return response.json()["id"]


def test_admin_login_page_and_cookie(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)

    page = client.get("/admin/login")
    assert page.status_code == 200
    assert "Miswitch" in page.text
    assert "insights console" in page.text

    bad = client.post("/admin/login", data={"secret": "wrong"})
    assert bad.status_code == 401
    assert "Invalid shared secret" in bad.text

    good = login(client)
    assert good.status_code == 303
    assert "admin_session" in good.headers.get("set-cookie", "")


def test_admin_dashboard_requires_login_and_shows_stats(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    seed_insight(client)

    unauth = client.get("/admin", follow_redirects=False)
    assert unauth.status_code == 303
    assert unauth.headers["location"].endswith("/admin/login")

    login(client)
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "Dashboard" in dashboard.text
    assert "Stored records" in dashboard.text
    assert "Recent insights" in dashboard.text
    assert "1" in dashboard.text

    stats = client.get("/admin/api/stats")
    assert stats.status_code == 200
    assert stats.json()["insight_count"] == 1


def test_admin_insight_list_and_detail(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    insight_id = seed_insight(client)
    login(client)

    list_page = client.get("/admin/insights")
    assert list_page.status_code == 200
    assert insight_id in list_page.text
    assert "assistant-admin-test" in list_page.text
    assert "Caller asked for service status" in list_page.text

    detail = client.get(f"/admin/insights/{insight_id}")
    assert detail.status_code == 200
    assert "Raw JSON" in detail.text
    assert "assistant-admin-test" in detail.text

    api_detail = client.get(f"/admin/api/insights/{insight_id}")
    assert api_detail.status_code == 200
    assert api_detail.json()["id"] == insight_id


def test_admin_assistant_init_tester(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    login(client)

    payload = '{"data":{"payload":{"telnyx_end_user_target":"248-555-0199","telnyx_agent_target":"+12485550100","telnyx_conversation_channel":"phone_call"}}}'
    response = client.post(
        "/admin/tools/assistant-init",
        data={"payload": payload, "assistant_id": "assistant-ui", "customer_id": "reefer", "environment": "test"},
    )

    assert response.status_code == 200
    assert "conversation_query" in response.text
    assert "assistant-ui" in response.text


def test_assistant_name_map_and_rollup_page(tmp_path, monkeypatch):
    configure_tmp_db(tmp_path)
    names_file = tmp_path / "assistant-names.json"
    names_file.write_text(
        '{"assistant-admin-test": "Admin Test Assistant"}',
        encoding="utf-8",
    )
    webhook_app.ASSISTANT_NAMES_PATH = names_file
    webhook_app._assistant_names_cache = {}
    webhook_app._assistant_names_cache_at = 0.0
    monkeypatch.setattr(webhook_app, "env_or_file", lambda name, default=None: default)
    client = TestClient(webhook_app.app)
    login(client)
    seed_insight(client)

    page = client.get("/admin/assistants")
    assert page.status_code == 200
    assert "Admin Test Assistant" in page.text
    assert "assistant-admin-test" in page.text
    assert webhook_app.assistant_name_for("assistant-admin-test") == "Admin Test Assistant"


def test_assistant_name_map_uses_telnyx_as_source_of_truth(tmp_path, monkeypatch):
    configure_tmp_db(tmp_path)
    names_file = tmp_path / "assistant-names.json"
    names_file.write_text(
        '{"assistant-live": "Local Override Should Not Win"}',
        encoding="utf-8",
    )
    webhook_app.ASSISTANT_NAMES_PATH = names_file
    webhook_app.ASSISTANT_NAMES_JSON = ""
    webhook_app._assistant_names_cache = {}
    webhook_app._assistant_names_cache_at = 0.0
    monkeypatch.setattr(webhook_app, "env_or_file", lambda name, default=None: "test-key" if name == "TELNYX_API_KEY" else default)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "assistant-live", "name": "Exact Telnyx Name"}]}).encode("utf-8")

    monkeypatch.setattr(webhook_app.urlrequest, "urlopen", lambda request, timeout=8: FakeResponse())

    assert webhook_app.assistant_name_for("assistant-live") == "Exact Telnyx Name"


def test_assistant_name_uses_payload_name_when_telnyx_lookup_unavailable(tmp_path, monkeypatch):
    configure_tmp_db(tmp_path)
    webhook_app.ASSISTANT_NAMES_PATH = tmp_path / "missing-assistant-names.json"
    webhook_app.ASSISTANT_NAMES_JSON = ""
    webhook_app._assistant_names_cache = {}
    webhook_app._assistant_names_cache_at = 0.0
    monkeypatch.setattr(webhook_app, "env_or_file", lambda name, default=None: default)
    record = {
        "id": "payload-name-record",
        "received_at": "2026-06-23T12:05:00Z",
        "payload": {
            "data": {
                "payload": {
                    "assistant_id": "assistant-payload-name",
                    "assistant_name": "Payload Telnyx Name",
                }
            }
        },
    }

    fields = webhook_app.extract_insight_fields(record)

    assert fields["assistant_name"] == "Payload Telnyx Name"


def test_admin_webhook_simulator_stores_insight(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    login(client)

    response = client.post("/admin/tools/webhook-simulator", data={"payload": __import__('json').dumps(SAMPLE_INSIGHT)})

    assert response.status_code == 200
    assert "Stored insight" in response.text
    assert client.get("/admin/api/stats").json()["insight_count"] == 1
