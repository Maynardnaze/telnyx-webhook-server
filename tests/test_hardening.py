import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-hardening-test.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app._login_failures.clear()
    webhook_app._assistant_names_cache = {}
    webhook_app._assistant_names_cache_at = 0.0
    webhook_app._assistant_names_failed_at = 0.0
    webhook_app.init_db()
    yield
    webhook_app._login_failures.clear()
    webhook_app._assistant_names_failed_at = 0.0


def test_admin_api_returns_401_json_when_unauthenticated():
    client = TestClient(webhook_app.app)
    for path in ("/admin/api/stats", "/admin/api/insights", "/admin/api/async-jobs", "/admin/api/assistant-names"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "Admin session required"


def test_admin_pages_still_redirect_to_login():
    client = TestClient(webhook_app.app)
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/login")


def test_login_rate_limit_locks_out_after_repeated_failures():
    client = TestClient(webhook_app.app)
    for _ in range(webhook_app.LOGIN_MAX_FAILURES):
        response = client.post("/admin/login", data={"secret": "wrong"})
        assert response.status_code == 401
    locked = client.post("/admin/login", data={"secret": "wrong"})
    assert locked.status_code == 429
    # Even the correct secret is rejected while locked out.
    still_locked = client.post("/admin/login", data={"secret": SECRET})
    assert still_locked.status_code == 429


def test_successful_login_clears_failure_count():
    client = TestClient(webhook_app.app)
    for _ in range(webhook_app.LOGIN_MAX_FAILURES - 1):
        client.post("/admin/login", data={"secret": "wrong"})
    good = client.post("/admin/login", data={"secret": SECRET}, follow_redirects=False)
    assert good.status_code == 303
    assert not webhook_app._login_failures


def test_admin_pages_send_security_headers():
    client = TestClient(webhook_app.app)
    response = client.get("/admin/login")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_assistant_name_lookup_failure_is_cached(monkeypatch):
    monkeypatch.setattr(webhook_app, "env_or_file", lambda name, default=None: "test-key" if name == "TELNYX_API_KEY" else default)
    calls = {"count": 0}

    def failing_urlopen(request, timeout=8):
        calls["count"] += 1
        raise URLError("connection refused")

    monkeypatch.setattr(webhook_app.urlrequest, "urlopen", failing_urlopen)

    assert webhook_app.load_telnyx_assistant_name_map() == {}
    assert webhook_app.load_telnyx_assistant_name_map() == {}
    assert webhook_app.load_telnyx_assistant_name_map() == {}
    assert calls["count"] == 1
    assert webhook_app._assistant_names_status["reason"] == "telnyx_request_error"

    # force=True bypasses the failure cache (used by the admin refresh button).
    webhook_app.load_telnyx_assistant_name_map(force=True)
    assert calls["count"] == 2


def test_assistant_name_lookup_success_resets_failure_cache(monkeypatch):
    monkeypatch.setattr(webhook_app, "env_or_file", lambda name, default=None: "test-key" if name == "TELNYX_API_KEY" else default)

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "assistant-x", "name": "Assistant X"}]}).encode("utf-8")

    webhook_app._assistant_names_failed_at = webhook_app.time.time()
    monkeypatch.setattr(webhook_app.urlrequest, "urlopen", lambda request, timeout=8: FakeResponse())

    assert webhook_app.load_telnyx_assistant_name_map(force=True) == {"assistant-x": "Assistant X"}
    assert webhook_app._assistant_names_failed_at == 0.0


def test_strict_phone_normalizer_used_by_sms_tools():
    assert webhook_app._normalize_phone("248-555-0100") == "+12485550100"
    assert webhook_app._normalize_phone("+12485550100") == "+12485550100"
    with pytest.raises(webhook_app.HTTPException):
        webhook_app._normalize_phone("not-a-number")
    with pytest.raises(webhook_app.HTTPException):
        webhook_app._normalize_phone("sip:caller@example.com")
