from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

try:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
except ImportError:  # pragma: no cover - only happens if optional dependency is missing locally
    BadSignatureError = Exception
    VerifyKey = None

APP_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("WEBHOOK_DB_PATH", "/data/webhook.db"))
# Legacy JSON path used only for one-time migration into SQLite.
_LEGACY_INSIGHTS_PATH = Path(os.environ.get("WEBHOOK_INSIGHTS_PATH", "/data/insights.json"))


def env_or_file(name: str, default: str | None = None) -> str | None:
    file_value = os.environ.get(f"{name}_FILE")
    if file_value:
        try:
            return Path(file_value).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return default
    return os.environ.get(name, default)


WEBHOOK_SECRET = env_or_file("WEBHOOK_SECRET") or env_or_file("DIRECTORY_WEBHOOK_SECRET") or "change-me-local-test"
TELNYX_PUBLIC_KEY = env_or_file("TELNYX_PUBLIC_KEY")
ALLOW_NO_SECRET = os.environ.get("WEBHOOK_ALLOW_NO_SECRET") == "1"

app = FastAPI(
    title="Miswitch Telnyx Webhook Server",
    version="0.2.0",
    description="Public webhook receiver for Telnyx behind webhook.miswitch.cloud.",
)

_db_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_secret(x_webhook_secret: str | None, query_secret: str | None = None) -> None:
    if ALLOW_NO_SECRET:
        return
    supplied = x_webhook_secret or query_secret
    if supplied != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Missing or invalid webhook secret")


def _configured_public_key() -> str | None:
    if not TELNYX_PUBLIC_KEY:
        return None
    public_key = TELNYX_PUBLIC_KEY.strip()
    if not public_key or public_key == "your_public_key_here" or public_key.lower().startswith("your_"):
        return None
    return public_key


def _decode_telnyx_public_key(public_key: str) -> bytes:
    if "BEGIN PUBLIC KEY" in public_key:
        public_key = "".join(line.strip() for line in public_key.splitlines() if "BEGIN" not in line and "END" not in line)
    return base64.b64decode(public_key, validate=True)


def verify_telnyx_signature(signature: str | None, timestamp: str | None, raw_body: bytes) -> bool:
    public_key = _configured_public_key()
    if not public_key:
        return False
    if not signature or not timestamp or VerifyKey is None:
        return False
    try:
        verify_key = VerifyKey(_decode_telnyx_public_key(public_key))
        signed_payload = timestamp.encode("utf-8") + b"|" + raw_body
        verify_key.verify(signed_payload, base64.b64decode(signature, validate=True))
        return True
    except (BadSignatureError, binascii.Error, ValueError):
        return False


def has_valid_shared_secret(x_webhook_secret: str | None, query_secret: str | None = None) -> bool:
    if ALLOW_NO_SECRET:
        return True
    supplied = x_webhook_secret or query_secret
    return supplied == WEBHOOK_SECRET


def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = _db_connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS insights (
                    id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_insights_received_at ON insights(received_at);
                """
            )
            conn.commit()
            _migrate_legacy_insights(conn)
        finally:
            conn.close()


def _migrate_legacy_insights(conn: sqlite3.Connection) -> None:
    insight_count = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    if insight_count == 0 and _LEGACY_INSIGHTS_PATH.exists():
        try:
            insights = json.loads(_LEGACY_INSIGHTS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            insights = []
        if isinstance(insights, list):
            for record in insights[-500:]:
                if isinstance(record, dict) and record.get("id"):
                    conn.execute(
                        "INSERT OR IGNORE INTO insights (id, received_at, data) VALUES (?, ?, ?)",
                        (
                            record["id"],
                            record.get("received_at") or utc_now(),
                            json.dumps(record, separators=(",", ":")),
                        ),
                    )
            conn.commit()


def read_insights() -> list[dict[str, Any]]:
    with _db_lock:
        conn = _db_connect()
        try:
            rows = conn.execute("SELECT data FROM insights ORDER BY received_at ASC").fetchall()
            insights: list[dict[str, Any]] = []
            for row in rows:
                try:
                    record = json.loads(row["data"])
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    insights.append(record)
            return insights
        finally:
            conn.close()


def append_insight(record: dict[str, Any]) -> None:
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute(
                "INSERT INTO insights (id, received_at, data) VALUES (?, ?, ?)",
                (
                    record["id"],
                    record.get("received_at") or utc_now(),
                    json.dumps(record, separators=(",", ":")),
                ),
            )
            excess = conn.execute("SELECT COUNT(*) - 500 FROM insights").fetchone()[0]
            if excess > 0:
                conn.execute(
                    """
                    DELETE FROM insights
                    WHERE id IN (
                        SELECT id FROM insights ORDER BY received_at ASC LIMIT ?
                    )
                    """,
                    (excess,),
                )
            conn.commit()
        finally:
            conn.close()


init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "miswitch-telnyx-webhook", "host": "webhook.miswitch.cloud"}


@app.post("/telnyx/insights")
async def receive_telnyx_insights(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
    telnyx_timestamp: str | None = Header(default=None),
) -> dict[str, Any]:
    # Prefer Telnyx's default Ed25519 webhook signature when TELNYX_PUBLIC_KEY is configured.
    # Keep the shared-secret path as a curl/testing fallback and for webhook UIs that cannot sign.
    raw_body = await request.body()
    signature_valid = verify_telnyx_signature(telnyx_signature_ed25519, telnyx_timestamp, raw_body)
    secret_valid = has_valid_shared_secret(x_webhook_secret, request.query_params.get("secret"))
    if not (signature_valid or secret_valid):
        raise HTTPException(status_code=401, detail="Missing or invalid Telnyx signature or webhook secret")
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON payload") from exc

    insight_id = uuid.uuid4().hex
    record = {
        "id": insight_id,
        "received_at": utc_now(),
        "path": str(request.url.path),
        "telnyx_signature_present": bool(telnyx_signature_ed25519),
        "telnyx_signature_verified": signature_valid,
        "shared_secret_verified": secret_valid,
        "telnyx_timestamp": telnyx_timestamp,
        "payload": payload,
    }
    append_insight(record)
    return {"accepted": True, "id": insight_id}


@app.get("/telnyx/insights")
def list_telnyx_insights(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, request.query_params.get("secret"))
    insights = read_insights()
    return {"count": len(insights), "insights": insights[-50:]}
