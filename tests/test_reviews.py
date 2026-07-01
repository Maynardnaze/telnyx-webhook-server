import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("WEBHOOK_DB_PATH", str(Path(tempfile.gettempdir()) / "telnyx-webhook-reviews-test.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webhook_app


SECRET = "unit-test-secret"
SAMPLE_INSIGHT = {
    "data": {
        "event_type": "conversation_insight_result",
        "payload": {
            "assistant_id": "assistant-review-test",
            "conversation_id": "conv-review-test",
            "from": {"phone_number": "+12485550199"},
            "summary": "Caller asked about party booking pricing.",
        },
    },
}


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    webhook_app.WEBHOOK_SECRET = SECRET
    webhook_app.ALLOW_NO_SECRET = False
    webhook_app.DB_PATH = tmp_path / "webhook.db"
    webhook_app._LEGACY_INSIGHTS_PATH = tmp_path / "insights.json"
    webhook_app._login_failures.clear()
    webhook_app.init_db()


def make_client() -> TestClient:
    client = TestClient(webhook_app.app)
    client.post("/admin/login", data={"secret": SECRET}, follow_redirects=False)
    return client


def seed_insight(client: TestClient) -> str:
    response = client.post("/telnyx/insights", json=SAMPLE_INSIGHT, headers={"x-webhook-secret": SECRET})
    assert response.status_code == 200
    return response.json()["id"]


def test_reviews_api_requires_admin_session():
    client = TestClient(webhook_app.app)
    assert client.get("/admin/api/reviews").status_code == 401
    assert client.get("/admin/api/reviews/some-id").status_code == 401
    assert client.put("/admin/api/reviews/some-id", json={"status": "reviewed"}).status_code == 401
    assert client.delete("/admin/api/reviews/some-id").status_code == 401


def test_review_upsert_get_and_list_roundtrip():
    client = make_client()
    insight_id = seed_insight(client)

    empty = client.get(f"/admin/api/reviews/{insight_id}")
    assert empty.status_code == 200
    assert empty.json() == webhook_app.default_review(insight_id)

    saved = client.put(
        f"/admin/api/reviews/{insight_id}",
        json={"status": "follow-up", "labels": ["vip", "vip", " low-confidence "], "note": "Call back Monday."},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["status"] == "follow-up"
    assert body["labels"] == ["vip", "low-confidence"]
    assert body["note"] == "Call back Monday."
    assert body["updated_at"]

    fetched = client.get(f"/admin/api/reviews/{insight_id}").json()
    assert fetched == body

    listed = client.get("/admin/api/reviews").json()
    assert listed["count"] == 1
    assert listed["reviews"][insight_id]["status"] == "follow-up"


def test_review_validation_and_missing_insight():
    client = make_client()
    insight_id = seed_insight(client)

    assert client.put(f"/admin/api/reviews/{insight_id}", json={"status": "bogus"}).status_code == 400
    assert client.put(f"/admin/api/reviews/{insight_id}", json={"labels": "not-a-list"}).status_code == 400
    assert client.put("/admin/api/reviews/does-not-exist", json={"status": "reviewed"}).status_code == 404
    assert client.get("/admin/api/reviews/does-not-exist").status_code == 404


def test_review_reset_to_new_deletes_row():
    client = make_client()
    insight_id = seed_insight(client)

    client.put(f"/admin/api/reviews/{insight_id}", json={"status": "reviewed"})
    assert client.get("/admin/api/reviews").json()["count"] == 1

    reset = client.put(f"/admin/api/reviews/{insight_id}", json={"status": "new", "labels": [], "note": ""})
    assert reset.status_code == 200
    assert reset.json()["updated_at"] is None
    assert client.get("/admin/api/reviews").json()["count"] == 0

    client.put(f"/admin/api/reviews/{insight_id}", json={"status": "ignored"})
    deleted = client.delete(f"/admin/api/reviews/{insight_id}")
    assert deleted.status_code == 200
    assert client.get("/admin/api/reviews").json()["count"] == 0


def test_review_status_shows_in_summaries_and_pages():
    client = make_client()
    insight_id = seed_insight(client)
    client.put(f"/admin/api/reviews/{insight_id}", json={"status": "follow-up", "labels": ["vip"]})

    summaries = client.get("/admin/api/insights").json()["insights"]
    assert summaries[0]["review_status"] == "follow-up"
    assert summaries[0]["review_labels"] == ["vip"]

    list_page = client.get("/admin/insights")
    assert "review-follow-up" in list_page.text

    detail_page = client.get(f"/admin/insights/{insight_id}")
    assert 'data-review-status="follow-up"' in detail_page.text


def test_needs_review_kpi_drops_after_triage():
    client = make_client()
    insight_id = seed_insight(client)

    # No resolution status parsed -> resolution_key "unresolved" -> flagged.
    assert client.get("/admin/api/stats").json()["needs_review_count"] == 1

    client.put(f"/admin/api/reviews/{insight_id}", json={"status": "reviewed"})
    stats = client.get("/admin/api/stats").json()
    assert stats["needs_review_count"] == 0
    assert stats["review_counts"] == {"reviewed": 1}

    client.put(f"/admin/api/reviews/{insight_id}", json={"status": "follow-up"})
    assert client.get("/admin/api/stats").json()["needs_review_count"] == 1


def test_reviews_pruned_with_insights():
    client = make_client()
    insight_id = seed_insight(client)
    client.put(f"/admin/api/reviews/{insight_id}", json={"status": "reviewed"})

    # Fill the store to the 500-record cap so the reviewed insight gets pruned.
    with webhook_app._db_lock:
        conn = webhook_app._db_connect()
        try:
            conn.executemany(
                "INSERT INTO insights (id, received_at, data) VALUES (?, ?, ?)",
                [(f"filler-{i}", f"2099-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00", "{}") for i in range(500)],
            )
            conn.commit()
        finally:
            conn.close()
    webhook_app.append_insight({"id": "one-more", "received_at": "2099-12-31T00:00:00+00:00"})

    assert webhook_app.get_insight_by_id(insight_id) is None
    assert webhook_app.list_insight_reviews() == {}
