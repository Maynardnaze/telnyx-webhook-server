from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

try:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
except ImportError:  # pragma: no cover - only happens if optional dependency is missing locally
    BadSignatureError = Exception
    VerifyKey = None

APP_DIR = Path(__file__).parent
DATA_PATH = Path(os.environ.get("DIRECTORY_DATA", APP_DIR / "tenants.json"))
JOBS_PATH = Path(os.environ.get("WEBHOOK_JOBS_PATH", "/data/jobs.json"))
INSIGHTS_PATH = Path(os.environ.get("WEBHOOK_INSIGHTS_PATH", "/data/insights.json"))


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
GUEST_SERVICES_PHONE = os.environ.get("GUEST_SERVICES_PHONE", "+124****0000")

app = FastAPI(
    title="Miswitch Telnyx Webhook Server",
    version="0.1.0",
    description="Public webhook tools for Telnyx AI Assistants behind webhook.miswitch.cloud.",
)

_jobs_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_secret(x_webhook_secret: str | None, x_directory_secret: str | None, query_secret: str | None = None) -> None:
    if ALLOW_NO_SECRET:
        return
    supplied = x_webhook_secret or x_directory_secret or query_secret
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


def has_valid_shared_secret(x_webhook_secret: str | None, x_directory_secret: str | None, query_secret: str | None = None) -> bool:
    if ALLOW_NO_SECRET:
        return True
    supplied = x_webhook_secret or x_directory_secret or query_secret
    return supplied == WEBHOOK_SECRET


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def load_tenants() -> list[dict[str, Any]]:
    with DATA_PATH.open("r", encoding="utf-8") as f:
        data: Any = json.load(f)
    tenants: Any = data["tenants"] if isinstance(data, dict) and "tenants" in data else data
    if not isinstance(tenants, list):
        raise ValueError(f"Directory data must be a list or object with tenants list: {DATA_PATH}")
    return [tenant for tenant in tenants if isinstance(tenant, dict)]


def score_tenant(query: str, tenant: dict[str, Any]) -> float:
    q = normalize(query)
    if not q:
        return 0.0
    names = [tenant.get("display_name", ""), tenant.get("category", ""), tenant.get("suite", "")]
    names.extend(tenant.get("aliases", []) or [])

    best = 0.0
    for name in names:
        n = normalize(str(name))
        if not n:
            continue
        if q == n:
            best = max(best, 1.0)
        elif q in n or n in q:
            best = max(best, 0.88 if len(q) >= 4 else 0.72)
        else:
            best = max(best, SequenceMatcher(None, q, n).ratio() * 0.82)
    return round(best, 3)


def directory_lookup(query: str) -> dict[str, Any]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for tenant in load_tenants():
        if not tenant.get("transfer_enabled", True):
            continue
        score = score_tenant(query, tenant)
        if score >= 0.45:
            scored.append((score, tenant))
    scored.sort(key=lambda item: item[0], reverse=True)

    matches = [
        {
            "name": tenant.get("display_name"),
            "category": tenant.get("category"),
            "suite": tenant.get("suite"),
            "phone": tenant.get("phone"),
            "hours": tenant.get("hours"),
            "confidence": score,
        }
        for score, tenant in scored[:5]
    ]

    if not matches:
        status = "no_match"
    elif matches[0]["confidence"] >= 0.86 and (len(matches) == 1 or matches[0]["confidence"] - matches[1]["confidence"] >= 0.12):
        status = "single_match"
        matches = matches[:1]
    else:
        status = "multiple_matches"

    return {
        "status": status,
        "query": query,
        "matches": matches,
        "fallback": {"name": "Mall Guest Services", "phone": GUEST_SERVICES_PHONE},
        "instructions_for_assistant": (
            "If single_match, confirm the store name briefly and transfer to matches[0].phone. "
            "If multiple_matches, ask which listed store they want. If no_match, offer guest services."
        ),
    }


def read_jobs() -> dict[str, Any]:
    if not JOBS_PATH.exists():
        return {}
    try:
        return json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_jobs(jobs: dict[str, Any]) -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(JOBS_PATH)


def save_job(job_id: str, patch: dict[str, Any]) -> None:
    with _jobs_lock:
        jobs = read_jobs()
        current = jobs.get(job_id, {})
        current.update(patch)
        jobs[job_id] = current
        write_jobs(jobs)


def read_insights() -> list[dict[str, Any]]:
    if not INSIGHTS_PATH.exists():
        return []
    try:
        data = json.loads(INSIGHTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def append_insight(record: dict[str, Any]) -> None:
    with _jobs_lock:
        insights = read_insights()
        insights.append(record)
        INSIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = INSIGHTS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(insights[-500:], indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(INSIGHTS_PATH)


def process_async_directory_job(job_id: str, query: str, call_control_id: str | None, payload: dict[str, Any]) -> None:
    save_job(job_id, {"status": "running", "started_at": utc_now()})
    # Placeholder for slower CRM/PBX/Telnyx enrichment. Keep the public HTTP response immediate.
    time.sleep(float(os.environ.get("ASYNC_MOCK_DELAY_SECONDS", "0.25")))
    result = directory_lookup(query)
    system_message = {
        "role": "system",
        "content": f"Directory lookup completed for query '{query}': {json.dumps(result, separators=(',', ':'))}",
    }
    save_job(
        job_id,
        {
            "status": "completed",
            "completed_at": utc_now(),
            "call_control_id": call_control_id,
            "result": result,
            "would_inject": {
                "telnyx_api": "Add Messages API",
                "dry_run": True,
                "call_control_id": call_control_id,
                "message": system_message,
            },
            "payload_preview": payload,
        },
    )


class DirectorySearchRequest(BaseModel):
    query: str = Field(..., description="Caller requested store, business, service, department, or category.")
    caller_intent: str | None = Field(default="transfer")
    caller_number: str | None = None
    current_time: str | None = None


class AsyncDirectoryRequest(BaseModel):
    query: str
    caller_number: str | None = None
    context: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    tenants = load_tenants()
    return {"ok": True, "service": "miswitch-telnyx-webhook", "host": "webhook.miswitch.cloud", "tenant_count": len(tenants)}


@app.post("/telnyx/insights")
async def receive_telnyx_insights(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    x_directory_secret: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
    telnyx_timestamp: str | None = Header(default=None),
) -> dict[str, Any]:
    # Prefer Telnyx's default Ed25519 webhook signature when TELNYX_PUBLIC_KEY is configured.
    # Keep the shared-secret path as a curl/testing fallback and for webhook UIs that cannot sign.
    raw_body = await request.body()
    signature_valid = verify_telnyx_signature(telnyx_signature_ed25519, telnyx_timestamp, raw_body)
    secret_valid = has_valid_shared_secret(x_webhook_secret, x_directory_secret, request.query_params.get("secret"))
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
    x_directory_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, x_directory_secret, request.query_params.get("secret"))
    insights = read_insights()
    return {"count": len(insights), "insights": insights[-50:]}


@app.post("/telnyx/mall-directory/search")
async def mall_directory_search(
    payload: DirectorySearchRequest,
    x_webhook_secret: str | None = Header(default=None),
    x_directory_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, x_directory_secret)
    return directory_lookup(payload.query)


@app.post("/telnyx/tools/directory-lookup-async")
async def directory_lookup_async(
    payload: AsyncDirectoryRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    x_telnyx_call_control_id: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    x_directory_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, x_directory_secret)
    job_id = uuid.uuid4().hex
    payload_dict = payload.model_dump()
    save_job(
        job_id,
        {
            "status": "accepted",
            "accepted_at": utc_now(),
            "path": str(request.url.path),
            "query": payload.query,
            "call_control_id": x_telnyx_call_control_id,
        },
    )
    background_tasks.add_task(process_async_directory_job, job_id, payload.query, x_telnyx_call_control_id, payload_dict)
    return {"accepted": True, "job_id": job_id, "dry_run": True}


@app.get("/jobs")
def list_jobs(
    x_webhook_secret: str | None = Header(default=None),
    x_directory_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, x_directory_secret)
    return {"jobs": read_jobs()}


@app.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    x_webhook_secret: str | None = Header(default=None),
    x_directory_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_secret(x_webhook_secret, x_directory_secret)
    jobs = read_jobs()
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="job not found")
    return jobs[job_id]
