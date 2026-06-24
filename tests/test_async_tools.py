import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-async-test-import.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"


def configure_tmp_db(tmp_path: Path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app.init_db()


def login(client: TestClient):
    return client.post("/admin/login", data={"secret": SECRET}, follow_redirects=False)


def test_async_tool_requires_secret_and_call_control_id(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)

    no_secret = client.post(
        "/telnyx/tools/async/order-status",
        json={"order_id": "TEST-1"},
        headers={"x-telnyx-call-control-id": "cc-test"},
    )
    assert no_secret.status_code == 401

    no_call_control = client.post(
        "/telnyx/tools/async/order-status",
        json={"order_id": "TEST-1"},
        headers={"x-webhook-secret": SECRET},
    )
    assert no_call_control.status_code == 400


def test_async_tool_ack_creates_completed_dry_run_job(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telnyx/tools/async/order-status",
        json={"order_id": "TEST-42", "customer": {"phone": "+12485550199"}},
        headers={"x-webhook-secret": SECRET, "x-telnyx-call-control-id": "cc-test-42"},
    )

    assert response.status_code == 200
    ack = response.json()
    assert ack["ok"] is True
    assert ack["mode"] == "async_ack_dry_run"
    assert ack["job_id"]
    assert ack["ack_ms"] < 250

    job = webhook_app.get_async_tool_job(ack["job_id"])
    assert job is not None
    assert job["status"] == "complete"
    assert job["result_data"]["order_id"] == "TEST-42"
    assert job["result_data"]["caller"] == "+12***99"
    assert job["add_messages_dry_run"]["call_control_id"] == "cc-test-42"
    assert job["add_messages_dry_run"]["messages"][0]["role"] == "system"
    assert "TEST-42" in job["add_messages_dry_run"]["messages"][0]["content"]


def test_admin_async_jobs_page_and_api(tmp_path):
    configure_tmp_db(tmp_path)
    client = TestClient(webhook_app.app)
    client.post(
        "/telnyx/tools/async/order-status",
        json={"order_id": "TEST-UI"},
        headers={"x-webhook-secret": SECRET, "x-telnyx-call-control-id": "cc-ui"},
    )

    unauth = client.get("/admin/tools/async-jobs", follow_redirects=False)
    assert unauth.status_code == 303

    login(client)
    page = client.get("/admin/tools/async-jobs")
    assert page.status_code == 200
    assert "Async Tool Jobs" in page.text
    assert "order-status" in page.text
    assert "cc-ui" in page.text

    api = client.get("/admin/api/async-jobs")
    assert api.status_code == 200
    assert api.json()["count"] == 1
    assert api.json()["jobs"][0]["tool_name"] == "order-status"
