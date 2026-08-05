"""One-off migration: copy all local SQLite state (data/webhook.db) into
Supabase Postgres — insights, insight_reviews, async_tool_jobs, and
sms_idempotency_keys — then refresh the shared `calls` table from the migrated
insights.

    DATABASE_URL=postgresql://... python3 scripts/migrate_sqlite_to_supabase.py [path/to/webhook.db]

Safe to re-run: primary tables insert with ON CONFLICT DO NOTHING; `calls`
rows upsert exactly like the live webhook path. Supersedes the old
backfill_supabase.py (calls-only backfill).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not os.environ.get("DATABASE_URL"):
    raise SystemExit("DATABASE_URL must be set")

# Importing app runs init_db() against Postgres, creating every table we need.
from app import _get_pg_pool, build_call_record, upsert_call_to_supabase  # noqa: E402

TABLES = ("insights", "insight_reviews", "async_tool_jobs", "sms_idempotency_keys")


def main() -> None:
    sqlite_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data" / "webhook.db"
    # Explicit read-only open: never write to the source database here.
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    insight_records: list[dict] = []
    with _get_pg_pool().connection() as pg:
        for table in TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            copied = 0
            for row in rows:
                cols = row.keys()
                placeholders = ", ".join(["%s"] * len(cols))
                pg.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    tuple(row[c] for c in cols),
                )
                copied += 1
                if table == "insights":
                    try:
                        record = json.loads(row["data"])
                        if isinstance(record, dict):
                            insight_records.append(record)
                    except json.JSONDecodeError:
                        pass
            print(f"{table}: copied {copied} rows")
    src.close()

    mirrored = skipped = 0
    for record in insight_records:
        call_row = build_call_record(record)
        if call_row is None:
            skipped += 1
            continue
        upsert_call_to_supabase(call_row)
        mirrored += 1
    print(f"calls: mirrored {mirrored} MySwitch records, skipped {skipped} others")


if __name__ == "__main__":
    main()
