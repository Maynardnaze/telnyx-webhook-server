from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sqlite3
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Request

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
TELNYX_TOLERANCE_SECONDS = int(os.environ.get("TELNYX_TOLERANCE_SECONDS", "300"))
DEFAULT_ASSISTANT_FAMILY = os.environ.get("ASSISTANT_MEMORY_FAMILY", "miswitch-ai-assistants")
DEFAULT_MEMORY_LIMIT = int(os.environ.get("ASSISTANT_MEMORY_LIMIT", "5"))
ASSISTANT_MEMORY_INSIGHT_QUERY = os.environ.get("ASSISTANT_MEMORY_INSIGHT_QUERY", "").strip()
ASSISTANT_MEMORY_PROFILES = os.environ.get("ASSISTANT_MEMORY_PROFILES", "").strip()

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
        timestamp_int = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp_int) > TELNYX_TOLERANCE_SECONDS:
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


def first_present(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            nested = first_present(
                value.get("phone_number"),
                value.get("number"),
                value.get("value"),
                value.get("id"),
            )
            if nested:
                return nested
        elif isinstance(value, list):
            nested = first_present(*value)
            if nested:
                return nested
        elif str(value):
            return str(value)
    return None


def load_assistant_memory_profiles() -> dict[str, dict[str, Any]]:
    """Optional per-assistant memory config, supplied as JSON in ASSISTANT_MEMORY_PROFILES."""
    if not ASSISTANT_MEMORY_PROFILES:
        return {}
    try:
        parsed = json.loads(ASSISTANT_MEMORY_PROFILES)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items() if isinstance(value, dict)}


def normalize_phone(value: Any) -> str | None:
    raw = first_present(value)
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("sip:"):
        return raw
    digits = re.sub(r"\D+", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    return raw if raw else None


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "a", "verified"}


def extract_assistant_init_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    inner = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    return inner or data or payload


def safe_metadata_value(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value).strip()).strip("-._")
    return text[:120] if text else None


def build_memory_query(phone: str, metadata: dict[str, str], limit: int) -> str:
    parts = [f"metadata->telnyx_end_user_target=eq.{quote(phone, safe='')}"]
    for key in ("customer_id", "tenant_id", "assistant_family", "environment"):
        value = metadata.get(key)
        if value:
            parts.append(f"metadata->{key}=eq.{quote(value, safe='')}")
    parts.append(f"limit={max(1, min(limit, 20))}")
    parts.append("order=last_message_at.desc")
    return "&".join(parts)


def build_assistant_initialization_response(
    payload: dict[str, Any],
    assistant_id_override: str | None = None,
    customer_id_override: str | None = None,
    tenant_id_override: str | None = None,
    assistant_family_override: str | None = None,
    environment_override: str | None = None,
) -> dict[str, Any]:
    init = extract_assistant_init_payload(payload)
    assistant_id = first_present(
        assistant_id_override,
        init.get("assistant_id"),
        init.get("assistant", {}),
        payload.get("assistant_id"),
    )
    profile: dict[str, Any] = load_assistant_memory_profiles().get(str(assistant_id), {}) if assistant_id else {}

    end_user_target = normalize_phone(init.get("telnyx_end_user_target") or init.get("from") or init.get("caller"))
    agent_target = normalize_phone(init.get("telnyx_agent_target") or init.get("to") or init.get("callee"))
    channel = first_present(init.get("telnyx_conversation_channel"), init.get("channel")) or "unknown"
    caller_verified = boolish(init.get("telnyx_end_user_target_verified") or init.get("telnyx_shaken_stir_attestation"))

    customer_id = safe_metadata_value(customer_id_override or profile.get("customer_id"))
    tenant_id = safe_metadata_value(tenant_id_override or profile.get("tenant_id"))
    assistant_family = safe_metadata_value(assistant_family_override or profile.get("assistant_family") or DEFAULT_ASSISTANT_FAMILY)
    environment = safe_metadata_value(environment_override or profile.get("environment") or "prod")
    memory_limit = int(profile.get("memory_limit") or DEFAULT_MEMORY_LIMIT)

    conversation_metadata: dict[str, str] = {
        "assistant_family": assistant_family or DEFAULT_ASSISTANT_FAMILY,
        "environment": environment or "prod",
        "memory_enabled": "true" if end_user_target else "false",
        "memory_scope": "caller_customer" if customer_id else "caller_family",
        "caller_verified": "true" if caller_verified else "false",
    }
    if assistant_id:
        conversation_metadata["assistant_id"] = str(assistant_id)
    if customer_id:
        conversation_metadata["customer_id"] = customer_id
    if tenant_id:
        conversation_metadata["tenant_id"] = tenant_id
    if end_user_target:
        conversation_metadata["telnyx_end_user_target"] = end_user_target
    if agent_target:
        conversation_metadata["telnyx_agent_target"] = agent_target
    if channel:
        conversation_metadata["telnyx_conversation_channel"] = str(channel)

    response: dict[str, Any] = {
        "dynamic_variables": {
            "caller_phone": end_user_target or "unknown",
            "caller_known": "true" if end_user_target else "false",
            "caller_verified": "true" if caller_verified else "false",
            "memory_enabled": "true" if end_user_target else "false",
            "memory_scope": conversation_metadata["memory_scope"],
            "customer_id": customer_id or "generic",
            "tenant_id": tenant_id or customer_id or "generic",
            "assistant_family": conversation_metadata["assistant_family"],
        },
        "conversation": {"metadata": conversation_metadata},
    }
    if end_user_target:
        memory: dict[str, str] = {"conversation_query": build_memory_query(end_user_target, conversation_metadata, memory_limit)}
        if ASSISTANT_MEMORY_INSIGHT_QUERY:
            memory["insight_query"] = ASSISTANT_MEMORY_INSIGHT_QUERY
        response["memory"] = memory
    return response


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


@app.post("/telnyx/assistant/init")
async def assistant_initialization(
    request: Request,
    assistant_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    assistant_family: str | None = Query(default=None),
    environment: str | None = Query(default=None),
    x_webhook_secret: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
    telnyx_timestamp: str | None = Header(default=None),
) -> dict[str, Any]:
    """Dynamic Variables Webhook for Telnyx Assistant memory initialization."""
    raw_body = await request.body()
    signature_valid = verify_telnyx_signature(telnyx_signature_ed25519, telnyx_timestamp, raw_body)
    secret_valid = has_valid_shared_secret(x_webhook_secret, request.query_params.get("secret"))
    if not (signature_valid or secret_valid):
        raise HTTPException(status_code=401, detail="Missing or invalid Telnyx signature or webhook secret")
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected top-level JSON object")

    return build_assistant_initialization_response(
        payload,
        assistant_id_override=assistant_id,
        customer_id_override=customer_id,
        tenant_id_override=tenant_id,
        assistant_family_override=assistant_family,
        environment_override=environment,
    )


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
