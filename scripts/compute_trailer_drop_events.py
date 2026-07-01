#!/usr/bin/env python3
"""Compute operational dropped-trailer events from asset_hour_tracks.

Default rule: a trailer is considered dropped after 12+ hours stationary outside
excluded yards (California Yard by default). This is intentionally separate from
billable hours.

Recommended flow after applying migration 0022:
    python scripts/build_asset_hour_tracks.py --incremental --overlap-hours 72
    python scripts/compute_trailer_drop_events.py --incremental --overlap-hours 96
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.compute_pair_hourly_evidence import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    SupabaseClient,
    _load_secrets,
    _parse_date_or_datetime,
    haversine_miles,
)

DROP_JOB_TYPE = "trailer_drop_events"
DEFAULT_EXCLUDED_YARDS = ("California Yard",)


def main() -> None:
    args = _parse_args()
    secrets = _load_secrets()
    supabase_url = (secrets.get("SUPABASE_URL") or "").rstrip("/")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY") or secrets.get("SUPABASE_KEY") or ""
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required.")

    client = SupabaseClient(supabase_url, supabase_key, batch_size=args.batch_size)
    start, end = _resolve_window(args, client)
    job_id = f"drops-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    excluded_yards = {str(y).strip() for y in args.excluded_yard if str(y).strip()}

    print(
        f"Trailer drop event job {job_id}\n"
        f"Window: {start.isoformat()} -> {end.isoformat()}\n"
        f"Minimum idle: {args.min_idle_hours:.1f}h; radius: {args.stationary_radius_miles:.2f} mi\n"
        f"Excluded yards: {', '.join(sorted(excluded_yards)) or 'none'}"
    )

    tracks = _load_trailer_tracks(client, start, end)
    print(f"Loaded {len(tracks):,} trailer-hour tracks")
    events: list[dict[str, Any]] = []
    for trailer_id, rows in _group_by_trailer(tracks).items():
        events.extend(_events_for_trailer(
            client,
            trailer_id,
            rows,
            range_end=end,
            job_id=job_id,
            min_idle_hours=float(args.min_idle_hours),
            stationary_radius_miles=float(args.stationary_radius_miles),
            max_gap_hours=float(args.max_gap_hours),
            dropper_lookback_hours=float(args.dropper_lookback_hours),
            pickup_lookahead_hours=float(args.pickup_lookahead_hours),
            excluded_yards=excluded_yards,
        ))

    print(f"Computed {len(events):,} drop event(s)")
    if events and args.dry_run:
        for row in events[:20]:
            print(row)
    elif not args.dry_run:
        _delete_events_range(client, start, end)
        client.upsert("trailer_drop_events", events, on_conflict="event_id")
        client.upsert_compute_state(DROP_JOB_TYPE, job_id=job_id, start=start, end=end, metadata={"rows": len(events)})
    print("Done.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute dropped-trailer custody events from asset_hour_tracks.")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start", help="UTC date/datetime start. Use with --end.")
    parser.add_argument("--end", help="UTC date/datetime end. Use with --start.")
    parser.add_argument("--incremental", action="store_true", help="Use gps_compute_state with an overlap window.")
    parser.add_argument("--overlap-hours", type=int, default=96, help="Overlap window; use longer than idle threshold so open drops are rechecked.")
    parser.add_argument("--min-idle-hours", type=float, default=12.0, help="Minimum stationary duration to mark a drop.")
    parser.add_argument("--stationary-radius-miles", type=float, default=0.20, help="Max cluster radius for a stationary drop location.")
    parser.add_argument("--max-gap-hours", type=float, default=3.0, help="Max gap between trailer-hour rows before splitting stop segments.")
    parser.add_argument("--dropper-lookback-hours", type=float, default=12.0)
    parser.add_argument("--pickup-lookahead-hours", type=float, default=12.0)
    parser.add_argument("--excluded-yard", action="append", default=list(DEFAULT_EXCLUDED_YARDS), help="Yard name to suppress, repeatable. Default: California Yard.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
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
        last_end = client.load_compute_state_end(DROP_JOB_TYPE) if args.incremental else None
        if last_end:
            start = last_end - timedelta(hours=max(1, int(args.overlap_hours)))
        else:
            start = end - timedelta(days=max(1, int(args.days)))
    if end < start:
        start, end = end, start
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _load_trailer_tracks(client: SupabaseClient, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return client_select_all(
        client,
        "asset_hour_tracks",
        select=(
            "hour_start,service_date,asset_id,division,provider,ping_count,first_ping,last_ping,"
            "centroid_lat,centroid_lon,miles_traveled,moving,avg_speed,max_speed,yard_name,address"
        ),
        filters={
            "asset_type": "eq.trailer",
            "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
        },
        order="asset_id.asc,hour_start.asc",
        hard_cap=500000,
    )


def client_select_all(
    client: SupabaseClient,
    table: str,
    *,
    select: str,
    filters: dict[str, Any],
    order: str,
    hard_cap: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while offset < hard_cap:
        response = requests.get(
            f"{client.url}/rest/v1/{table}",
            headers=client.headers,
            params={"select": select, **filters, "order": order, "limit": page_size, "offset": offset},
            timeout=90,
        )
        if not response.ok:
            raise RuntimeError(f"load {table} failed: HTTP {response.status_code} {response.text[:500]}")
        page = response.json()
        if not isinstance(page, list):
            break
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def _group_by_trailer(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        trailer_id = str(row.get("asset_id") or "").strip()
        if trailer_id:
            grouped[trailer_id].append(row)
    return grouped


def _events_for_trailer(
    client: SupabaseClient,
    trailer_id: str,
    rows: list[dict[str, Any]],
    *,
    range_end: datetime,
    job_id: str,
    min_idle_hours: float,
    stationary_radius_miles: float,
    max_gap_hours: float,
    dropper_lookback_hours: float,
    pickup_lookahead_hours: float,
    excluded_yards: set[str],
) -> list[dict[str, Any]]:
    rows = [r for r in rows if _coords(r) is not None and _parse_dt(r.get("first_ping"))]
    rows.sort(key=lambda r: _parse_dt(r.get("hour_start")) or datetime.min.replace(tzinfo=timezone.utc))
    events: list[dict[str, Any]] = []
    segment: list[dict[str, Any]] = []

    def close_segment(ended_at: datetime | None) -> None:
        nonlocal segment
        if not segment:
            return
        event = _segment_to_event(
            client,
            trailer_id,
            segment,
            ended_at=ended_at,
            range_end=range_end,
            job_id=job_id,
            min_idle_hours=min_idle_hours,
            stationary_radius_miles=stationary_radius_miles,
            dropper_lookback_hours=dropper_lookback_hours,
            pickup_lookahead_hours=pickup_lookahead_hours,
            excluded_yards=excluded_yards,
        )
        if event:
            events.append(event)
        segment = []

    for row in rows:
        row_start = _parse_dt(row.get("first_ping"))
        moving = bool(row.get("moving")) or float(row.get("miles_traveled") or 0) >= 0.5
        if not row_start:
            continue
        if moving:
            close_segment(row_start)
            continue
        if not segment:
            segment = [row]
            continue
        previous_end = _parse_dt(segment[-1].get("last_ping")) or _parse_dt(segment[-1].get("hour_start"))
        if previous_end and (row_start - previous_end).total_seconds() / 3600.0 > max_gap_hours:
            close_segment(row_start)
            segment = [row]
            continue
        anchor = _segment_anchor(segment)
        coords = _coords(row)
        if anchor and coords and haversine_miles(anchor[0], anchor[1], coords[0], coords[1]) <= stationary_radius_miles:
            segment.append(row)
        else:
            close_segment(row_start)
            segment = [row]
    close_segment(None)
    return events


def _segment_to_event(
    client: SupabaseClient,
    trailer_id: str,
    segment: list[dict[str, Any]],
    *,
    ended_at: datetime | None,
    range_end: datetime,
    job_id: str,
    min_idle_hours: float,
    stationary_radius_miles: float,
    dropper_lookback_hours: float,
    pickup_lookahead_hours: float,
    excluded_yards: set[str],
) -> dict[str, Any] | None:
    first_ping = min((_parse_dt(r.get("first_ping")) for r in segment if _parse_dt(r.get("first_ping"))), default=None)
    last_ping = max((_parse_dt(r.get("last_ping")) for r in segment if _parse_dt(r.get("last_ping"))), default=None)
    if not first_ping or not last_ping:
        return None
    idle_hours = (last_ping - first_ping).total_seconds() / 3600.0
    if idle_hours < min_idle_hours:
        return None

    lat, lon = _segment_anchor(segment) or (None, None)
    yard = _mode([str(r.get("yard_name") or "") for r in segment])
    is_excluded = bool(yard and yard in excluded_yards)
    if is_excluded:
        return None

    dropper = _best_pair_for_trailer(
        client,
        trailer_id,
        first_ping - timedelta(hours=dropper_lookback_hours),
        first_ping + timedelta(hours=1),
        order="hour_start.desc",
    )
    pickup = None
    if ended_at:
        pickup = _best_pair_for_trailer(
            client,
            trailer_id,
            ended_at - timedelta(hours=1),
            ended_at + timedelta(hours=pickup_lookahead_hours),
            order="hour_start.asc",
        )

    status = "active_drop" if ended_at is None else "picked_up"
    if yard:
        status = "yard_drop"
    if not dropper:
        status = "unknown_dropper"

    event_id = _event_id(trailer_id, first_ping)
    addresses = [str(r.get("address") or "") for r in segment if r.get("address")]
    ping_count = sum(int(r.get("ping_count") or 0) for r in segment)
    return {
        "event_id": event_id,
        "trailer_id": trailer_id,
        "status": status,
        "drop_started_at": first_ping.isoformat(),
        "drop_ended_at": ended_at.isoformat() if ended_at else None,
        "idle_hours": round(idle_hours, 2),
        "lat": round(float(lat), 6) if lat is not None else None,
        "lon": round(float(lon), 6) if lon is not None else None,
        "address": addresses[-1] if addresses else "",
        "yard_name": yard,
        "is_excluded_yard": is_excluded,
        "dropped_by_truck_id": str(dropper.get("truck_id") or "") if dropper else "",
        "dropped_by_confidence": round(float(dropper.get("confidence") or 0), 3) if dropper else 0,
        "picked_up_by_truck_id": str(pickup.get("truck_id") or "") if pickup else "",
        "pickup_confidence": round(float(pickup.get("confidence") or 0), 3) if pickup else 0,
        "last_pair_hour": dropper.get("hour_start") if dropper else None,
        "first_stationary_ping": first_ping.isoformat(),
        "last_stationary_ping": last_ping.isoformat(),
        "stationary_radius_miles": stationary_radius_miles,
        "ping_count": ping_count,
        "evidence": {
            "segment_hours": len(segment),
            "ended_at": ended_at.isoformat() if ended_at else "",
            "range_end": range_end.isoformat(),
            "rule": f"idle >= {min_idle_hours}h outside excluded yards",
        },
        "source_job_id": job_id,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _best_pair_for_trailer(client: SupabaseClient, trailer_id: str, start: datetime, end: datetime, *, order: str) -> dict[str, Any] | None:
    rows = client_select_all(
        client,
        "asset_pair_hourly_evidence",
        select="hour_start,truck_id,trailer_id,status,confidence,best_distance_miles",
        filters={
            "trailer_id": f"eq.{trailer_id}",
            "source": "eq.auto",
            "status": "eq.paired",
            "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
        },
        order=order,
        hard_cap=200,
    )
    if not rows:
        return None
    rows.sort(key=lambda r: (float(r.get("confidence") or 0), -float(r.get("best_distance_miles") or 999)), reverse=True)
    return rows[0]


def _delete_events_range(client: SupabaseClient, start: datetime, end: datetime) -> None:
    response = requests.delete(
        f"{client.url}/rest/v1/trailer_drop_events",
        headers=client.headers,
        params={"drop_started_at": f"gte.{start.isoformat()}", "and": f"(drop_started_at.lte.{end.isoformat()})"},
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(f"delete trailer_drop_events failed: HTTP {response.status_code} {response.text[:500]}")


def _segment_anchor(segment: list[dict[str, Any]]) -> tuple[float, float] | None:
    coords = [_coords(r) for r in segment]
    coords = [c for c in coords if c]
    if not coords:
        return None
    return (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))


def _coords(row: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat = float(row.get("centroid_lat"))
        lon = float(row.get("centroid_lon"))
    except (TypeError, ValueError):
        return None
    if lat == 0 and lon == 0:
        return None
    return lat, lon


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mode(values: list[str]) -> str:
    clean = [v for v in values if v]
    if not clean:
        return ""
    return Counter(clean).most_common(1)[0][0]


def _event_id(trailer_id: str, started_at: datetime) -> str:
    digest = hashlib.sha1(f"{trailer_id}|{started_at.isoformat()}".encode("utf-8")).hexdigest()[:16]
    return f"drop-{digest}"


if __name__ == "__main__":
    main()
