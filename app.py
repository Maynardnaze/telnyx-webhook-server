from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
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
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
TELNYX_API_KEY = env_or_file("TELNYX_API_KEY")
TELNYX_MESSAGES_URL = os.environ.get("TELNYX_MESSAGES_URL", "https://api.telnyx.com/v2/messages")
ASSISTANT_NAMES_PATH = Path(os.environ.get("ASSISTANT_NAMES_PATH", "/data/assistant-names.json"))
ASSISTANT_NAMES_JSON = os.environ.get("ASSISTANT_NAMES", "").strip()
ASSISTANT_NAMES_REFRESH_SECONDS = int(os.environ.get("ASSISTANT_NAMES_REFRESH_SECONDS", "900"))
TELNYX_ASSISTANTS_URL = os.environ.get("TELNYX_ASSISTANTS_URL", "https://api.telnyx.com/v2/ai/assistants?page[size]=100")
TELNYX_ASSISTANT_URL_TEMPLATE = os.environ.get("TELNYX_ASSISTANT_URL_TEMPLATE", "https://api.telnyx.com/v2/ai/assistants/{assistant_id}")
_assistant_names_cache: dict[str, str] = {}
_assistant_names_cache_at = 0.0

WAIVER_SMS_TEMPLATES = {
    "k1_speed": {
        "display_name": "K1 Speed Oxford",
        "url": "https://register.k1speed.com/oxf",
        "text": "Here is the K1 Speed Oxford waiver / online check-in link for your visit to Legacy nine-two-five: https://register.k1speed.com/oxf",
    },
    "urban_air": {
        "display_name": "Urban Air Oxford",
        "url": "https://store.unleashedbrands.com/urban-air/oxford-mi/waiver",
        "text": "Here is the Urban Air Oxford waiver link for your visit to Legacy nine-two-five: https://store.unleashedbrands.com/urban-air/oxford-mi/waiver",
    },
}

app = FastAPI(
    title="Miswitch Telnyx Webhook Server",
    version="0.2.0",
    description="Public webhook receiver for Telnyx behind webhook.miswitch.cloud.",
)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/admin/static", StaticFiles(directory=str(APP_DIR / "static"), check_dir=False), name="admin_static")

_db_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_secret(x_webhook_secret: str | None, query_secret: str | None = None) -> None:
    if ALLOW_NO_SECRET:
        return
    supplied = x_webhook_secret or query_secret
    if supplied != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Missing or invalid webhook secret")


def _secret_matches(supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(str(supplied), str(WEBHOOK_SECRET))


def _admin_signature(payload: str) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_admin_session() -> str:
    issued = str(int(time.time()))
    payload = f"admin:{issued}"
    return f"{payload}.{_admin_signature(payload)}"


def verify_admin_session(cookie_value: str | None) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    payload, signature = cookie_value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _admin_signature(payload)):
        return False
    try:
        role, issued = payload.split(":", 1)
        issued_at = int(issued)
    except ValueError:
        return False
    return role == "admin" and 0 <= time.time() - issued_at <= 60 * 60 * 12


def is_admin_request(request: Request) -> bool:
    return verify_admin_session(request.cookies.get("admin_session"))


def require_admin_response(request: Request) -> RedirectResponse | None:
    if is_admin_request(request):
        return None
    return RedirectResponse(url="/admin/login", status_code=303)


async def read_form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def json_pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _extract_telnyx_data(body: dict[str, Any]) -> Any:
    data = body.get("data") if isinstance(body, dict) else None
    return data if data is not None else body


def _assistant_name_from_item(item: dict[str, Any]) -> tuple[str | None, str | None]:
    assistant_id = first_present(item.get("id"), item.get("assistant_id"))
    assistant_name = first_present(item.get("name"), item.get("display_name"), item.get("title"))
    return (str(assistant_id) if assistant_id else None, str(assistant_name) if assistant_name else None)


def load_local_assistant_name_map() -> dict[str, str]:
    names: dict[str, str] = {}
    if ASSISTANT_NAMES_PATH.exists():
        try:
            parsed = json.loads(ASSISTANT_NAMES_PATH.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                names.update({str(key): str(value) for key, value in parsed.items() if value})
        except (OSError, json.JSONDecodeError):
            pass
    if ASSISTANT_NAMES_JSON:
        try:
            parsed = json.loads(ASSISTANT_NAMES_JSON)
            if isinstance(parsed, dict):
                names.update({str(key): str(value) for key, value in parsed.items() if value})
        except json.JSONDecodeError:
            pass
    return names


def fetch_telnyx_json(url: str) -> dict[str, Any] | None:
    if not TELNYX_API_KEY:
        return None
    req = urlrequest.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {TELNYX_API_KEY}",
            "Accept": "application/json",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return parsed if isinstance(parsed, dict) else None
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def load_telnyx_assistant_name_map() -> dict[str, str]:
    body = fetch_telnyx_json(TELNYX_ASSISTANTS_URL)
    data = _extract_telnyx_data(body or {})
    items = data if isinstance(data, list) else []
    names: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        assistant_id, assistant_name = _assistant_name_from_item(item)
        if assistant_id and assistant_name:
            names[assistant_id] = assistant_name
    return names


def load_assistant_name_map(force_refresh: bool = False) -> dict[str, str]:
    global _assistant_names_cache, _assistant_names_cache_at
    now = time.time()
    if not force_refresh and _assistant_names_cache and now - _assistant_names_cache_at < ASSISTANT_NAMES_REFRESH_SECONDS:
        return dict(_assistant_names_cache)
    names = load_telnyx_assistant_name_map()
    names.update(load_local_assistant_name_map())
    _assistant_names_cache = names
    _assistant_names_cache_at = now
    return dict(names)


def fetch_telnyx_assistant_name(assistant_id: str) -> str | None:
    if not assistant_id or not TELNYX_API_KEY:
        return None
    url = TELNYX_ASSISTANT_URL_TEMPLATE.format(assistant_id=quote(assistant_id, safe=""))
    body = fetch_telnyx_json(url)
    data = _extract_telnyx_data(body or {})
    if not isinstance(data, dict):
        return None
    returned_id, name = _assistant_name_from_item(data)
    if name and (not returned_id or returned_id == assistant_id):
        _assistant_names_cache[assistant_id] = name
        return name
    return None


def assistant_display_name(assistant_id: Any) -> str:
    assistant_id_str = str(assistant_id or "").strip()
    if not assistant_id_str:
        return ""
    names = load_assistant_name_map()
    return names.get(assistant_id_str) or fetch_telnyx_assistant_name(assistant_id_str) or assistant_id_str


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


def _normalize_phone(value: Any) -> str:
    phone = str(value or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if phone.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    raise HTTPException(status_code=400, detail="Invalid phone number")


def send_telnyx_sms(*, from_number: str, to_number: str, text: str) -> dict[str, Any]:
    if not TELNYX_API_KEY:
        raise HTTPException(status_code=500, detail="TELNYX_API_KEY is not configured")
    payload = json.dumps({"from": from_number, "to": to_number, "text": text}).encode("utf-8")
    req = urlrequest.Request(
        TELNYX_MESSAGES_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {TELNYX_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            try:
                data = json.loads(body or "{}")
            except json.JSONDecodeError:
                data = {"raw": body}
            return {"status_code": response.status, "response": data}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Telnyx Messages API error {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"Telnyx Messages API request failed: {exc.reason}") from exc


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

                CREATE TABLE IF NOT EXISTS async_tool_jobs (
                    id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    call_control_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    completed_at TEXT,
                    request_data TEXT NOT NULL,
                    result_data TEXT,
                    add_messages_dry_run TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_async_tool_jobs_received_at ON async_tool_jobs(received_at);
                CREATE INDEX IF NOT EXISTS idx_async_tool_jobs_status ON async_tool_jobs(status);
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


def redact_phone(value: Any) -> Any:
    if not isinstance(value, str) or len(value) < 7:
        return value
    if value.startswith("+") and value[1:].isdigit():
        return value[:3] + "***" + value[-2:]
    return value


def summarize_order_status(body: dict[str, Any]) -> dict[str, Any]:
    customer = body.get("customer") or body.get("caller") or {}
    order_id = body.get("order_id") or body.get("orderId") or body.get("case_id") or "DEMO-1001"
    caller = redact_phone(customer.get("phone") if isinstance(customer, dict) else body.get("from"))
    return {
        "order_id": order_id,
        "status": "ready_for_pickup",
        "eta_minutes": 0,
        "caller": caller,
        "confidence": "dry_run_mock",
        "note": "Dry-run result generated locally. Replace this lookup with n8n/Tripleseat/Zoho/NetSapiens logic before live sends.",
    }


def create_async_tool_job(tool_name: str, call_control_id: str, request_body: dict[str, Any]) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex,
        "tool_name": tool_name,
        "call_control_id": call_control_id,
        "status": "queued",
        "received_at": utc_now(),
        "completed_at": None,
        "request_data": request_body,
        "result_data": None,
        "add_messages_dry_run": None,
        "error": None,
    }
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute(
                """
                INSERT INTO async_tool_jobs
                (id, tool_name, call_control_id, status, received_at, completed_at, request_data, result_data, add_messages_dry_run, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"],
                    job["tool_name"],
                    job["call_control_id"],
                    job["status"],
                    job["received_at"],
                    job["completed_at"],
                    json.dumps(request_body, separators=(",", ":")),
                    None,
                    None,
                    None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return job


def _decode_job_row(row: sqlite3.Row) -> dict[str, Any]:
    def maybe_json(value: str | None) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    return {
        "id": row["id"],
        "tool_name": row["tool_name"],
        "call_control_id": row["call_control_id"],
        "status": row["status"],
        "received_at": row["received_at"],
        "completed_at": row["completed_at"],
        "request_data": maybe_json(row["request_data"]),
        "result_data": maybe_json(row["result_data"]),
        "add_messages_dry_run": maybe_json(row["add_messages_dry_run"]),
        "error": row["error"],
    }


def get_async_tool_job(job_id: str) -> dict[str, Any] | None:
    with _db_lock:
        conn = _db_connect()
        try:
            row = conn.execute("SELECT * FROM async_tool_jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
    return _decode_job_row(row) if row else None


def list_async_tool_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with _db_lock:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM async_tool_jobs ORDER BY received_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        finally:
            conn.close()
    return [_decode_job_row(row) for row in rows]


def run_async_tool_job(job_id: str) -> None:
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute("UPDATE async_tool_jobs SET status = ? WHERE id = ?", ("running", job_id))
            conn.commit()
        finally:
            conn.close()
    job = get_async_tool_job(job_id)
    if not job:
        return
    try:
        if job["tool_name"] == "order-status":
            result = summarize_order_status(job["request_data"] or {})
            content = (
                f"Background lookup complete for order {result['order_id']}: "
                f"status={result['status']}, eta={result['eta_minutes']} minutes."
            )
        else:
            result = {
                "tool_name": job["tool_name"],
                "status": "received",
                "confidence": "dry_run_mock",
                "note": "Generic dry-run async tool result. Add real lookup logic before live sends.",
            }
            content = f"Background tool {job['tool_name']} completed in dry-run mode."
        add_messages = {
            "call_control_id": job["call_control_id"],
            "messages": [{"role": "system", "content": content}],
        }
        status = "complete"
        error = None
    except Exception as exc:  # pragma: no cover - defensive background path
        result = None
        add_messages = None
        status = "error"
        error = repr(exc)
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute(
                """
                UPDATE async_tool_jobs
                SET status = ?, completed_at = ?, result_data = ?, add_messages_dry_run = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now(),
                    json.dumps(result, separators=(",", ":")) if result is not None else None,
                    json.dumps(add_messages, separators=(",", ":")) if add_messages is not None else None,
                    error,
                    job_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def store_insight(payload: dict[str, Any], auth_metadata: dict[str, Any] | None = None, path: str = "/telnyx/insights") -> dict[str, Any]:
    insight_id = uuid.uuid4().hex
    record = {
        "id": insight_id,
        "received_at": utc_now(),
        "path": path,
        **(auth_metadata or {}),
        "payload": payload,
    }
    append_insight(record)
    return record


def get_insight_by_id(insight_id: str) -> dict[str, Any] | None:
    with _db_lock:
        conn = _db_connect()
        try:
            row = conn.execute("SELECT data FROM insights WHERE id = ?", (insight_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        parsed = json.loads(row["data"])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_insight_fields(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    inner = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    event_type = data.get("event_type") or payload.get("event_type") or inner.get("event_type") or "unknown"
    summary = first_present(inner.get("summary"), inner.get("intent"), payload.get("summary"), data.get("summary")) or "No summary found"
    assistant_id = first_present(inner.get("assistant_id"), data.get("assistant_id"), payload.get("assistant_id"))
    return {
        "id": record.get("id"),
        "received_at": record.get("received_at"),
        "event_type": event_type,
        "assistant_id": assistant_id,
        "assistant_display_name": assistant_display_name(assistant_id),
        "conversation_id": first_present(inner.get("conversation_id"), data.get("conversation_id"), payload.get("conversation_id")),
        "caller": first_present(inner.get("from"), payload.get("from"), inner.get("caller"), payload.get("caller")),
        "signature_verified": bool(record.get("telnyx_signature_verified")),
        "shared_secret_verified": bool(record.get("shared_secret_verified")),
        "summary": str(summary)[:220],
    }


def list_insight_summaries(limit: int = 50, q: str = "") -> list[dict[str, Any]]:
    summaries = [extract_insight_fields(record) for record in read_insights()]
    if q:
        needle = q.lower()
        summaries = [item for item in summaries if needle in json.dumps(item, default=str).lower()]
    return summaries[-max(1, min(limit, 200)):][::-1]


def get_insight_stats() -> dict[str, Any]:
    insights = read_insights()
    signature_verified = 0
    shared_secret_verified = 0
    latest_received_at = None
    for record in insights:
        if record.get("telnyx_signature_verified"):
            signature_verified += 1
        if record.get("shared_secret_verified"):
            shared_secret_verified += 1
        latest_received_at = record.get("received_at") or latest_received_at
    return {
        "ok": True,
        "service": "miswitch-telnyx-webhook",
        "db_path": str(DB_PATH),
        "insight_count": len(insights),
        "latest_received_at": latest_received_at,
        "signature_verified_count": signature_verified,
        "shared_secret_verified_count": shared_secret_verified,
    }


init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "miswitch-telnyx-webhook", "host": "webhook.miswitch.cloud"}


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    fields = await read_form_fields(request)
    if not _secret_matches(fields.get("secret")):
        return templates.TemplateResponse(request, "admin_login.html", {"error": "Invalid shared secret"}, status_code=401)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "admin_session",
        make_admin_session(),
        max_age=60 * 60 * 12,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/admin/logout")
def admin_logout() -> RedirectResponse:
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    stats = get_insight_stats()
    recent = list_insight_summaries(limit=10)
    return templates.TemplateResponse(request, "admin_dashboard.html", {"stats": stats, "recent": recent})


@app.get("/admin/api/stats")
def admin_api_stats(request: Request):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    return get_insight_stats()


@app.get("/admin/insights", response_class=HTMLResponse)
def admin_insights_page(request: Request, q: str = "", limit: int = Query(default=50, ge=1, le=200)):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    insights = list_insight_summaries(limit=limit, q=q)
    return templates.TemplateResponse(request, "admin_insights.html", {"insights": insights, "q": q, "limit": limit})


@app.get("/admin/api/insights")
def admin_api_insights(request: Request, q: str = "", limit: int = Query(default=50, ge=1, le=200)):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    insights = list_insight_summaries(limit=limit, q=q)
    return {"count": len(insights), "insights": insights}


@app.get("/admin/insights/{insight_id}", response_class=HTMLResponse)
def admin_insight_detail_page(request: Request, insight_id: str):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    record = get_insight_by_id(insight_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    fields = extract_insight_fields(record)
    return templates.TemplateResponse(
        request,
        "admin_insight_detail.html",
        {"record": record, "fields": fields, "pretty_json": json_pretty(record)},
    )


@app.get("/admin/api/insights/{insight_id}")
def admin_api_insight_detail(request: Request, insight_id: str):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    record = get_insight_by_id(insight_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    return record


@app.get("/admin/tools/async-jobs", response_class=HTMLResponse)
def admin_async_jobs_page(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    jobs = list_async_tool_jobs(limit=limit)
    return templates.TemplateResponse(request, "admin_async_jobs.html", {"jobs": jobs})


@app.get("/admin/api/async-jobs")
def admin_api_async_jobs(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    jobs = list_async_tool_jobs(limit=limit)
    return {"count": len(jobs), "jobs": jobs}


@app.get("/admin/tools/assistant-init", response_class=HTMLResponse)
def admin_assistant_init_page(request: Request):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    sample = json_pretty({"data": {"payload": {"telnyx_end_user_target": "+12485550199", "telnyx_agent_target": "+12485550100", "telnyx_conversation_channel": "phone_call"}}})
    return templates.TemplateResponse(request, "admin_assistant_init.html", {"payload": sample, "result": None, "error": None})


@app.post("/admin/tools/assistant-init", response_class=HTMLResponse)
async def admin_assistant_init_submit(request: Request):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    fields = await read_form_fields(request)
    payload_text = fields.get("payload", "")
    try:
        payload = json.loads(payload_text or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Top-level JSON must be an object")
        result = build_assistant_initialization_response(
            payload,
            assistant_id_override=fields.get("assistant_id") or None,
            customer_id_override=fields.get("customer_id") or None,
            tenant_id_override=fields.get("tenant_id") or None,
            assistant_family_override=fields.get("assistant_family") or None,
            environment_override=fields.get("environment") or None,
        )
        return templates.TemplateResponse(request, "admin_assistant_init.html", {"payload": payload_text, "result": json_pretty(result), "error": None})
    except Exception as exc:
        return templates.TemplateResponse(request, "admin_assistant_init.html", {"payload": payload_text, "result": None, "error": str(exc)}, status_code=400)


@app.get("/admin/tools/webhook-simulator", response_class=HTMLResponse)
def admin_webhook_simulator_page(request: Request):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    sample = json_pretty({"event_type": "conversation_insight_result", "payload": {"summary": "Sample insight from admin simulator"}})
    return templates.TemplateResponse(request, "admin_webhook_simulator.html", {"payload": sample, "result": None, "error": None})


@app.post("/admin/tools/webhook-simulator", response_class=HTMLResponse)
async def admin_webhook_simulator_submit(request: Request):
    redirect = require_admin_response(request)
    if redirect:
        return redirect
    fields = await read_form_fields(request)
    payload_text = fields.get("payload", "")
    try:
        payload = json.loads(payload_text or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Top-level JSON must be an object")
        record = store_insight(payload, {"admin_simulated": True, "shared_secret_verified": True}, path="/admin/tools/webhook-simulator")
        result = {"message": "Stored insight", "id": record["id"], "detail_url": f"/admin/insights/{record['id']}"}
        return templates.TemplateResponse(request, "admin_webhook_simulator.html", {"payload": payload_text, "result": json_pretty(result), "error": None})
    except Exception as exc:
        return templates.TemplateResponse(request, "admin_webhook_simulator.html", {"payload": payload_text, "result": None, "error": str(exc)}, status_code=400)


@app.post("/telnyx/tools/send-waiver-sms")
async def send_waiver_sms_tool(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, request.query_params.get("secret"))
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected top-level JSON object")

    business = str(payload.get("business") or "").strip()
    template = WAIVER_SMS_TEMPLATES.get(business)
    if not template:
        raise HTTPException(status_code=400, detail="Unsupported waiver business")

    from_number = _normalize_phone(payload.get("from"))
    to_number = _normalize_phone(payload.get("to"))
    result = send_telnyx_sms(from_number=from_number, to_number=to_number, text=template["text"])
    message_id = None
    response_data = result.get("response")
    if isinstance(response_data, dict):
        message_id = (response_data.get("data") or {}).get("id") if isinstance(response_data.get("data"), dict) else response_data.get("id")
    return {
        "ok": True,
        "business": business,
        "display_name": template["display_name"],
        "sent_to": to_number,
        "message_id": message_id,
        "message": f"Sent {template['display_name']} waiver link by SMS.",
    }


@app.post("/telnyx/tools/async/{tool_name}")
async def receive_async_tool_request(
    tool_name: str,
    background_tasks: BackgroundTasks,
    request: Request,
    x_telnyx_call_control_id: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, request.query_params.get("secret"))
    if not x_telnyx_call_control_id:
        raise HTTPException(status_code=400, detail="Missing x-telnyx-call-control-id header")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected top-level JSON object")
    started = time.perf_counter()
    job = create_async_tool_job(tool_name=tool_name, call_control_id=x_telnyx_call_control_id, request_body=payload)
    background_tasks.add_task(run_async_tool_job, job["id"])
    return {
        "ok": True,
        "mode": "async_ack_dry_run",
        "job_id": job["id"],
        "ack_ms": round((time.perf_counter() - started) * 1000, 2),
        "message": "Accepted. Background work queued; inspect /admin/tools/async-jobs for dry-run Add Messages payload.",
    }


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

    record = store_insight(
        payload,
        {
            "telnyx_signature_present": bool(telnyx_signature_ed25519),
            "telnyx_signature_verified": signature_valid,
            "shared_secret_verified": secret_valid,
            "telnyx_timestamp": telnyx_timestamp,
        },
        path=str(request.url.path),
    )
    return {"accepted": True, "id": record["id"]}


@app.get("/telnyx/insights")
def list_telnyx_insights(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, request.query_params.get("secret"))
    insights = read_insights()
    return {"count": len(insights), "insights": insights[-50:]}
