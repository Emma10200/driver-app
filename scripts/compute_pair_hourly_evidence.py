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
EARTH_RADIUS_MILES = 3958.8
YARD_GEOFENCES = {
    "California Yard": (34.09686, -117.47642, 0.25),
    "Illinois Yard": (41.896873, -87.86982, 0.25),
}


@dataclass(frozen=True)
class AssetHour:
    hour_start: datetime
    asset_type: str
    asset_id: str
    pings: int
    first_ping: datetime
    last_ping: datetime
    lat: float
    lon: float
    provider: str
    division: str
    address: str


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
        rows = client.load_history(start, end, hard_cap=args.hard_cap)
        points = [_row_to_point(row) for row in rows]
        points = [point for point in points if point is not None]
        print(f"Loaded {len(rows):,} raw rows; usable coordinate rows: {len(points):,}")
        if not points:
            raise RuntimeError("No usable GPS points found for requested range.")

        asset_hours = build_asset_hours(points)
        print(f"Built {len(asset_hours):,} asset-hour summaries")

        hourly_rows = compute_hourly_evidence(
            asset_hours,
            max_distance_miles=args.max_distance,
            near_distance_miles=args.near_distance,
            max_ping_gap_minutes=args.max_ping_gap_minutes,
            include_same_yard_as_paired=args.include_same_yard,
            job_id=job_id,
        )
        print(f"Computed {len(hourly_rows):,} hourly candidate rows")
        daily_rows, weekly_rows = summarize_rows(hourly_rows, job_id=job_id)
        print(f"Computed {len(daily_rows):,} daily rows and {len(weekly_rows):,} weekly rows")

        if args.dry_run:
            _print_preview(hourly_rows, daily_rows, weekly_rows)
            return

        print("Deleting existing auto rows in range...")
        client.delete_range("asset_pair_hourly_evidence", "hour_start", start, end)
        client.delete_date_range("asset_pair_daily_summary", "service_date", start.date(), end.date())
        # Weekly rows are upserted. Existing review status can be overwritten only while UI review is not live.

        print("Writing hourly evidence...")
        client.upsert("asset_pair_hourly_evidence", hourly_rows, on_conflict="hour_start,truck_id,trailer_id,source")
        print("Writing daily summaries...")
        client.upsert("asset_pair_daily_summary", daily_rows, on_conflict="service_date,truck_id,trailer_id,source")
        print("Writing weekly review rows...")
        client.upsert("asset_pair_weekly_review", weekly_rows, on_conflict="week_start,truck_id,trailer_id,source")
        client.finish_job(
            job_id,
            status="complete",
            history_rows=len(rows),
            usable_points=len(points),
            hourly_rows=len(hourly_rows),
            daily_rows=len(daily_rows),
            weekly_rows=len(weekly_rows),
            message="Hourly evidence backfill completed.",
        )
        print(f"Done. Job {job_id} complete.")
    except Exception as exc:
        if not args.dry_run:
            try:
                client.finish_job(job_id, status="failed", error=str(exc)[:4000])
            except Exception:
                pass
        raise


def build_asset_hours(points: list[Point]) -> list[AssetHour]:
    grouped: dict[tuple[datetime, str, str], list[Point]] = defaultdict(list)
    for point in points:
        grouped[(_floor_hour(point.ts), point.asset_type, point.asset_id)].append(point)

    out: list[AssetHour] = []
    for (hour_start, asset_type, asset_id), group in grouped.items():
        group.sort(key=lambda p: p.ts)
        lat = sum(p.lat for p in group) / len(group)
        lon = sum(p.lon for p in group) / len(group)
        out.append(
            AssetHour(
                hour_start=hour_start,
                asset_type=asset_type,
                asset_id=asset_id,
                pings=len(group),
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


def compute_hourly_evidence(
    asset_hours: list[AssetHour],
    *,
    max_distance_miles: float,
    near_distance_miles: float,
    max_ping_gap_minutes: float,
    include_same_yard_as_paired: bool,
    job_id: str,
) -> list[dict[str, Any]]:
    by_hour: dict[datetime, dict[str, list[AssetHour]]] = defaultdict(lambda: {"truck": [], "trailer": []})
    for row in asset_hours:
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
            lat_delta = near_distance_miles / 69.0
            cos_lat = max(0.2, abs(math.cos(math.radians(trailer.lat))))
            lon_delta = near_distance_miles / (69.0 * cos_lat)
            for truck in trucks:
                if abs(truck.lat - trailer.lat) > lat_delta or abs(truck.lon - trailer.lon) > lon_delta:
                    continue
                ping_gap = _time_range_gap_minutes(truck, trailer)
                if ping_gap > max_ping_gap_minutes:
                    continue
                distance = haversine_miles(truck.lat, truck.lon, trailer.lat, trailer.lon)
                if distance > near_distance_miles:
                    continue

                truck_yard = yard_name(truck.lat, truck.lon)
                trailer_yard = yard_name(trailer.lat, trailer.lon)
                same_yard = bool(truck_yard and truck_yard == trailer_yard)
                if distance <= max_distance_miles:
                    status = "same_yard" if same_yard and not include_same_yard_as_paired else "paired"
                else:
                    status = "near"

                paired = status == "paired"
                confidence = _confidence(distance, ping_gap, truck.pings, trailer.pings, max_distance_miles, max_ping_gap_minutes, paired)
                records.append(
                    {
                        "hour_start": hour_start.isoformat(),
                        "service_date": hour_start.date().isoformat(),
                        "week_start": _week_start(hour_start.date()).isoformat(),
                        "truck_id": truck.asset_id,
                        "trailer_id": trailer.asset_id,
                        "status": status,
                        "paired_evidence": paired,
                        "billable_candidate": paired,
                        "confidence": round(confidence, 3),
                        "best_distance_miles": round(distance, 3),
                        "best_ping_gap_minutes": round(ping_gap, 1),
                        "truck_pings": truck.pings,
                        "trailer_pings": trailer.pings,
                        "truck_first_ping": truck.first_ping.isoformat(),
                        "truck_last_ping": truck.last_ping.isoformat(),
                        "trailer_first_ping": trailer.first_ping.isoformat(),
                        "trailer_last_ping": trailer.last_ping.isoformat(),
                        "truck_lat": round(truck.lat, 6),
                        "truck_lon": round(truck.lon, 6),
                        "trailer_lat": round(trailer.lat, 6),
                        "trailer_lon": round(trailer.lon, 6),
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
                    }
                )
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
    return daily.to_dicts(), weekly.to_dicts()


class SupabaseClient:
    def __init__(self, url: str, key: str, *, batch_size: int) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.batch_size = max(1, int(batch_size))
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}

    def load_history(self, start: datetime, end: datetime, *, hard_cap: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        params_base = {
            "select": "asset_type,asset_id,division,lat,lon,provider,recorded_at,address",
            "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
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
    parser.add_argument("--max-ping-gap-minutes", type=float, default=45)
    parser.add_argument("--include-same-yard", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--hard-cap", type=int, default=750000)
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


def _time_range_gap_minutes(truck: AssetHour, trailer: AssetHour) -> float:
    if truck.first_ping <= trailer.last_ping and trailer.first_ping <= truck.last_ping:
        return 0.0
    if truck.last_ping < trailer.first_ping:
        return (trailer.first_ping - truck.last_ping).total_seconds() / 60.0
    return (truck.first_ping - trailer.last_ping).total_seconds() / 60.0


def _confidence(distance: float, ping_gap: float, truck_pings: int, trailer_pings: int, max_distance: float, max_gap: float, paired: bool) -> float:
    if not paired:
        return 0.0
    dist_score = max(0.0, 1.0 - distance / max(max_distance, 0.01))
    time_score = max(0.0, 1.0 - ping_gap / max(max_gap, 1.0))
    density_score = min(1.0, min(truck_pings, trailer_pings) / 4.0)
    return 0.50 * dist_score + 0.30 * time_score + 0.20 * density_score


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
