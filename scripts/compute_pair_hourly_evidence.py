#!/usr/bin/env python3
"""Compute all-fleet hourly truck↔trailer evidence and write to Supabase.

This is the production/backfill version of ``pair_hourly_history.py``. It scans
raw ``assets_history`` for a date window, finds likely truck↔trailer pair hours,
then upserts:

* asset_pair_hourly_evidence
* asset_pair_daily_summary
* asset_pair_weekly_review
* gps_pairing_job_runs

It stores only candidate evidence rows, not every possible truck×trailer×hour
combination. This keeps storage realistic while preserving what is needed for
weekly trailer-usage review.

Example:
    python scripts/compute_pair_hourly_evidence.py --days 60
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import uuid
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import polars as pl
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Polars is required. Run `python -m pip install -r requirements.txt`.") from exc


ROOT = Path(__file__).resolve().parent.parent
PAGE_SIZE = 1000
DEFAULT_BATCH_SIZE = 500
DEFAULT_HARD_CAP = 5_000_000
DEFAULT_CHUNK_DAYS = 1
EARTH_RADIUS_MILES = 3958.8
YARD_GEOFENCES = {
    "California Yard": (34.09686, -117.47642, 0.25),
    "Illinois Yard": (41.896873, -87.86982, 0.25),
}
# Suppression radius: if BOTH truck and trailer centroids are within this radius
# of the same yard, suppress matching entirely (consider them parked).
YARD_SUPPRESSION_RADIUS = {
    "California Yard": 3.0,
    "Illinois Yard": 0.5,
}
# Accepted historical GPS evidence sources for pairing.
# Includes dense backfills plus blank-source historical imports that contain
# useful older 888/dispatch-board history for trucks like 129. Explicitly do
# NOT include sparse live publisher snapshots (truck_publish/trailer_publish).
MATCHING_SOURCES = ("gpstab_backfill", "anytrek_backfill", "track888_backfill", "eroad_backfill", "")
MOVING_SPEED_THRESHOLD = 5.0
STATIONARY_SPEED_THRESHOLD = 2.0
MOTION_DERIVE_MAX_GAP_MINUTES = 30.0
MOTION_MIN_SEGMENT_MILES = 0.02
YARD_PROXIMITY_MULTIPLIER = 4.0
YARD_STRICT_GENERIC_MATCHES = 20
YARD_STRICT_CALIFORNIA_MATCHES = 30
YARD_STRICT_MIN_EVIDENCE_RATIO = 0.80
YARD_STRICT_MIN_MOVEMENT_MATCHES = 2


@dataclass(frozen=True)
class AssetTrack:
    hour_start: datetime
    asset_type: str
    asset_id: str
    points: tuple[Point, ...]
    first_ping: datetime
    last_ping: datetime
    lat: float
    lon: float
    provider: str
    division: str
    address: str

    @property
    def pings(self) -> int:
        return len(self.points)


@dataclass(frozen=True)
class TimestampMatch:
    distance_miles: float
    ping_gap_minutes: float
    truck_lat: float
    truck_lon: float
    trailer_lat: float
    trailer_lon: float
    truck_ts: datetime
    trailer_ts: datetime
    truck_speed: float | None = None
    trailer_speed: float | None = None
    truck_heading: float | None = None
    trailer_heading: float | None = None
    movement_score: float = 0.5
    movement_mismatch: bool = False
    movement_compatible: bool = False


def main() -> None:
    args = _parse_args()
    start, end = _resolve_window(args)
    job_id = f"hourly-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    secrets = _load_secrets()
    supabase_url = (secrets.get("SUPABASE_URL") or "").rstrip("/")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY") or secrets.get("SUPABASE_KEY") or ""
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required in .env, .streamlit/secrets.toml, or environment.")

    client = SupabaseClient(supabase_url, supabase_key, batch_size=args.batch_size)
    print(
        f"All-fleet hourly evidence job {job_id}\n"
        f"Window: {start.isoformat()} → {end.isoformat()}\n"
        f"Max paired distance: {args.max_distance:.2f} mi; near distance stored: {args.near_distance:.2f} mi; "
        f"max ping gap: {args.max_ping_gap_minutes:.0f} min\n"
        f"Supabase: {supabase_url}"
    )

    if not args.dry_run:
        client.insert_job(job_id, start, end, args)

    try:
        if args.dry_run or args.chunk_days <= 0:
            hourly_rows, daily_rows, weekly_rows, trailer_activity_rows, stats = compute_window(
                client,
                start,
                end,
                args=args,
                job_id=job_id,
                apply_billable=True,
            )
            print(
                f"Computed {len(hourly_rows):,} hourly rows, {len(daily_rows):,} daily rows, "
                f"{len(weekly_rows):,} weekly rows, {len(trailer_activity_rows):,} trailer activity rows "
                f"from {stats['history_rows']:,} raw rows."
            )
            if args.dry_run:
                _print_preview(hourly_rows, daily_rows, weekly_rows)
                _print_trailer_activity_preview(trailer_activity_rows)
                return

            print("Deleting existing auto rows in range...")
            delete_derived_range(client, start, end)
            write_derived_rows(client, hourly_rows, daily_rows, weekly_rows)
            print(f"Writing trailer activity summary rows: {len(trailer_activity_rows):,}")
            client.upsert("trailer_activity_summary", trailer_activity_rows, on_conflict="service_date,trailer_id")
        else:
            print("Deleting existing auto evidence rows for selected range before chunked rebuild...")
            delete_derived_range(client, start, end)
            all_hourly_rows: list[dict[str, Any]] = []
            stats = {"history_rows": 0, "usable_points": 0, "hourly_rows": 0, "daily_rows": 0, "weekly_rows": 0}
            chunk_start = start
            chunk_index = 0
            while chunk_start < end:
                chunk_end = min(chunk_start + timedelta(days=max(1, int(args.chunk_days))), end)
                chunk_index += 1
                print(f"\n--- Chunk {chunk_index}: {chunk_start.isoformat()} → {chunk_end.isoformat()} ---")
                hourly_rows, daily_rows, _weekly_unused, trailer_activity_rows, chunk_stats = compute_window(
                    client,
                    chunk_start,
                    chunk_end,
                    args=args,
                    job_id=job_id,
                    apply_billable=False,
                )
                stats["history_rows"] += chunk_stats["history_rows"]
                stats["usable_points"] += chunk_stats["usable_points"]
                stats["hourly_rows"] += len(hourly_rows)
                all_hourly_rows.extend(hourly_rows)
                print(f"Writing chunk {chunk_index}: {len(hourly_rows):,} hourly rows")
                client.upsert("asset_pair_hourly_evidence", hourly_rows, on_conflict="hour_start,truck_id,trailer_id,source")
                if trailer_activity_rows:
                    client.upsert("trailer_activity_summary", trailer_activity_rows, on_conflict="service_date,trailer_id")
                chunk_start = chunk_end

            print("Computing final daily/weekly summaries from all hourly rows...")
            all_hourly_rows = dedupe_hourly_rows(all_hourly_rows)
            apply_billable_candidate_rules(
                all_hourly_rows,
                min_pair_hours=args.billable_min_pair_hours,
                min_pair_days=args.billable_min_pair_days,
                min_confidence=args.billable_min_confidence,
            )
            print("Updating hourly rows with final billable-candidate flags...")
            client.upsert("asset_pair_hourly_evidence", all_hourly_rows, on_conflict="hour_start,truck_id,trailer_id,source")
            daily_rows, weekly_rows = summarize_rows(all_hourly_rows, job_id=job_id)
            stats["daily_rows"] = len(daily_rows)
            stats["weekly_rows"] = len(weekly_rows)
            print(f"Writing final daily summary rows: {len(daily_rows):,}")
            client.upsert("asset_pair_daily_summary", daily_rows, on_conflict="service_date,truck_id,trailer_id,source")
            print(f"Writing final weekly review rows: {len(weekly_rows):,}")
            client.upsert("asset_pair_weekly_review", weekly_rows, on_conflict="week_start,truck_id,trailer_id,source")

        client.finish_job(
            job_id,
            status="complete",
            history_rows=stats["history_rows"],
            usable_points=stats["usable_points"],
            hourly_rows=stats["hourly_rows"],
            daily_rows=stats["daily_rows"],
            weekly_rows=stats["weekly_rows"],
            message="Dense timestamp evidence backfill completed.",
        )
        print(f"Done. Job {job_id} complete.")
    except Exception as exc:
        if not args.dry_run:
            try:
                client.finish_job(job_id, status="failed", error=str(exc)[:4000])
            except Exception:
                pass
        raise


@dataclass(frozen=True)
class Point:
    asset_type: str
    asset_id: str
    ts: datetime
    lat: float
    lon: float
    speed: float | None
    heading_deg: float | None
    provider: str
    division: str
    address: str


def compute_window(
    client: SupabaseClient,
    start: datetime,
    end: datetime,
    *,
    args: argparse.Namespace,
    job_id: str,
    apply_billable: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    rows = client.load_history(start, end, hard_cap=args.hard_cap)
    points = [_row_to_point(row) for row in rows]
    points = [point for point in points if point is not None]
    points = enrich_point_motion(points)
    print(f"Loaded {len(rows):,} raw rows; usable coordinate rows: {len(points):,}")
    if not points:
        return [], [], [], {"history_rows": len(rows), "usable_points": 0, "hourly_rows": 0, "daily_rows": 0, "weekly_rows": 0}

    asset_tracks = build_asset_tracks(points)
    print(f"Built {len(asset_tracks):,} asset-hour raw tracks")

    hourly_rows = compute_hourly_evidence(
        asset_tracks,
        max_distance_miles=args.max_distance,
        near_distance_miles=args.near_distance,
        max_ping_gap_minutes=args.max_ping_gap_minutes,
        include_same_yard_as_paired=args.include_same_yard,
        min_matches=args.min_matches,
        min_match_ratio=args.min_match_ratio,
        max_candidates_per_trailer_hour=args.max_candidates_per_trailer_hour,
        job_id=job_id,
    )
    print(f"Computed {len(hourly_rows):,} dense timestamp hourly candidate rows")
    if apply_billable:
        apply_billable_candidate_rules(
            hourly_rows,
            min_pair_hours=args.billable_min_pair_hours,
            min_pair_days=args.billable_min_pair_days,
            min_confidence=args.billable_min_confidence,
        )
    daily_rows, weekly_rows = summarize_rows(hourly_rows, job_id=job_id)
    trailer_activity_rows = compute_trailer_activity(asset_tracks, hourly_rows, job_id=job_id)
    return hourly_rows, daily_rows, weekly_rows, trailer_activity_rows, {
        "history_rows": len(rows),
        "usable_points": len(points),
        "hourly_rows": len(hourly_rows),
        "daily_rows": len(daily_rows),
        "weekly_rows": len(weekly_rows),
    }


def delete_derived_range(client: SupabaseClient, start: datetime, end: datetime) -> None:
    client.delete_range("asset_pair_hourly_evidence", "hour_start", start, end)
    client.delete_date_range("asset_pair_daily_summary", "service_date", start.date(), end.date())
    client.delete_date_range("asset_pair_weekly_review", "week_start", _week_start(start.date()), _week_start(end.date()))


def write_derived_rows(
    client: SupabaseClient,
    hourly_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    weekly_rows: list[dict[str, Any]],
) -> None:
    print("Writing hourly evidence...")
    client.upsert("asset_pair_hourly_evidence", hourly_rows, on_conflict="hour_start,truck_id,trailer_id,source")
    print("Writing daily summaries...")
    client.upsert("asset_pair_daily_summary", daily_rows, on_conflict="service_date,truck_id,trailer_id,source")
    print("Writing weekly review rows...")
    client.upsert("asset_pair_weekly_review", weekly_rows, on_conflict="week_start,truck_id,trailer_id,source")


def build_asset_tracks(points: list[Point]) -> list[AssetTrack]:
    grouped: dict[tuple[datetime, str, str], list[Point]] = defaultdict(list)
    seen: set[tuple[str, str, datetime, float, float]] = set()
    for point in points:
        if point.asset_type not in ("truck", "trailer"):
            continue
        key = (point.asset_type, point.asset_id, point.ts, round(point.lat, 6), round(point.lon, 6))
        if key in seen:
            continue
        seen.add(key)
        grouped[(_floor_hour(point.ts), point.asset_type, point.asset_id)].append(point)

    out: list[AssetTrack] = []
    for (hour_start, asset_type, asset_id), group in grouped.items():
        group.sort(key=lambda p: p.ts)
        lat = sum(p.lat for p in group) / len(group)
        lon = sum(p.lon for p in group) / len(group)
        out.append(
            AssetTrack(
                hour_start=hour_start,
                asset_type=asset_type,
                asset_id=asset_id,
                points=tuple(group),
                first_ping=group[0].ts,
                last_ping=group[-1].ts,
                lat=lat,
                lon=lon,
                provider=_mode([p.provider for p in group]),
                division=_mode([p.division for p in group]),
                address=group[-1].address,
            )
        )
    return out


def enrich_point_motion(points: list[Point]) -> list[Point]:
    """Fill missing speed/heading from adjacent coordinates when possible.

    Provider speed/heading is preferred. For older blank-source history, derive
    movement from nearby pings on the same asset so the dense matcher can still
    reject obvious moving-vs-stationary false candidates without requiring every
    provider to expose identical fields.
    """
    by_asset: dict[tuple[str, str], list[Point]] = defaultdict(list)
    for point in points:
        by_asset[(point.asset_type, point.asset_id)].append(point)

    enriched: list[Point] = []
    for group in by_asset.values():
        group.sort(key=lambda p: p.ts)
        for idx, point in enumerate(group):
            speed = _clean_speed(point.speed)
            heading = _normalize_heading(point.heading_deg)
            derived_speed, derived_heading = _derive_motion_for_point(group, idx)
            if speed is None:
                speed = derived_speed
            if heading is None:
                heading = derived_heading
            enriched.append(Point(
                asset_type=point.asset_type,
                asset_id=point.asset_id,
                ts=point.ts,
                lat=point.lat,
                lon=point.lon,
                speed=speed,
                heading_deg=heading,
                provider=point.provider,
                division=point.division,
                address=point.address,
            ))
    enriched.sort(key=lambda p: p.ts)
    return enriched


def _derive_motion_for_point(points: list[Point], idx: int) -> tuple[float | None, float | None]:
    best: tuple[float, float, float] | None = None
    for a_idx, b_idx in ((idx - 1, idx), (idx, idx + 1)):
        if a_idx < 0 or b_idx >= len(points):
            continue
        a = points[a_idx]
        b = points[b_idx]
        seconds = abs((b.ts - a.ts).total_seconds())
        if seconds <= 0 or seconds > MOTION_DERIVE_MAX_GAP_MINUTES * 60.0:
            continue
        distance = haversine_miles(a.lat, a.lon, b.lat, b.lon)
        speed = distance / (seconds / 3600.0)
        heading = _bearing_degrees(a.lat, a.lon, b.lat, b.lon) if distance >= MOTION_MIN_SEGMENT_MILES else None
        score = seconds - (1000.0 if distance >= MOTION_MIN_SEGMENT_MILES else 0.0)
        if best is None or score < best[0]:
            best = (score, speed, heading if heading is not None else -1.0)
    if best is None:
        return None, None
    return best[1], (best[2] if best[2] >= 0 else None)


def compute_hourly_evidence(
    asset_tracks: list[AssetTrack],
    *,
    max_distance_miles: float,
    near_distance_miles: float,
    max_ping_gap_minutes: float,
    include_same_yard_as_paired: bool,
    min_matches: int,
    min_match_ratio: float,
    max_candidates_per_trailer_hour: int,
    job_id: str,
) -> list[dict[str, Any]]:
    by_hour: dict[datetime, dict[str, list[AssetTrack]]] = defaultdict(lambda: {"truck": [], "trailer": []})
    for row in asset_tracks:
        if row.asset_type in ("truck", "trailer"):
            by_hour[row.hour_start][row.asset_type].append(row)

    records: list[dict[str, Any]] = []
    computed_at = datetime.now(timezone.utc).isoformat()
    for hour_start in sorted(by_hour):
        trucks = by_hour[hour_start]["truck"]
        trailers = by_hour[hour_start]["trailer"]
        if not trucks or not trailers:
            continue

        hour_candidates: list[dict[str, Any]] = []
        for trailer in trailers:
            candidates: list[dict[str, Any]] = []
            for truck in trucks:
                # Hard suppression: if both truck and trailer are within a yard's
                # suppression radius, skip entirely - they are parked.
                if _both_in_yard_suppression_zone(truck.lat, truck.lon, trailer.lat, trailer.lon):
                    continue

                matches = _timestamp_matches(truck, trailer, max_ping_gap_minutes=max_ping_gap_minutes)
                matches = [m for m in matches if not m.movement_mismatch]
                if not matches:
                    continue

                distances = [m.distance_miles for m in matches]
                near_matches = [m for m in matches if m.distance_miles <= near_distance_miles]
                if not near_matches:
                    continue

                close_matches = [m for m in matches if m.distance_miles <= max_distance_miles]
                best_match = min(matches, key=lambda m: (m.distance_miles, m.ping_gap_minutes))
                best_near_match = min(near_matches, key=lambda m: (m.distance_miles, m.ping_gap_minutes))
                matched_count = len(matches)
                sparse_pings = min(truck.pings, trailer.pings)
                close_count = len(close_matches)
                close_ratio = close_count / max(1, matched_count)
                evidence_ratio = close_count / max(1, sparse_pings)
                movement_scores = [m.movement_score for m in close_matches]
                movement_score = sum(movement_scores) / len(movement_scores) if movement_scores else 0.5
                movement_compatible_count = sum(1 for m in close_matches if m.movement_compatible)
                median_distance = _median(distances)
                median_gap = _median([m.ping_gap_minutes for m in matches])

                truck_yard = yard_name(best_near_match.truck_lat, best_near_match.truck_lon)
                trailer_yard = yard_name(best_near_match.trailer_lat, best_near_match.trailer_lon)
                same_yard = bool(truck_yard and truck_yard == trailer_yard)
                yard_touch = bool(truck_yard or trailer_yard)
                yard_proximity = yard_proximity_names(best_near_match.truck_lat, best_near_match.truck_lon) | yard_proximity_names(best_near_match.trailer_lat, best_near_match.trailer_lon)
                near_yard = bool(yard_touch or yard_proximity)

                if near_yard and not _passes_strict_yard_gate(
                    close_count=close_count,
                    sparse_pings=sparse_pings,
                    evidence_ratio=evidence_ratio,
                    movement_compatible_count=movement_compatible_count,
                    yard_names=yard_proximity | {truck_yard, trailer_yard},
                ):
                    # Suppress weak parking-lot proximity entirely. A truck can
                    # pass dozens of parked trailers near Fontana/Illinois yards;
                    # without dense ping-by-ping co-movement, this is noise.
                    continue

                miles_traveled = _miles_traveled_together(close_matches if close_matches else near_matches)
                paired_by_distance = bool(close_matches) and (
                    close_count >= min_matches or evidence_ratio >= min_match_ratio
                )
                movement_evidence = miles_traveled >= 0.5 or movement_compatible_count >= 1
                if paired_by_distance and not movement_evidence:
                    # A stationary-only proximity hour is useful review evidence, but it is
                    # not enough to claim an actual truck/trailer pairing. This suppresses
                    # one-off parked-near-each-other false positives such as truck 1175.
                    paired_by_distance = False

                if same_yard and not include_same_yard_as_paired:
                    status = "same_yard"
                elif yard_touch and not include_same_yard_as_paired:
                    # Yard hours are intentionally review-only. A truck entering/leaving a
                    # shared yard can pass many parked trailers, so do not promote yard
                    # proximity to a paired/billable hour unless explicitly requested.
                    status = "near"
                elif paired_by_distance:
                    status = "paired"
                else:
                    status = "near"

                paired = status == "paired"
                confidence = _dense_confidence(
                    best_distance=best_match.distance_miles,
                    median_distance=median_distance,
                    median_gap=median_gap,
                    close_ratio=close_ratio,
                    evidence_ratio=evidence_ratio,
                    close_count=close_count,
                    max_distance=max_distance_miles,
                    max_gap=max_ping_gap_minutes,
                    paired=paired,
                    same_yard=same_yard,
                    movement_score=movement_score,
                )
                candidates.append(
                    {
                        "hour_start": hour_start.isoformat(),
                        "service_date": hour_start.date().isoformat(),
                        "week_start": _week_start(hour_start.date()).isoformat(),
                        "truck_id": truck.asset_id,
                        "trailer_id": trailer.asset_id,
                        "status": status,
                        "paired_evidence": paired,
                        "billable_candidate": paired and not yard_touch and close_count >= min_matches,
                        "confidence": round(confidence, 3),
                        "best_distance_miles": round(best_match.distance_miles, 3),
                        "best_ping_gap_minutes": round(best_match.ping_gap_minutes, 2),
                        "miles_traveled": round(miles_traveled, 2),
                        "truck_pings": truck.pings,
                        "trailer_pings": trailer.pings,
                        "truck_first_ping": truck.first_ping.isoformat(),
                        "truck_last_ping": truck.last_ping.isoformat(),
                        "trailer_first_ping": trailer.first_ping.isoformat(),
                        "trailer_last_ping": trailer.last_ping.isoformat(),
                        "truck_lat": round(best_match.truck_lat, 6),
                        "truck_lon": round(best_match.truck_lon, 6),
                        "trailer_lat": round(best_match.trailer_lat, 6),
                        "trailer_lon": round(best_match.trailer_lon, 6),
                        "truck_yard": truck_yard,
                        "trailer_yard": trailer_yard,
                        "truck_provider": truck.provider,
                        "trailer_provider": trailer.provider,
                        "truck_division": truck.division,
                        "trailer_division": trailer.division,
                        "truck_address": truck.address,
                        "trailer_address": trailer.address,
                        "source": "auto",
                        "job_id": job_id,
                        "computed_at": computed_at,
                        "_sort_score": (paired, confidence, close_count, -best_match.distance_miles),
                    }
                )
            candidates.sort(key=lambda row: row["_sort_score"], reverse=True)
            for row in candidates[:max(1, max_candidates_per_trailer_hour)]:
                hour_candidates.append(row)

        # --- Exclusive matching: one truck ↔ one trailer per hour ---
        # Greedy assignment: highest-scoring paired candidate claims the truck and trailer.
        # Conflicting lower-scored candidates get demoted to "near" (kept for review).
        hour_candidates.sort(key=lambda row: row["_sort_score"], reverse=True)
        claimed_trucks: set[str] = set()
        claimed_trailers: set[str] = set()
        for row in hour_candidates:
            truck_id = str(row.get("truck_id") or "")
            trailer_id = str(row.get("trailer_id") or "")
            is_paired = row.get("status") == "paired"
            if is_paired:
                if truck_id in claimed_trucks or trailer_id in claimed_trailers:
                    # Demote: this truck or trailer already has a stronger match this hour
                    row["status"] = "near"
                    row["paired_evidence"] = False
                    row["billable_candidate"] = False
                else:
                    claimed_trucks.add(truck_id)
                    claimed_trailers.add(trailer_id)
            row.pop("_sort_score", None)
            records.append(row)
    return records


def apply_billable_candidate_rules(
    hourly_rows: list[dict[str, Any]],
    *,
    min_pair_hours: int,
    min_pair_days: int,
    min_confidence: float,
) -> None:
    """Mark sustained, non-yard paired evidence as billable-candidate hours.

    Hourly `paired_evidence` is intentionally sensitive: it should catch short
    anomalies like 129/759012 for review. `billable_candidate` is intentionally
    more conservative and should avoid one-off yard/roadside/convoy noise.

    Rule: for the same truck + trailer, require at least `min_pair_hours`
    qualifying non-yard paired hours across at least `min_pair_days` service
    dates before any hours in that group are marked billable candidates.
    """
    min_pair_hours = max(1, int(min_pair_hours))
    min_pair_days = max(1, int(min_pair_days))
    min_confidence = max(0.0, min(1.0, float(min_confidence)))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for row in hourly_rows:
        qualifies_hour = bool(row.get("billable_candidate"))
        row["billable_candidate"] = False
        if not qualifies_hour:
            continue
        if row.get("status") != "paired":
            continue
        if row.get("truck_yard") or row.get("trailer_yard"):
            continue
        if float(row.get("confidence") or 0) < min_confidence:
            continue
        key = (str(row.get("truck_id") or ""), str(row.get("trailer_id") or ""))
        groups[key].append(row)

    for rows in groups.values():
        if len(rows) < min_pair_hours:
            continue
        service_dates = {str(row.get("service_date") or "") for row in rows if row.get("service_date")}
        if len(service_dates) < min_pair_days:
            continue
        for row in rows:
            row["billable_candidate"] = True

    # --- CA Yard idle-reset rule ---
    # If a pair is billable but ALL their evidence is inside the California Yard
    # (or they've been idle in CA yard for 3+ consecutive days), retroactively
    # un-mark the idle period. Only paired hours where the unit actually LEAVES
    # the CA yard with the same partner count as billable.
    _apply_ca_yard_idle_reset(hourly_rows, ca_idle_days=3)


def _apply_ca_yard_idle_reset(hourly_rows: list[dict[str, Any]], ca_idle_days: int = 3) -> None:
    """CA Yard billing exception: revoke billable on idle CA-yard-only pairs.

    Logic per truck+trailer group:
    1. Identify consecutive runs of CA-yard hours with no movement (miles_traveled == 0).
    2. If an idle run lasts >= ca_idle_days, un-mark the billable flag on the
       preceding (ca_idle_days - 1) days retroactively.
    3. If a pair ONLY has CA-yard hours (never leaves together), none are billable.
    4. If the pair leaves the CA yard together (has non-yard billable hours),
       the departure day's hours stay billable.
    """
    CA_YARD = "California Yard"
    # Build per-pair, date-ordered views of ALL rows (billable or not)
    from collections import defaultdict

    pair_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in hourly_rows:
        key = (str(row.get("truck_id") or ""), str(row.get("trailer_id") or ""))
        pair_rows[key].append(row)

    for (truck_id, trailer_id), rows in pair_rows.items():
        # Only process pairs that have at least one billable hour
        billable_rows = [r for r in rows if r.get("billable_candidate")]
        if not billable_rows:
            continue

        # Check if this pair has any non-yard billable hours (proof they travel together)
        non_yard_billable = [
            r for r in billable_rows
            if not r.get("truck_yard") and not r.get("trailer_yard")
        ]

        # All evidence rows in the CA yard for this pair
        ca_yard_rows = [
            r for r in rows
            if r.get("truck_yard") == CA_YARD or r.get("trailer_yard") == CA_YARD
        ]

        if not ca_yard_rows:
            continue  # Not relevant to CA yard logic

        # Group CA yard rows by service_date
        ca_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in ca_yard_rows:
            d = str(r.get("service_date") or "")
            if d:
                ca_dates[d].append(r)

        # Check for consecutive idle days in CA yard
        sorted_dates = sorted(ca_dates.keys())
        consecutive_idle = 0
        idle_streak_dates: list[str] = []

        for d in sorted_dates:
            day_rows = ca_dates[d]
            # Consider a day "idle" if total miles_traveled for all rows is ~0
            day_miles = sum(float(r.get("miles_traveled") or 0) for r in day_rows)
            if day_miles < 1.0:  # Less than 1 mile = effectively idle
                consecutive_idle += 1
                idle_streak_dates.append(d)
            else:
                # Moving day resets the streak
                consecutive_idle = 0
                idle_streak_dates = []

            # If idle streak hits threshold, revoke billable on preceding days
            if consecutive_idle >= ca_idle_days:
                # Revoke the (ca_idle_days - 1) days before the trigger day
                revoke_dates = set(idle_streak_dates[:-1])  # All except the trigger day
                for r in rows:
                    if (
                        r.get("billable_candidate")
                        and str(r.get("service_date") or "") in revoke_dates
                        and (r.get("truck_yard") == CA_YARD or r.get("trailer_yard") == CA_YARD)
                    ):
                        r["billable_candidate"] = False

        # If pair NEVER leaves CA yard together, no hours should be billable
        if not non_yard_billable:
            for r in billable_rows:
                r["billable_candidate"] = False


def dedupe_hourly_rows(hourly_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate hourly rows before batch upsert/summarization.

    Chunked rebuilds use inclusive boundaries so an asset-hour that lands exactly
    on a boundary can be produced by both adjacent chunks. Postgres cannot update
    the same unique key twice inside one INSERT .. ON CONFLICT batch.
    """
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in hourly_rows:
        key = (
            str(row.get("hour_start") or ""),
            str(row.get("truck_id") or ""),
            str(row.get("trailer_id") or ""),
            str(row.get("source") or "auto"),
        )
        existing = by_key.get(key)
        if existing is None or _hourly_row_rank(row) > _hourly_row_rank(existing):
            by_key[key] = row
    return list(by_key.values())


def _hourly_row_rank(row: dict[str, Any]) -> tuple[int, float, int, float]:
    status = str(row.get("status") or "")
    paired_rank = 2 if status == "paired" else (1 if status == "near" else 0)
    confidence = float(row.get("confidence") or 0)
    pings = int(row.get("truck_pings") or 0) + int(row.get("trailer_pings") or 0)
    distance = float(row.get("best_distance_miles") or 999)
    return paired_rank, confidence, pings, -distance


def summarize_rows(hourly_rows: list[dict[str, Any]], *, job_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not hourly_rows:
        return [], []
    df = pl.DataFrame(hourly_rows, infer_schema_length=len(hourly_rows)).with_columns(
        pl.col("paired_evidence").cast(pl.Int64).alias("paired_i"),
        (pl.col("status") == "same_yard").cast(pl.Int64).alias("same_yard_i"),
        (pl.col("status") == "near").cast(pl.Int64).alias("near_i"),
        pl.col("billable_candidate").cast(pl.Int64).alias("billable_i"),
    )

    daily = (
        df.group_by(["service_date", "week_start", "truck_id", "trailer_id", "source"])
        .agg(
            pl.col("paired_i").sum().alias("paired_hours"),
            pl.col("same_yard_i").sum().alias("same_yard_hours"),
            pl.col("near_i").sum().alias("near_hours"),
            pl.col("billable_i").sum().alias("billable_candidate_hours"),
            pl.len().alias("evidence_hours"),
            pl.col("best_distance_miles").mean().round(3).alias("avg_distance_miles"),
            pl.col("best_distance_miles").min().round(3).alias("min_distance_miles"),
            pl.col("confidence").mean().round(3).alias("avg_confidence"),
            pl.col("miles_traveled").sum().round(2).alias("miles_traveled"),
            pl.col("hour_start").min().alias("first_evidence_at"),
            pl.col("hour_start").max().alias("last_evidence_at"),
            pl.col("truck_pings").sum().alias("truck_pings"),
            pl.col("trailer_pings").sum().alias("trailer_pings"),
        )
        .with_columns(pl.lit(job_id).alias("job_id"), pl.lit(datetime.now(timezone.utc).isoformat()).alias("computed_at"))
        .sort(["service_date", "trailer_id", "truck_id"])
    )

    weekly = (
        daily.group_by(["week_start", "truck_id", "trailer_id", "source"])
        .agg(
            pl.col("paired_hours").sum(),
            pl.col("same_yard_hours").sum(),
            pl.col("near_hours").sum(),
            pl.col("billable_candidate_hours").sum(),
            pl.len().alias("evidence_days"),
            pl.col("avg_distance_miles").mean().round(3).alias("avg_distance_miles"),
            pl.col("min_distance_miles").min().round(3).alias("min_distance_miles"),
            pl.col("avg_confidence").mean().round(3).alias("avg_confidence"),
            pl.col("miles_traveled").sum().round(2).alias("miles_traveled"),
            pl.col("first_evidence_at").min().alias("first_evidence_at"),
            pl.col("last_evidence_at").max().alias("last_evidence_at"),
        )
        .with_columns(
            pl.lit("pending").alias("review_status"),
            pl.lit(job_id).alias("job_id"),
            pl.lit(datetime.now(timezone.utc).isoformat()).alias("computed_at"),
        )
        .sort(["week_start", "trailer_id", "truck_id"])
    )
    daily_rows = daily.to_dicts()
    return daily_rows, weekly.to_dicts()


def summarize_weekly_from_daily(daily_rows: list[dict[str, Any]], *, job_id: str) -> list[dict[str, Any]]:
    if not daily_rows:
        return []
    df = pl.DataFrame(daily_rows, infer_schema_length=len(daily_rows))
    weekly = (
        df.group_by(["week_start", "truck_id", "trailer_id", "source"])
        .agg(
            pl.col("paired_hours").sum(),
            pl.col("same_yard_hours").sum(),
            pl.col("near_hours").sum(),
            pl.col("billable_candidate_hours").sum(),
            pl.len().alias("evidence_days"),
            pl.col("avg_distance_miles").mean().round(3).alias("avg_distance_miles"),
            pl.col("min_distance_miles").min().round(3).alias("min_distance_miles"),
            pl.col("avg_confidence").mean().round(3).alias("avg_confidence"),
            pl.col("miles_traveled").sum().round(2).alias("miles_traveled"),
            pl.col("first_evidence_at").min().alias("first_evidence_at"),
            pl.col("last_evidence_at").max().alias("last_evidence_at"),
        )
        .with_columns(
            pl.lit("pending").alias("review_status"),
            pl.lit(job_id).alias("job_id"),
            pl.lit(datetime.now(timezone.utc).isoformat()).alias("computed_at"),
        )
        .sort(["week_start", "trailer_id", "truck_id"])
    )
    return weekly.to_dicts()


def compute_trailer_activity(
    asset_tracks: list[AssetTrack],
    hourly_rows: list[dict[str, Any]],
    *,
    job_id: str,
) -> list[dict[str, Any]]:
    """Compute per-trailer daily activity summary (movement, paired hours, unmatched).

    This enables the "unmatched moving trailers" alert in the UI.  A trailer
    is considered "moving" in an hour if its centroid-to-centroid displacement
    across consecutive pings exceeds 0.5 miles or if any ping has speed >= 5 mph.
    """
    # Build per-trailer-day activity from asset tracks
    trailer_day: dict[tuple[str, date], dict[str, Any]] = {}
    for track in asset_tracks:
        if track.asset_type != "trailer":
            continue
        service_date = track.hour_start.date()
        key = (track.asset_id, service_date)
        acc = trailer_day.setdefault(key, {
            "trailer_id": track.asset_id,
            "service_date": service_date,
            "active_hours": 0,
            "moving_hours": 0,
            "miles_moved": 0.0,
            "in_yard_hours": 0,
            "provider": "",
            "division": "",
        })
        acc["active_hours"] += 1
        if track.provider:
            acc["provider"] = track.provider
        if track.division:
            acc["division"] = track.division

        # Check if this trailer-hour shows movement
        is_moving = False
        hour_miles = 0.0
        pts = track.points
        if len(pts) >= 2:
            for i in range(1, len(pts)):
                seg = haversine_miles(pts[i - 1].lat, pts[i - 1].lon, pts[i].lat, pts[i].lon)
                hour_miles += seg
            if hour_miles >= 0.5:
                is_moving = True
        # Also check speed
        for p in pts:
            if p.speed is not None and p.speed >= MOVING_SPEED_THRESHOLD:
                is_moving = True
                break
        if is_moving:
            acc["moving_hours"] += 1
            acc["miles_moved"] += hour_miles

        # Check if in a yard
        in_yard = False
        for _name, suppression_radius in YARD_SUPPRESSION_RADIUS.items():
            yard_lat, yard_lon, _r = YARD_GEOFENCES[_name]
            if haversine_miles(track.lat, track.lon, yard_lat, yard_lon) <= suppression_radius:
                in_yard = True
                break
        if in_yard:
            acc["in_yard_hours"] += 1

    # Count paired hours per trailer-day from hourly evidence
    paired_by_trailer_day: dict[tuple[str, str], int] = defaultdict(int)
    for row in hourly_rows:
        if row.get("status") == "paired":
            trailer_id = str(row.get("trailer_id") or "")
            sd = str(row.get("service_date") or "")
            if trailer_id and sd:
                paired_by_trailer_day[(trailer_id, sd)] += 1

    # Build output rows
    computed_at = datetime.now(timezone.utc).isoformat()
    out: list[dict[str, Any]] = []
    for (trailer_id, service_date), acc in trailer_day.items():
        paired = paired_by_trailer_day.get((trailer_id, service_date.isoformat()), 0)
        moving = acc["moving_hours"]
        # Unmatched moving hours = moving hours not in a yard that lack pairing
        non_yard_moving = max(0, moving - acc["in_yard_hours"])
        unmatched = max(0, non_yard_moving - paired)
        out.append({
            "service_date": service_date.isoformat(),
            "week_start": _week_start(service_date).isoformat(),
            "trailer_id": trailer_id,
            "active_hours": acc["active_hours"],
            "moving_hours": moving,
            "miles_moved": round(acc["miles_moved"], 2),
            "paired_hours": paired,
            "unmatched_moving_hours": unmatched,
            "in_yard_hours": acc["in_yard_hours"],
            "provider": acc["provider"],
            "division": acc["division"],
            "job_id": job_id,
            "computed_at": computed_at,
        })
    return out


class SupabaseClient:
    def __init__(self, url: str, key: str, *, batch_size: int) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.batch_size = max(1, int(batch_size))
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}

    def load_history(self, start: datetime, end: datetime, *, hard_cap: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        # Filter to accepted historical sources only (exclude sparse dispatch board snapshots)
        source_filter = ",".join(f"source.eq.{s}" for s in MATCHING_SOURCES)
        params_base = {
            "select": "asset_type,asset_id,division,lat,lon,speed,heading_deg,provider,recorded_at,address",
            "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
            "or": f"({source_filter})",
            "order": "recorded_at.asc",
        }
        while offset < hard_cap:
            params = {**params_base, "limit": PAGE_SIZE, "offset": offset}
            response = requests.get(f"{self.url}/rest/v1/assets_history", headers=self.headers, params=params, timeout=90)
            if not response.ok:
                raise RuntimeError(f"assets_history load failed: HTTP {response.status_code} {response.text[:500]}")
            page = response.json()
            if not isinstance(page, list):
                break
            out.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            print(f"  loaded {len(out):,} history rows...", flush=True)
            time.sleep(0.03)
        if offset >= hard_cap:
            print(f"WARNING: reached hard cap {hard_cap:,}; output may be incomplete.")
        return out

    def insert_job(self, job_id: str, start: datetime, end: datetime, args: argparse.Namespace) -> None:
        self.insert("gps_pairing_job_runs", {
            "job_id": job_id,
            "job_type": "hourly_evidence",
            "status": "running",
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "max_distance_miles": args.max_distance,
            "near_distance_miles": args.near_distance,
            "max_ping_gap_minutes": args.max_ping_gap_minutes,
            "message": "Hourly evidence job started.",
        })

    def finish_job(self, job_id: str, *, status: str, history_rows: int = 0, usable_points: int = 0, hourly_rows: int = 0, daily_rows: int = 0, weekly_rows: int = 0, message: str = "", error: str = "") -> None:
        self.patch("gps_pairing_job_runs", {
            "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "history_rows": int(history_rows or 0),
            "usable_points": int(usable_points or 0),
            "hourly_rows": int(hourly_rows or 0),
            "daily_rows": int(daily_rows or 0),
            "weekly_rows": int(weekly_rows or 0),
            "message": message,
            "error": error,
        }, filters={"job_id": f"eq.{job_id}"})

    def insert(self, table: str, row: dict[str, Any]) -> None:
        response = requests.post(f"{self.url}/rest/v1/{table}", headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"}, json=row, timeout=60)
        if not response.ok:
            raise RuntimeError(f"insert {table} failed: HTTP {response.status_code} {response.text[:500]}")

    def upsert(self, table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> None:
        if not rows:
            return
        for i in range(0, len(rows), self.batch_size):
            batch = rows[i:i + self.batch_size]
            response = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": on_conflict},
                json=batch,
                timeout=90,
            )
            if not response.ok:
                raise RuntimeError(f"upsert {table} failed: HTTP {response.status_code} {response.text[:500]}")
            print(f"  {table}: wrote {min(i + len(batch), len(rows)):,}/{len(rows):,}", flush=True)

    def patch(self, table: str, row: dict[str, Any], *, filters: dict[str, Any]) -> None:
        response = requests.patch(f"{self.url}/rest/v1/{table}", headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"}, params=filters, json=row, timeout=60)
        if not response.ok:
            raise RuntimeError(f"patch {table} failed: HTTP {response.status_code} {response.text[:500]}")

    def delete_range(self, table: str, column: str, start: datetime, end: datetime) -> None:
        response = requests.delete(f"{self.url}/rest/v1/{table}", headers=self.headers, params={column: f"gte.{start.isoformat()}", "and": f"({column}.lte.{end.isoformat()},source.eq.auto)"}, timeout=90)
        if not response.ok:
            raise RuntimeError(f"delete {table} failed: HTTP {response.status_code} {response.text[:500]}")

    def delete_date_range(self, table: str, column: str, start: date, end: date) -> None:
        response = requests.delete(f"{self.url}/rest/v1/{table}", headers=self.headers, params={column: f"gte.{start.isoformat()}", "and": f"({column}.lte.{end.isoformat()},source.eq.auto)"}, timeout=90)
        if not response.ok:
            raise RuntimeError(f"delete {table} failed: HTTP {response.status_code} {response.text[:500]}")


def _row_to_point(row: dict[str, Any]) -> Point | None:
    try:
        ts = datetime.fromisoformat(str(row.get("recorded_at") or "").replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        lat = float(row.get("lat"))
        lon = float(row.get("lon"))
    except (TypeError, ValueError):
        return None
    if lat == 0 and lon == 0:
        return None
    return Point(
        asset_type=str(row.get("asset_type") or ""),
        asset_id=str(row.get("asset_id") or ""),
        ts=ts,
        lat=lat,
        lon=lon,
        speed=_to_optional_float(row.get("speed")),
        heading_deg=_to_optional_float(row.get("heading_deg")),
        provider=str(row.get("provider") or ""),
        division=str(row.get("division") or ""),
        address=str(row.get("address") or ""),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute all-fleet hourly truck/trailer evidence from Supabase GPS history.")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--start", help="UTC date/datetime start. Use with --end.")
    parser.add_argument("--end", help="UTC date/datetime end. Use with --start.")
    parser.add_argument("--max-distance", type=float, default=0.5, help="Miles to classify as paired evidence.")
    parser.add_argument("--near-distance", type=float, default=1.0, help="Miles to retain as near evidence for review.")
    parser.add_argument("--max-ping-gap-minutes", type=float, default=5, help="Max timestamp gap for dense ping interpolation/matching.")
    parser.add_argument("--min-matches", type=int, default=2, help="Minimum close timestamp matches to mark an hour paired.")
    parser.add_argument("--min-match-ratio", type=float, default=0.5, help="Fallback close-match ratio to mark sparse hours paired.")
    parser.add_argument("--billable-min-pair-hours", type=int, default=2, help="Minimum qualifying paired hours for a truck/trailer before marking billable candidates.")
    parser.add_argument("--billable-min-pair-days", type=int, default=2, help="Minimum distinct service dates for a truck/trailer before marking billable candidates.")
    parser.add_argument("--billable-min-confidence", type=float, default=0.55, help="Minimum hourly confidence for billable candidate hours.")
    parser.add_argument("--max-candidates-per-trailer-hour", type=int, default=5, help="Keep only the strongest truck candidates per trailer-hour.")
    parser.add_argument("--include-same-yard", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--hard-cap", type=int, default=DEFAULT_HARD_CAP)
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS, help="Chunk size for non-dry-run rebuilds; use 0 to process in one query.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.start or args.end:
        if not args.start or not args.end:
            raise SystemExit("Use both --start and --end, or neither.")
        start = _parse_date_or_datetime(args.start, is_end=False)
        end = _parse_date_or_datetime(args.end, is_end=True)
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, int(args.days)))
    if end < start:
        start, end = end, start
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _parse_date_or_datetime(value: str, *, is_end: bool) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if "T" not in text and len(text) == 10:
        d = date.fromisoformat(text)
        return datetime.combine(d, datetime.max.time() if is_end else datetime.min.time(), tzinfo=timezone.utc)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_secrets() -> dict[str, str]:
    secrets: dict[str, str] = {}
    for path in (ROOT / ".streamlit" / "secrets.toml", ROOT / ".env"):
        if not path.exists():
            continue
        try:
            if path.suffix == ".toml":
                import tomllib
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                for key, value in data.items():
                    if isinstance(value, dict):
                        for nested_key, nested_value in value.items():
                            secrets[nested_key] = str(nested_value)
                    else:
                        secrets[key] = str(value)
            else:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    secrets[key.strip()] = value.strip().strip('"').strip("'")
        except Exception as exc:
            print(f"WARNING: could not read {path.name}: {exc}", file=sys.stderr)
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


def _timestamp_matches(truck: AssetTrack, trailer: AssetTrack, *, max_ping_gap_minutes: float) -> list[TimestampMatch]:
    """Match sparse-unit ping times against interpolated positions on the denser unit.

    The sparser track provides the target timestamps. The denser track is linearly
    interpolated between bracketing pings when possible; otherwise the nearest ping
    is used if it is within ``max_ping_gap_minutes``. This avoids hourly centroids
    and preserves the 10-second GPSTab truck precision against 5-minute Anytrek
    trailer pings.
    """
    sample = truck if truck.pings <= trailer.pings else trailer
    other = trailer if sample is truck else truck
    other_times = [p.ts for p in other.points]
    out: list[TimestampMatch] = []

    for sample_point in sample.points:
        projected = _interpolated_position(other.points, other_times, sample_point.ts, max_ping_gap_minutes=max_ping_gap_minutes)
        if projected is None:
            continue
        other_lat, other_lon, gap_minutes, other_ts, other_speed, other_heading = projected
        if sample is truck:
            truck_lat, truck_lon = sample_point.lat, sample_point.lon
            trailer_lat, trailer_lon = other_lat, other_lon
            truck_ts, trailer_ts = sample_point.ts, other_ts
        else:
            truck_lat, truck_lon = other_lat, other_lon
            trailer_lat, trailer_lon = sample_point.lat, sample_point.lon
            truck_ts, trailer_ts = other_ts, sample_point.ts
        truck_speed = sample_point.speed if sample is truck else other_speed
        trailer_speed = other_speed if sample is truck else sample_point.speed
        truck_heading = sample_point.heading_deg if sample is truck else other_heading
        trailer_heading = other_heading if sample is truck else sample_point.heading_deg
        movement_score, movement_mismatch, movement_compatible = _movement_agreement(
            truck_speed=truck_speed,
            trailer_speed=trailer_speed,
            truck_heading=truck_heading,
            trailer_heading=trailer_heading,
        )

        out.append(TimestampMatch(
            distance_miles=haversine_miles(truck_lat, truck_lon, trailer_lat, trailer_lon),
            ping_gap_minutes=gap_minutes,
            truck_lat=truck_lat,
            truck_lon=truck_lon,
            trailer_lat=trailer_lat,
            trailer_lon=trailer_lon,
            truck_ts=truck_ts,
            trailer_ts=trailer_ts,
            truck_speed=truck_speed,
            trailer_speed=trailer_speed,
            truck_heading=truck_heading,
            trailer_heading=trailer_heading,
            movement_score=movement_score,
            movement_mismatch=movement_mismatch,
            movement_compatible=movement_compatible,
        ))
    return out


def _interpolated_position(
    points: tuple[Point, ...],
    times: list[datetime],
    target: datetime,
    *,
    max_ping_gap_minutes: float,
) -> tuple[float, float, float, datetime, float | None, float | None] | None:
    if not points:
        return None
    max_gap_seconds = max(1.0, max_ping_gap_minutes * 60.0)
    i = bisect_left(times, target)

    before = points[i - 1] if i > 0 else None
    after = points[i] if i < len(points) else None

    if before and after:
        span_seconds = (after.ts - before.ts).total_seconds()
        gap_before = abs((target - before.ts).total_seconds())
        gap_after = abs((after.ts - target).total_seconds())
        nearest_gap = min(gap_before, gap_after)
        if span_seconds > 0 and nearest_gap <= max_gap_seconds and span_seconds <= max_gap_seconds * 2:
            ratio = max(0.0, min(1.0, (target - before.ts).total_seconds() / span_seconds))
            lat = before.lat + (after.lat - before.lat) * ratio
            lon = before.lon + (after.lon - before.lon) * ratio
            speed = _interpolate_optional(before.speed, after.speed, ratio)
            if speed is None:
                speed = before.speed if gap_before <= gap_after else after.speed
            heading = _interpolate_heading(before.heading_deg, after.heading_deg, ratio)
            if heading is None:
                segment_distance = haversine_miles(before.lat, before.lon, after.lat, after.lon)
                heading = _bearing_degrees(before.lat, before.lon, after.lat, after.lon) if segment_distance >= MOTION_MIN_SEGMENT_MILES else None
            return lat, lon, nearest_gap / 60.0, target, speed, heading

    nearest: Point | None = None
    nearest_gap_seconds = float("inf")
    for candidate in (before, after):
        if candidate is None:
            continue
        gap = abs((candidate.ts - target).total_seconds())
        if gap < nearest_gap_seconds:
            nearest_gap_seconds = gap
            nearest = candidate
    if nearest is None or nearest_gap_seconds > max_gap_seconds:
        return None
    return nearest.lat, nearest.lon, nearest_gap_seconds / 60.0, nearest.ts, nearest.speed, nearest.heading_deg


def _movement_agreement(
    *,
    truck_speed: float | None,
    trailer_speed: float | None,
    truck_heading: float | None,
    trailer_heading: float | None,
) -> tuple[float, bool, bool]:
    truck_speed = _clean_speed(truck_speed)
    trailer_speed = _clean_speed(trailer_speed)
    truck_moving = truck_speed is not None and truck_speed >= MOVING_SPEED_THRESHOLD
    trailer_moving = trailer_speed is not None and trailer_speed >= MOVING_SPEED_THRESHOLD
    truck_stationary = truck_speed is not None and truck_speed <= STATIONARY_SPEED_THRESHOLD
    trailer_stationary = trailer_speed is not None and trailer_speed <= STATIONARY_SPEED_THRESHOLD

    if (truck_moving and trailer_stationary) or (trailer_moving and truck_stationary):
        return 0.0, True, False

    if truck_moving and trailer_moving:
        speed_score = _speed_agreement(truck_speed, trailer_speed)
        heading_delta = _heading_delta(truck_heading, trailer_heading)
        heading_score = 0.5 if heading_delta is None else max(0.0, 1.0 - heading_delta / 90.0)
        mismatch = heading_delta is not None and heading_delta > 75.0
        score = 0.45 * speed_score + 0.55 * heading_score
        return score, mismatch, (not mismatch and score >= 0.45)

    if truck_stationary and trailer_stationary:
        return 0.35, False, False

    return 0.5, False, False


def _dense_confidence(
    *,
    best_distance: float,
    median_distance: float,
    median_gap: float,
    close_ratio: float,
    evidence_ratio: float,
    close_count: int,
    max_distance: float,
    max_gap: float,
    paired: bool,
    same_yard: bool,
    movement_score: float = 0.5,
) -> float:
    distance_score = max(0.0, 1.0 - min(best_distance, median_distance) / max(max_distance, 0.01))
    gap_score = max(0.0, 1.0 - median_gap / max(max_gap, 0.01))
    density_score = min(1.0, close_count / 4.0)
    consistency_score = max(0.0, min(1.0, 0.55 * close_ratio + 0.45 * evidence_ratio))
    movement_score = max(0.0, min(1.0, movement_score))
    score = 0.30 * distance_score + 0.20 * gap_score + 0.20 * consistency_score + 0.15 * density_score + 0.15 * movement_score
    if same_yard and not paired:
        return 0.0
    if not paired:
        return min(0.49, score * 0.55)
    return max(0.0, min(1.0, score))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    to_rad = math.pi / 180
    d_lat = (lat2 - lat1) * to_rad
    d_lon = (lon2 - lon1) * to_rad
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1 * to_rad) * math.cos(lat2 * to_rad) * math.sin(d_lon / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(d_lon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _heading_delta(a: float | None, b: float | None) -> float | None:
    a = _normalize_heading(a)
    b = _normalize_heading(b)
    if a is None or b is None:
        return None
    diff = abs(a - b) % 360.0
    return 360.0 - diff if diff > 180.0 else diff


def _speed_agreement(a: float | None, b: float | None) -> float:
    a = _clean_speed(a)
    b = _clean_speed(b)
    if a is None or b is None:
        return 0.5
    max_speed = max(a, b, 1.0)
    diff_ratio = abs(a - b) / max_speed
    return max(0.0, 1.0 - diff_ratio)


def yard_name(lat: float, lon: float) -> str:
    for name, (yard_lat, yard_lon, radius) in YARD_GEOFENCES.items():
        if haversine_miles(lat, lon, yard_lat, yard_lon) <= radius:
            return name
    return ""


def yard_proximity_names(lat: float, lon: float) -> set[str]:
    names: set[str] = set()
    for name, (yard_lat, yard_lon, radius) in YARD_GEOFENCES.items():
        if haversine_miles(lat, lon, yard_lat, yard_lon) <= radius * YARD_PROXIMITY_MULTIPLIER:
            names.add(name)
    return names


def _both_in_yard_suppression_zone(truck_lat: float, truck_lon: float, trailer_lat: float, trailer_lon: float) -> bool:
    """Return True if both truck and trailer are within the same yard's suppression radius."""
    for name, suppression_radius in YARD_SUPPRESSION_RADIUS.items():
        yard_lat, yard_lon, _geofence_radius = YARD_GEOFENCES[name]
        truck_dist = haversine_miles(truck_lat, truck_lon, yard_lat, yard_lon)
        if truck_dist > suppression_radius:
            continue
        trailer_dist = haversine_miles(trailer_lat, trailer_lon, yard_lat, yard_lon)
        if trailer_dist <= suppression_radius:
            return True
    return False


def _miles_traveled_together(matches: list[TimestampMatch]) -> float:
    """Sum distance traveled between consecutive matched positions.

    Uses the truck positions sorted by timestamp. For truly paired assets
    these are effectively the same path.
    """
    if len(matches) < 2:
        return 0.0
    sorted_matches = sorted(matches, key=lambda m: m.truck_ts)
    total = 0.0
    for i in range(1, len(sorted_matches)):
        prev = sorted_matches[i - 1]
        cur = sorted_matches[i]
        total += haversine_miles(prev.truck_lat, prev.truck_lon, cur.truck_lat, cur.truck_lon)
    return total


def _passes_strict_yard_gate(
    *,
    close_count: int,
    sparse_pings: int,
    evidence_ratio: float,
    movement_compatible_count: int,
    yard_names: set[str],
) -> bool:
    clean_yard_names = {name for name in yard_names if name}
    absolute_target = YARD_STRICT_CALIFORNIA_MATCHES if "California Yard" in clean_yard_names else YARD_STRICT_GENERIC_MATCHES
    required_matches = max(4, min(max(1, sparse_pings), absolute_target))
    if close_count < required_matches:
        return False
    if evidence_ratio < YARD_STRICT_MIN_EVIDENCE_RATIO:
        return False
    required_movement = max(YARD_STRICT_MIN_MOVEMENT_MATCHES, min(4, required_matches // 2))
    return movement_compatible_count >= required_movement


def _to_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _clean_speed(value: float | None) -> float | None:
    parsed = _to_optional_float(value)
    if parsed is None or parsed < 0 or parsed > 140:
        return None
    return parsed


def _normalize_heading(value: float | None) -> float | None:
    parsed = _to_optional_float(value)
    if parsed is None:
        return None
    return parsed % 360.0


def _interpolate_optional(a: float | None, b: float | None, ratio: float) -> float | None:
    a = _to_optional_float(a)
    b = _to_optional_float(b)
    if a is None or b is None:
        return None
    return a + (b - a) * ratio


def _interpolate_heading(a: float | None, b: float | None, ratio: float) -> float | None:
    a = _normalize_heading(a)
    b = _normalize_heading(b)
    if a is None or b is None:
        return None
    delta = ((b - a + 540.0) % 360.0) - 180.0
    return (a + delta * ratio) % 360.0


def _floor_hour(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _mode(values: list[str]) -> str:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _print_preview(hourly_rows: list[dict[str, Any]], daily_rows: list[dict[str, Any]], weekly_rows: list[dict[str, Any]]) -> None:
    print("DRY RUN only — nothing written.")
    print(f"Hourly rows: {len(hourly_rows):,}")
    print(f"Daily rows: {len(daily_rows):,}")
    print(f"Weekly rows: {len(weekly_rows):,}")
    print("Sample hourly rows:")
    for row in hourly_rows[:10]:
        print(row)


def _print_trailer_activity_preview(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No trailer activity rows.")
        return
    unmatched = [r for r in rows if r.get("unmatched_moving_hours", 0) > 0]
    print(f"\nTrailer activity: {len(rows):,} total rows, {len(unmatched):,} with unmatched moving hours")
    unmatched.sort(key=lambda r: r.get("unmatched_moving_hours", 0), reverse=True)
    for row in unmatched[:15]:
        print(
            f"  trailer {row['trailer_id']}: {row['moving_hours']}h moving, "
            f"{row['paired_hours']}h paired, {row['unmatched_moving_hours']}h UNMATCHED, "
            f"{row['miles_moved']:.1f} mi, yard={row['in_yard_hours']}h"
        )


if __name__ == "__main__":
    main()
