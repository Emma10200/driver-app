#!/usr/bin/env python3
"""Build compact per-asset/per-hour GPS tracks from assets_history.

This is the performance foundation for incremental pairing/drop-event jobs. It
rolls raw GPS pings into one row per asset/hour so later jobs can avoid scanning
millions of raw assets_history rows.

Examples:
    python scripts/build_asset_hour_tracks.py --days 7
    python scripts/build_asset_hour_tracks.py --incremental --overlap-hours 72
    python scripts/build_asset_hour_tracks.py --start 2026-06-01 --end 2026-07-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.compute_pair_hourly_evidence import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_HARD_CAP,
    MATCHING_SOURCES,
    MOVING_SPEED_THRESHOLD,
    SupabaseClient,
    _load_secrets,
    _parse_date_or_datetime,
    _row_to_point,
    build_asset_tracks,
    enrich_point_motion,
    haversine_miles,
    yard_name,
)

TRACK_JOB_TYPE = "asset_hour_tracks"


def main() -> None:
    args = _parse_args()
    secrets = _load_secrets()
    supabase_url = (secrets.get("SUPABASE_URL") or "").rstrip("/")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY") or secrets.get("SUPABASE_KEY") or ""
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required.")

    client = SupabaseClient(supabase_url, supabase_key, batch_size=args.batch_size)
    start, end = _resolve_window(args, client)
    job_id = f"tracks-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"

    print(
        f"Asset-hour track build {job_id}\n"
        f"Window: {start.isoformat()} -> {end.isoformat()}\n"
        f"Supabase: {supabase_url}"
    )

    total_rows = 0
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=max(1, args.chunk_days)), end)
        print(f"\n--- Track chunk: {chunk_start.isoformat()} -> {chunk_end.isoformat()} ---")
        raw_rows = _load_history(client, chunk_start, chunk_end, hard_cap=args.hard_cap)
        points = [_row_to_point(row) for row in raw_rows]
        points = [point for point in points if point is not None]
        points = enrich_point_motion(points)
        tracks = build_asset_tracks(points)
        out_rows = [_track_to_row(track, job_id=job_id) for track in tracks]
        print(f"Loaded {len(raw_rows):,} raw rows -> {len(out_rows):,} asset-hour rows")
        if not args.dry_run:
            _delete_track_range(client, chunk_start, chunk_end)
            client.upsert("asset_hour_tracks", out_rows, on_conflict="hour_start,asset_type,asset_id")
        total_rows += len(out_rows)
        chunk_start = chunk_end

    if not args.dry_run:
        _upsert_state(client, TRACK_JOB_TYPE, job_id, start, end, {"rows": total_rows})
    print(f"Done. {total_rows:,} asset-hour rows {'would be written' if args.dry_run else 'written'}.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build compact asset_hour_tracks rows from assets_history.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start", help="UTC date/datetime start. Use with --end.")
    parser.add_argument("--end", help="UTC date/datetime end. Use with --start.")
    parser.add_argument("--incremental", action="store_true", help="Use gps_compute_state with an overlap window.")
    parser.add_argument("--overlap-hours", type=int, default=72, help="Safety overlap for late GPS/backfills.")
    parser.add_argument("--chunk-days", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--hard-cap", type=int, default=DEFAULT_HARD_CAP)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _resolve_window(args: argparse.Namespace, client: SupabaseClient) -> tuple[datetime, datetime]:
    if args.start or args.end:
        if not args.start or not args.end:
            raise SystemExit("Use both --start and --end, or neither.")
        start = _parse_date_or_datetime(args.start, is_end=False)
        end = _parse_date_or_datetime(args.end, is_end=True)
    else:
        end = datetime.now(timezone.utc)
        if args.incremental:
            last_end = _load_state_end(client, TRACK_JOB_TYPE)
            if last_end:
                start = last_end - timedelta(hours=max(1, int(args.overlap_hours)))
            else:
                start = end - timedelta(days=max(1, int(args.days)))
        else:
            start = end - timedelta(days=max(1, int(args.days)))
    if end < start:
        start, end = end, start
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _load_history(client: SupabaseClient, start: datetime, end: datetime, *, hard_cap: int) -> list[dict[str, Any]]:
    source_filter = ",".join(f"source.eq.{s}" for s in MATCHING_SOURCES)
    params_base = {
        "select": "asset_type,asset_id,division,lat,lon,speed,heading_deg,provider,recorded_at,address,source",
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
        "or": f"({source_filter})",
        "order": "recorded_at.asc",
    }
    out: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while offset < hard_cap:
        response = requests.get(
            f"{client.url}/rest/v1/assets_history",
            headers=client.headers,
            params={**params_base, "limit": page_size, "offset": offset},
            timeout=90,
        )
        if not response.ok:
            raise RuntimeError(f"assets_history load failed: HTTP {response.status_code} {response.text[:500]}")
        page = response.json()
        if not isinstance(page, list):
            break
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        print(f"  loaded {len(out):,} raw history rows...", flush=True)
    if offset >= hard_cap:
        print(f"WARNING: reached hard cap {hard_cap:,}; output may be incomplete.")
    return out


def _track_to_row(track: Any, *, job_id: str) -> dict[str, Any]:
    points = list(track.points)
    speeds = [float(p.speed) for p in points if p.speed is not None]
    headings = [float(p.heading_deg) for p in points if p.heading_deg is not None]
    miles = 0.0
    for idx in range(1, len(points)):
        miles += haversine_miles(points[idx - 1].lat, points[idx - 1].lon, points[idx].lat, points[idx].lon)
    moving = miles >= 0.5 or any(speed >= MOVING_SPEED_THRESHOLD for speed in speeds)
    source_counts = Counter(str(getattr(p, "source", "") or "") for p in points)
    # Point does not currently retain source; keep provider/mode as the practical audit fields.
    sample = [
        {
            "ts": p.ts.isoformat(),
            "lat": round(p.lat, 6),
            "lon": round(p.lon, 6),
            "speed": p.speed,
            "heading_deg": p.heading_deg,
        }
        for p in _sample_points(points, max_points=12)
    ]
    return {
        "hour_start": track.hour_start.isoformat(),
        "service_date": track.hour_start.date().isoformat(),
        "asset_type": track.asset_type,
        "asset_id": track.asset_id,
        "division": track.division,
        "provider": track.provider,
        "source": source_counts.most_common(1)[0][0] if source_counts else "",
        "ping_count": len(points),
        "first_ping": track.first_ping.isoformat(),
        "last_ping": track.last_ping.isoformat(),
        "centroid_lat": round(track.lat, 6),
        "centroid_lon": round(track.lon, 6),
        "min_lat": round(min(p.lat for p in points), 6),
        "max_lat": round(max(p.lat for p in points), 6),
        "min_lon": round(min(p.lon for p in points), 6),
        "max_lon": round(max(p.lon for p in points), 6),
        "miles_traveled": round(miles, 3),
        "moving": moving,
        "avg_speed": round(sum(speeds) / len(speeds), 3) if speeds else None,
        "max_speed": round(max(speeds), 3) if speeds else None,
        "heading_deg": round(sum(headings) / len(headings), 3) if headings else None,
        "yard_name": yard_name(track.lat, track.lon),
        "address": track.address,
        "sample_points": sample,
        "source_row_count": len(points),
        "last_source_recorded_at": track.last_ping.isoformat(),
        "job_id": job_id,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _sample_points(points: list[Any], *, max_points: int) -> list[Any]:
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / float(max_points - 1)
    indexes = sorted({round(i * step) for i in range(max_points)})
    return [points[int(i)] for i in indexes]


def _delete_track_range(client: SupabaseClient, start: datetime, end: datetime) -> None:
    response = requests.delete(
        f"{client.url}/rest/v1/asset_hour_tracks",
        headers=client.headers,
        params={"hour_start": f"gte.{start.isoformat()}", "and": f"(hour_start.lte.{end.isoformat()})"},
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(f"delete asset_hour_tracks failed: HTTP {response.status_code} {response.text[:500]}")


def _load_state_end(client: SupabaseClient, job_type: str) -> datetime | None:
    response = requests.get(
        f"{client.url}/rest/v1/gps_compute_state",
        headers=client.headers,
        params={"select": "last_range_end", "job_type": f"eq.{job_type}", "limit": 1},
        timeout=30,
    )
    if not response.ok:
        return None
    rows = response.json()
    if not rows:
        return None
    value = rows[0].get("last_range_end")
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _upsert_state(client: SupabaseClient, job_type: str, job_id: str, start: datetime, end: datetime, metadata: dict[str, Any]) -> None:
    client.upsert("gps_compute_state", [{
        "job_type": job_type,
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "last_range_start": start.isoformat(),
        "last_range_end": end.isoformat(),
        "last_job_id": job_id,
        "metadata": metadata,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }], on_conflict="job_type")


if __name__ == "__main__":
    main()
