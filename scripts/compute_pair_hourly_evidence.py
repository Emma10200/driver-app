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
# Accepted historical GPS evidence sources for pairing.
# Includes dense backfills plus blank-source historical imports that contain
# useful older 888/dispatch-board history for trucks like 129. Explicitly do
# NOT include sparse live publisher snapshots (truck_publish/trailer_publish).
MATCHING_SOURCES = ("gpstab_backfill", "anytrek_backfill", "track888_backfill", "eroad_backfill", "")


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
            hourly_rows, daily_rows, weekly_rows, stats = compute_window(
                client,
                start,
                end,
                args=args,
                job_id=job_id,
            )
            print(
                f"Computed {len(hourly_rows):,} hourly rows, {len(daily_rows):,} daily rows, "
                f"{len(weekly_rows):,} weekly rows from {stats['history_rows']:,} raw rows."
            )
            if args.dry_run:
                _print_preview(hourly_rows, daily_rows, weekly_rows)
                return

            print("Deleting existing auto rows in range...")
            delete_derived_range(client, start, end)
            write_derived_rows(client, hourly_rows, daily_rows, weekly_rows)
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
                hourly_rows, daily_rows, _weekly_unused, chunk_stats = compute_window(
                    client,
                    chunk_start,
                    chunk_end,
                    args=args,
                    job_id=job_id,
                )
                stats["history_rows"] += chunk_stats["history_rows"]
                stats["usable_points"] += chunk_stats["usable_points"]
                stats["hourly_rows"] += len(hourly_rows)
                all_hourly_rows.extend(hourly_rows)
                print(f"Writing chunk {chunk_index}: {len(hourly_rows):,} hourly rows")
                client.upsert("asset_pair_hourly_evidence", hourly_rows, on_conflict="hour_start,truck_id,trailer_id,source")
                chunk_start = chunk_end

            print("Computing final daily/weekly summaries from all hourly rows...")
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    rows = client.load_history(start, end, hard_cap=args.hard_cap)
    points = [_row_to_point(row) for row in rows]
    points = [point for point in points if point is not None]
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
    daily_rows, weekly_rows = summarize_rows(hourly_rows, job_id=job_id)
    return hourly_rows, daily_rows, weekly_rows, {
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

        for trailer in trailers:
            candidates: list[dict[str, Any]] = []
            for truck in trucks:
                matches = _timestamp_matches(truck, trailer, max_ping_gap_minutes=max_ping_gap_minutes)
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
                median_distance = _median(distances)
                median_gap = _median([m.ping_gap_minutes for m in matches])

                truck_yard = yard_name(best_near_match.truck_lat, best_near_match.truck_lon)
                trailer_yard = yard_name(best_near_match.trailer_lat, best_near_match.trailer_lon)
                same_yard = bool(truck_yard and truck_yard == trailer_yard)

                paired_by_distance = bool(close_matches) and (
                    close_count >= min_matches or evidence_ratio >= min_match_ratio
                )
                if paired_by_distance:
                    status = "same_yard" if same_yard and not include_same_yard_as_paired else "paired"
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
                        "billable_candidate": paired and confidence >= 0.55 and close_count >= min_matches,
                        "confidence": round(confidence, 3),
                        "best_distance_miles": round(best_match.distance_miles, 3),
                        "best_ping_gap_minutes": round(best_match.ping_gap_minutes, 2),
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
                row.pop("_sort_score", None)
                records.append(row)
    return records


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
            "select": "asset_type,asset_id,division,lat,lon,provider,recorded_at,address",
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
    parser.add_argument("--max-candidates-per-trailer-hour", type=int, default=3, help="Keep only the strongest truck candidates per trailer-hour.")
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
        other_lat, other_lon, gap_minutes, other_ts = projected
        if sample is truck:
            truck_lat, truck_lon = sample_point.lat, sample_point.lon
            trailer_lat, trailer_lon = other_lat, other_lon
            truck_ts, trailer_ts = sample_point.ts, other_ts
        else:
            truck_lat, truck_lon = other_lat, other_lon
            trailer_lat, trailer_lon = sample_point.lat, sample_point.lon
            truck_ts, trailer_ts = other_ts, sample_point.ts

        out.append(TimestampMatch(
            distance_miles=haversine_miles(truck_lat, truck_lon, trailer_lat, trailer_lon),
            ping_gap_minutes=gap_minutes,
            truck_lat=truck_lat,
            truck_lon=truck_lon,
            trailer_lat=trailer_lat,
            trailer_lon=trailer_lon,
            truck_ts=truck_ts,
            trailer_ts=trailer_ts,
        ))
    return out


def _interpolated_position(
    points: tuple[Point, ...],
    times: list[datetime],
    target: datetime,
    *,
    max_ping_gap_minutes: float,
) -> tuple[float, float, float, datetime] | None:
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
            return lat, lon, nearest_gap / 60.0, target

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
    return nearest.lat, nearest.lon, nearest_gap_seconds / 60.0, nearest.ts


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
) -> float:
    distance_score = max(0.0, 1.0 - min(best_distance, median_distance) / max(max_distance, 0.01))
    gap_score = max(0.0, 1.0 - median_gap / max(max_gap, 0.01))
    density_score = min(1.0, close_count / 4.0)
    consistency_score = max(0.0, min(1.0, 0.55 * close_ratio + 0.45 * evidence_ratio))
    score = 0.35 * distance_score + 0.25 * gap_score + 0.25 * consistency_score + 0.15 * density_score
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


def yard_name(lat: float, lon: float) -> str:
    for name, (yard_lat, yard_lon, radius) in YARD_GEOFENCES.items():
        if haversine_miles(lat, lon, yard_lat, yard_lon) <= radius:
            return name
    return ""


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


if __name__ == "__main__":
    main()
