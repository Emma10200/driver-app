#!/usr/bin/env python3
"""Finalize a chunked hourly-evidence job from already-written hourly rows.

Use this if compute_pair_hourly_evidence.py completed chunk writes but failed while
updating final billable flags or daily/weekly summaries.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.qbo_supabase import SupabaseRestClient
from scripts.compute_pair_hourly_evidence import (
    DEFAULT_BATCH_SIZE,
    SupabaseClient,
    _load_secrets,
    _parse_date_or_datetime,
    _week_start,
    apply_billable_candidate_rules,
    dedupe_hourly_rows,
    summarize_rows,
)


def main() -> None:
    args = _parse_args()
    rest = SupabaseRestClient()
    job = _load_job(rest, args.job_id)
    rows = rest.select_all(
        "asset_pair_hourly_evidence",
        filters={"job_id": f"eq.{args.job_id}", "source": "eq.auto"},
        order="hour_start.asc,trailer_id.asc,truck_id.asc",
        page_size=1000,
        hard_cap=args.hard_cap,
    )
    print(f"Loaded {len(rows):,} hourly rows for job {args.job_id}")
    rows = [_strip_db_only_columns(row) for row in rows]
    rows = dedupe_hourly_rows(rows)
    print(f"Deduped to {len(rows):,} unique hourly rows")

    apply_billable_candidate_rules(
        rows,
        min_pair_hours=args.billable_min_pair_hours,
        min_pair_days=args.billable_min_pair_days,
        min_confidence=args.billable_min_confidence,
    )
    daily_rows, weekly_rows = summarize_rows(rows, job_id=args.job_id)
    print(f"Final summaries: {len(daily_rows):,} daily rows, {len(weekly_rows):,} weekly rows")

    secrets = _load_secrets()
    client = SupabaseClient(
        (secrets.get("SUPABASE_URL") or "").rstrip("/"),
        secrets.get("SUPABASE_SERVICE_KEY") or secrets.get("SUPABASE_KEY") or "",
        batch_size=args.batch_size,
    )

    start = _parse_date_or_datetime(str(job.get("range_start")), is_end=False)
    end = _parse_date_or_datetime(str(job.get("range_end")), is_end=True)
    print("Deleting stale daily/weekly summaries in job range...")
    client.delete_date_range("asset_pair_daily_summary", "service_date", start.date(), end.date())
    client.delete_date_range("asset_pair_weekly_review", "week_start", _week_start(start.date()), _week_start(end.date()))

    print("Updating hourly billable flags...")
    client.upsert("asset_pair_hourly_evidence", rows, on_conflict="hour_start,truck_id,trailer_id,source")
    print("Writing daily summaries...")
    client.upsert("asset_pair_daily_summary", daily_rows, on_conflict="service_date,truck_id,trailer_id,source")
    print("Writing weekly reviews...")
    client.upsert("asset_pair_weekly_review", weekly_rows, on_conflict="week_start,truck_id,trailer_id,source")
    client.finish_job(
        args.job_id,
        status="complete",
        history_rows=int(job.get("history_rows") or 0),
        usable_points=int(job.get("usable_points") or 0),
        hourly_rows=len(rows),
        daily_rows=len(daily_rows),
        weekly_rows=len(weekly_rows),
        message="Dense timestamp evidence backfill finalized from existing hourly rows.",
    )
    print("Finalize complete.")


def _load_job(rest: SupabaseRestClient, job_id: str) -> dict[str, Any]:
    rows = rest.select("gps_pairing_job_runs", filters={"job_id": f"eq.{job_id}"}, limit=1)
    if not rows:
        raise SystemExit(f"Job not found: {job_id}")
    return rows[0]


def _strip_db_only_columns(row: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(row)
    cleaned.pop("id", None)
    return cleaned


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize an existing hourly evidence job.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--billable-min-pair-hours", type=int, default=2)
    parser.add_argument("--billable-min-pair-days", type=int, default=2)
    parser.add_argument("--billable-min-confidence", type=float, default=0.55)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--hard-cap", type=int, default=100000)
    return parser.parse_args()


if __name__ == "__main__":
    main()
