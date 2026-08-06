"""Sync NetSapiens (SkySwitch) CDRs into the Postgres ``cdrs`` table.

Pulls call detail records from the NetSapiens v1 API (``object=cdr2&action=read``)
for one PBX domain and upserts them into Supabase keyed on ``cdr_id``. Each row
stores a handful of promoted query columns plus the full raw record (including
the ``CdrR`` leg detail) as JSONB.

Designed to run repeatedly: date windows overlap and upserts are idempotent, so
a cron entry like ``--days 2`` every 15 minutes is safe. Use ``--start`` for a
one-off historical backfill.

Required environment (supplied by Doppler):
    NETSAPIENS_BASE_URL, NETSAPIENS_CLIENT_ID, NETSAPIENS_CLIENT_SECRET,
    NETSAPIENS_USERNAME, NETSAPIENS_PASSWORD, DATABASE_URL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import psycopg

DEFAULT_DOMAIN = os.environ.get("NETSAPIENS_CDR_DOMAIN", "miswitch.22191.service")
PAGE_SIZE = 500
HTTP_TIMEOUT = 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cdrs (
  cdr_id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  territory TEXT,
  type_code INTEGER,
  orig_sub TEXT,
  term_sub TEXT,
  by_sub TEXT,
  orig_from_user TEXT,
  orig_from_name TEXT,
  orig_to_user TEXT,
  orig_req_user TEXT,
  time_start TIMESTAMPTZ,
  time_answer TIMESTAMPTZ,
  time_release TIMESTAMPTZ,
  duration INTEGER,
  time_talking INTEGER,
  release_code TEXT,
  release_text TEXT,
  codec TEXT,
  mos REAL,
  raw JSONB NOT NULL,
  first_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE cdrs ADD COLUMN IF NOT EXISTS mos REAL;
CREATE INDEX IF NOT EXISTS idx_cdrs_time_start ON cdrs (time_start);
CREATE INDEX IF NOT EXISTS idx_cdrs_from_time ON cdrs (orig_from_user, time_start);
CREATE INDEX IF NOT EXISTS idx_cdrs_domain_time ON cdrs (domain, time_start);
"""

UPSERT_SQL = """
INSERT INTO cdrs (
  cdr_id, domain, territory, type_code, orig_sub, term_sub, by_sub,
  orig_from_user, orig_from_name, orig_to_user, orig_req_user,
  time_start, time_answer, time_release, duration, time_talking,
  release_code, release_text, codec, mos, raw
) VALUES (
  %(cdr_id)s, %(domain)s, %(territory)s, %(type_code)s, %(orig_sub)s,
  %(term_sub)s, %(by_sub)s, %(orig_from_user)s, %(orig_from_name)s,
  %(orig_to_user)s, %(orig_req_user)s, %(time_start)s, %(time_answer)s,
  %(time_release)s, %(duration)s, %(time_talking)s, %(release_code)s,
  %(release_text)s, %(codec)s, %(mos)s, %(raw)s
)
ON CONFLICT (cdr_id) DO UPDATE SET
  duration = EXCLUDED.duration,
  time_answer = EXCLUDED.time_answer,
  time_release = EXCLUDED.time_release,
  time_talking = EXCLUDED.time_talking,
  release_code = EXCLUDED.release_code,
  release_text = EXCLUDED.release_text,
  mos = EXCLUDED.mos,
  raw = EXCLUDED.raw,
  updated_at = NOW()
"""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def api_base() -> str:
    return require_env("NETSAPIENS_BASE_URL").rstrip("/")


def post_form(url: str, fields: dict[str, str], token: str | None = None) -> bytes:
    req = urlrequest.Request(url, data=urlencode(fields).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def get_access_token() -> str:
    body = post_form(
        f"{api_base()}/oauth2/token/",
        {
            "client_id": require_env("NETSAPIENS_CLIENT_ID"),
            "client_secret": require_env("NETSAPIENS_CLIENT_SECRET"),
            "username": require_env("NETSAPIENS_USERNAME"),
            "password": require_env("NETSAPIENS_PASSWORD"),
            "grant_type": "password",
        },
    )
    data = json.loads(body)
    token = data.get("access_token")
    if not token:
        raise SystemExit(f"Token grant failed: {data}")
    return str(token)


def fetch_cdr_page(token: str, domain: str, start: datetime, end: datetime, offset: int) -> list[dict]:
    body = post_form(
        f"{api_base()}/",
        {
            "object": "cdr2",
            "action": "read",
            "domain": domain,
            "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "raw": "yes",
            "qos": "yes",
            "start": str(offset),
            "limit": str(PAGE_SIZE),
        },
        token=token,
    )
    data = json.loads(body) if body.strip() else []
    if not isinstance(data, list):
        # The API answers {} for empty/invalid windows.
        return []
    return data


SUBSCRIPTION_TTL_DAYS = 365
SUBSCRIPTION_RENEW_BEFORE_DAYS = 30


def _extract_subscriptions(data: object) -> list[dict]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
        return [data] if data else []
    return []


def ensure_cdr_subscription(token: str, domain: str) -> None:
    """Keep a non-expiring CDR push subscription pointed at NETSAPIENS_CDR_POST_URL.

    NetSapiens subscriptions always carry an expiry (and the docs warn the
    server computes it unreliably), so every sync run recreates the
    subscription whenever it is missing or expires within
    SUBSCRIPTION_RENEW_BEFORE_DAYS.
    """
    post_url = os.environ.get("NETSAPIENS_CDR_POST_URL", "").strip()
    if not post_url:
        return
    body = post_form(
        f"{api_base()}/",
        {"object": "event", "action": "read", "domain": domain, "format": "json"},
        token=token,
    )
    try:
        subs = _extract_subscriptions(json.loads(body) if body.strip() else [])
    except ValueError:
        subs = []
    endpoint_base = post_url.split("?", 1)[0]
    ours = [
        s for s in subs
        if str(s.get("model")) == "cdr"
        and str(s.get("domain")) == domain
        and str(s.get("post_url", "")).split("?", 1)[0] == endpoint_base
    ]
    now = datetime.now(tz=timezone.utc)
    renew_cutoff = now + timedelta(days=SUBSCRIPTION_RENEW_BEFORE_DAYS)
    keep = None
    for sub in ours:
        try:
            expires = datetime.strptime(str(sub.get("expires")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            expires = None
        if expires and expires > renew_cutoff and keep is None and str(sub.get("post_url")) == post_url:
            keep = sub
        else:
            sub_id = sub.get("subscription_id")
            if sub_id:
                post_form(
                    f"{api_base()}/",
                    {"object": "event", "action": "delete", "subscription_id": str(sub_id)},
                    token=token,
                )
                print(f"Deleted stale/expiring CDR subscription {sub_id}")
    if keep:
        return
    expires_str = (now + timedelta(days=SUBSCRIPTION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    post_form(
        f"{api_base()}/",
        {
            "object": "event",
            "action": "create",
            "model": "cdr",
            "domain": domain,
            "user": "*",
            "post_url": post_url,
            "expires": expires_str,
        },
        token=token,
    )
    print(f"Created CDR subscription for {domain} expiring {expires_str}")


def epoch_to_dt(value: object) -> datetime | None:
    try:
        epoch = int(str(value))
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def to_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def from_user(record: dict, leg: dict) -> str | None:
    if leg.get("orig_from_user"):
        return str(leg["orig_from_user"])
    uri = str(record.get("orig_from_uri") or "")
    if uri.startswith("sip:") and "@" in uri:
        return uri[4:].split("@", 1)[0]
    return None


def extract_mos(record: dict) -> float | None:
    """Worst MOS across both legs' A/B streams (portal shows the same score /10)."""
    values = []
    for leg_key in ("qos_orig", "qos_term"):
        leg = record.get(leg_key)
        if not isinstance(leg, dict):
            continue
        for side in ("a_mos_min_mult10", "b_mos_min_mult10"):
            try:
                values.append(int(str(leg.get(side))))
            except (TypeError, ValueError):
                continue
    return min(values) / 10 if values else None


def to_row(record: dict) -> dict | None:
    cdr_id = str(record.get("cdr_id") or "").strip()
    if not cdr_id:
        return None
    leg = record.get("CdrR") or {}
    return {
        "cdr_id": cdr_id,
        "domain": str(record.get("domain") or ""),
        "territory": record.get("territory"),
        "type_code": to_int(record.get("type")),
        "orig_sub": record.get("orig_sub"),
        "term_sub": record.get("term_sub"),
        "by_sub": record.get("by_sub"),
        "orig_from_user": from_user(record, leg),
        "orig_from_name": record.get("orig_from_name"),
        "orig_to_user": record.get("orig_to_user"),
        "orig_req_user": record.get("orig_req_user"),
        "time_start": epoch_to_dt(record.get("time_start")),
        "time_answer": epoch_to_dt(record.get("time_answer")),
        "time_release": epoch_to_dt(record.get("time_release")),
        "duration": to_int(record.get("duration")),
        "time_talking": to_int(record.get("time_talking")),
        "release_code": leg.get("release_code"),
        "release_text": leg.get("release_text"),
        "codec": leg.get("codec"),
        "mos": extract_mos(record),
        "raw": json.dumps(record),
    }


def sync(domain: str, start: datetime, end: datetime, dry_run: bool) -> None:
    token = get_access_token()
    if not dry_run:
        try:
            ensure_cdr_subscription(token, domain)
        except Exception as exc:  # noqa: BLE001 - subscription upkeep must not block the sync
            print(f"Warning: could not ensure CDR subscription: {exc}")
    rows: list[dict] = []
    offset = 0
    while True:
        page = fetch_cdr_page(token, domain, start, end, offset)
        rows.extend(r for r in (to_row(rec) for rec in page) if r)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    print(f"Fetched {len(rows)} CDRs for {domain} between {start} and {end}")
    if dry_run or not rows:
        if dry_run:
            print("Dry run: not writing to the database")
        return

    database_url = require_env("DATABASE_URL")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.executemany(UPSERT_SQL, rows)
        conn.commit()
    print(f"Upserted {len(rows)} rows into cdrs")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync NetSapiens CDRs into Postgres")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="PBX domain to pull CDRs for")
    parser.add_argument("--days", type=float, default=2.0, help="Lookback window in days (ignored when --start is set)")
    parser.add_argument("--start", help="Window start, 'YYYY-MM-DD[ HH:MM:SS]' (UTC)")
    parser.add_argument("--end", help="Window end, 'YYYY-MM-DD[ HH:MM:SS]' (UTC); defaults to now")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report, but do not write")
    args = parser.parse_args()

    def parse_when(value: str) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        raise SystemExit(f"Unparseable date: {value!r}")

    end = parse_when(args.end) if args.end else datetime.now(tz=timezone.utc)
    start = parse_when(args.start) if args.start else end - timedelta(days=args.days)
    if start >= end:
        raise SystemExit("Window start must be before end")

    try:
        sync(args.domain, start, end, args.dry_run)
    except (HTTPError, URLError) as exc:
        raise SystemExit(f"NetSapiens API request failed: {exc}") from exc


if __name__ == "__main__":
    main()
