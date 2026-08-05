"""One-off backfill: copy every stored MySwitch insight from local SQLite
(data/webhook.db) into the shared Supabase `calls` table.

Run manually, once, after DATABASE_URL is configured and backend/schema.sql
has been applied to the Supabase database:

    DATABASE_URL=postgresql://... python3 scripts/backfill_supabase.py

Safe to re-run: upserts on `id` (ON CONFLICT DO UPDATE), same as the live
webhook path in app.py.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import build_call_record, upsert_call_to_supabase, DB_PATH  # noqa: E402
import json  # noqa: E402


def read_local_insights() -> list[dict]:
    # Explicit read-only open: this is the live production database, never write to it here.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT data FROM insights ORDER BY received_at ASC").fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        try:
            record = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


async def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL must be set")

    records = read_local_insights()
    print(f"Read {len(records)} records from {DB_PATH}")

    migrated = 0
    skipped = 0
    for record in records:
        call_row = build_call_record(record)
        if call_row is None:
            skipped += 1
            continue
        await upsert_call_to_supabase(call_row)
        migrated += 1

    print(f"Migrated {migrated} call records, skipped {skipped} non-MySwitch/unidentifiable records.")


if __name__ == "__main__":
    asyncio.run(main())
