#!/usr/bin/env python3
"""Build an hour-by-hour truck↔trailer GPS history report.

This script is intentionally different from the Unit Timeline pairing table:

* Unit Timeline / asset_pairings stores continuous co-location segments only.
* This report creates one row for every hour in the requested range, even when
  one side did not ping, pings were asynchronous, or both units were parked.

It is useful for forensic review when a unit obviously has multiple days of GPS
history but the timeline only shows a short confirmed segment.

Examples
--------
    python scripts/pair_hourly_history.py --truck 129 --trailer 759012 --days 60
    python scripts/pair_hourly_history.py --truck 129 --trailer 759012 --start 2026-06-01 --end 2026-06-30

Outputs two CSV files in gps_reports/ by default:
    pair_hourly_<truck>_<trailer>_<timestamp>.csv
    pair_daily_<truck>_<trailer>_<timestamp>.csv
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import polars as pl
except ImportError as exc:  # pragma: no cover - user environment guard
    raise SystemExit(
        "Polars is required for this report. Install dependencies with `python -m pip install -r requirements.txt`."
    ) from exc


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "gps_reports"
PAGE_SIZE = 1000
EARTH_RADIUS_MILES = 3958.8
YARD_RADIUS_MILES = 0.25
YARD_GEOFENCES = {
    "California Yard": (34.09686, -117.47642, YARD_RADIUS_MILES),
    "Illinois Yard": (41.896873, -87.86982, YARD_RADIUS_MILES),
}


@dataclass(frozen=True)
class Point:
    asset_type: str
    asset_id: str
    ts: datetime
    lat: float
    lon: float
    speed: float | None
    heading_deg: float | None
    division: str
    provider: str
    address: str


def main() -> None:
    args = _parse_args()
    start, end = _resolve_window(args)

    secrets = _load_secrets()
    supabase_url = (secrets.get("SUPABASE_URL") or "").rstrip("/")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY") or secrets.get("SUPABASE_KEY") or ""
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required in .env, .streamlit/secrets.toml, or environment.")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Hourly pair history: truck {args.truck} ↔ trailer {args.trailer}\n"
        f"Window: {start.isoformat()} → {end.isoformat()}\n"
        f"Max distance: {args.max_distance:.2f} mi; max ping gap: {args.max_ping_gap_minutes:.0f} min\n"
        f"Supabase: {supabase_url}"
    )

    rows = _load_pair_history(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        truck_id=args.truck,
        trailer_id=args.trailer,
        start=start,
        end=end,
        hard_cap=args.hard_cap,
    )
    print(f"Loaded {len(rows):,} raw GPS rows for the two units.")
    if not rows:
        raise SystemExit("No GPS history rows found for this truck/trailer in the selected range.")

    points = [_row_to_point(row) for row in rows]
    points = [point for point in points if point is not None]
    print(
        f"Usable coordinate rows: {len(points):,} "
        f"(truck={sum(p.asset_type == 'truck' for p in points):,}, trailer={sum(p.asset_type == 'trailer' for p in points):,})"
    )
    if not points:
        raise SystemExit("Rows were found, but none had valid coordinates/timestamps.")

    hourly = build_hourly_report(
        points=points,
        truck_id=args.truck,
        trailer_id=args.trailer,
        start=start,
        end=end,
        max_distance_miles=args.max_distance,
        max_ping_gap_minutes=args.max_ping_gap_minutes,
        include_same_yard_as_paired=args.include_same_yard,
    )
    daily = build_daily_summary(hourly)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_truck = _safe_name(args.truck)
    safe_trailer = _safe_name(args.trailer)
    hourly_path = output_dir / f"pair_hourly_{safe_truck}_{safe_trailer}_{stamp}.csv"
    daily_path = output_dir / f"pair_daily_{safe_truck}_{safe_trailer}_{stamp}.csv"
    hourly.write_csv(hourly_path)
    daily.write_csv(daily_path)

    _print_summary(hourly, daily, hourly_path, daily_path)


def build_hourly_report(
    *,
    points: list[Point],
    truck_id: str,
    trailer_id: str,
    start: datetime,
    end: datetime,
    max_distance_miles: float,
    max_ping_gap_minutes: float,
    include_same_yard_as_paired: bool,
) -> pl.DataFrame:
    start_hour = _floor_hour(start)
    end_hour = _floor_hour(end)
    hours = _hour_range(start_hour, end_hour)

    by_hour: dict[datetime, dict[str, list[Point]]] = defaultdict(lambda: {"truck": [], "trailer": []})
    for point in points:
        if point.ts < start or point.ts > end:
            continue
        if point.asset_type == "truck" and point.asset_id != truck_id:
            continue
        if point.asset_type == "trailer" and point.asset_id != trailer_id:
            continue
        by_hour[_floor_hour(point.ts)][point.asset_type].append(point)

    records: list[dict[str, Any]] = []
    for hour in hours:
        truck_points = sorted(by_hour[hour]["truck"], key=lambda p: p.ts)
        trailer_points = sorted(by_hour[hour]["trailer"], key=lambda p: p.ts)
        best_any = _best_pair(truck_points, trailer_points, max_ping_gap_minutes=None)
        best_tolerant = _best_pair(truck_points, trailer_points, max_ping_gap_minutes=max_ping_gap_minutes)
        best = best_tolerant or best_any

        status = _hour_status(
            truck_points=truck_points,
            trailer_points=trailer_points,
            best_tolerant=best_tolerant,
            best_any=best_any,
            max_distance_miles=max_distance_miles,
            include_same_yard_as_paired=include_same_yard_as_paired,
        )

        records.append(
            {
                "date": hour.date().isoformat(),
                "hour_start_utc": hour.isoformat(),
                "truck_id": truck_id,
                "trailer_id": trailer_id,
                "status": status,
                "paired_evidence": status == "paired",
                "truck_pings": len(truck_points),
                "trailer_pings": len(trailer_points),
                "truck_first_ping": _iso(truck_points[0].ts if truck_points else None),
                "truck_last_ping": _iso(truck_points[-1].ts if truck_points else None),
                "trailer_first_ping": _iso(trailer_points[0].ts if trailer_points else None),
                "trailer_last_ping": _iso(trailer_points[-1].ts if trailer_points else None),
                "best_distance_miles": _round_or_none(best.distance_miles if best else None, 3),
                "best_distance_within_gap_miles": _round_or_none(best_tolerant.distance_miles if best_tolerant else None, 3),
                "best_ping_gap_minutes": _round_or_none(best.time_gap_minutes if best else None, 1),
                "best_within_gap_minutes": _round_or_none(best_tolerant.time_gap_minutes if best_tolerant else None, 1),
                "truck_yard": best.truck_yard if best else "",
                "trailer_yard": best.trailer_yard if best else "",
                "truck_lat": _round_or_none(best.truck.lat if best else None, 6),
                "truck_lon": _round_or_none(best.truck.lon if best else None, 6),
                "trailer_lat": _round_or_none(best.trailer.lat if best else None, 6),
                "trailer_lon": _round_or_none(best.trailer.lon if best else None, 6),
                "truck_provider": _mode([p.provider for p in truck_points]),
                "trailer_provider": _mode([p.provider for p in trailer_points]),
                "truck_division": _mode([p.division for p in truck_points]),
                "trailer_division": _mode([p.division for p in trailer_points]),
                "truck_address": best.truck.address if best else "",
                "trailer_address": best.trailer.address if best else "",
            }
        )

    return pl.DataFrame(records, infer_schema_length=len(records))


def build_daily_summary(hourly: pl.DataFrame) -> pl.DataFrame:
    return (
        hourly
        .with_columns(
            pl.col("paired_evidence").cast(pl.Int64).alias("paired_hour_int"),
            (pl.col("status") == "same_yard").cast(pl.Int64).alias("same_yard_hour_int"),
            (pl.col("status") == "close_but_async").cast(pl.Int64).alias("close_async_hour_int"),
            (pl.col("truck_pings") > 0).cast(pl.Int64).alias("truck_has_data_int"),
            (pl.col("trailer_pings") > 0).cast(pl.Int64).alias("trailer_has_data_int"),
            ((pl.col("truck_pings") > 0) & (pl.col("trailer_pings") > 0)).cast(pl.Int64).alias("both_have_data_int"),
        )
        .group_by("date")
        .agg(
            pl.len().alias("hours_in_report"),
            pl.col("paired_hour_int").sum().alias("paired_hours"),
            pl.col("same_yard_hour_int").sum().alias("same_yard_hours"),
            pl.col("close_async_hour_int").sum().alias("close_but_async_hours"),
            pl.col("both_have_data_int").sum().alias("hours_with_both_data"),
            pl.col("truck_has_data_int").sum().alias("truck_data_hours"),
            pl.col("trailer_has_data_int").sum().alias("trailer_data_hours"),
            pl.col("truck_pings").sum().alias("truck_pings"),
            pl.col("trailer_pings").sum().alias("trailer_pings"),
            pl.col("best_distance_within_gap_miles").min().alias("min_distance_within_gap_miles"),
            pl.col("best_distance_within_gap_miles").mean().alias("avg_distance_within_gap_miles"),
        )
        .with_columns(
            (pl.col("paired_hours") / pl.col("hours_in_report") * 100).round(1).alias("paired_hour_pct"),
            pl.col("min_distance_within_gap_miles").round(3),
            pl.col("avg_distance_within_gap_miles").round(3),
        )
        .sort("date")
    )


@dataclass(frozen=True)
class BestPair:
    truck: Point
    trailer: Point
    distance_miles: float
    time_gap_minutes: float
    truck_yard: str
    trailer_yard: str


def _best_pair(
    truck_points: list[Point],
    trailer_points: list[Point],
    *,
    max_ping_gap_minutes: float | None,
) -> BestPair | None:
    best: BestPair | None = None
    best_key: tuple[float, float] | None = None
    for truck in truck_points:
        for trailer in trailer_points:
            gap = abs((truck.ts - trailer.ts).total_seconds()) / 60.0
            if max_ping_gap_minutes is not None and gap > max_ping_gap_minutes:
                continue
            distance = haversine_miles(truck.lat, truck.lon, trailer.lat, trailer.lon)
            key = (distance, gap)
            if best_key is None or key < best_key:
                best_key = key
                best = BestPair(
                    truck=truck,
                    trailer=trailer,
                    distance_miles=distance,
                    time_gap_minutes=gap,
                    truck_yard=yard_name(truck.lat, truck.lon),
                    trailer_yard=yard_name(trailer.lat, trailer.lon),
                )
    return best


def _hour_status(
    *,
    truck_points: list[Point],
    trailer_points: list[Point],
    best_tolerant: BestPair | None,
    best_any: BestPair | None,
    max_distance_miles: float,
    include_same_yard_as_paired: bool,
) -> str:
    if not truck_points and not trailer_points:
        return "no_data"
    if not truck_points:
        return "missing_truck"
    if not trailer_points:
        return "missing_trailer"
    if best_tolerant is None:
        if best_any and best_any.distance_miles <= max_distance_miles:
            return "close_but_async"
        return "both_data_no_time_overlap"
    if best_tolerant.distance_miles > max_distance_miles:
        return "far"
    if (
        best_tolerant.truck_yard
        and best_tolerant.trailer_yard
        and best_tolerant.truck_yard == best_tolerant.trailer_yard
        and not include_same_yard_as_paired
    ):
        return "same_yard"
    return "paired"


def _load_pair_history(
    *,
    supabase_url: str,
    supabase_key: str,
    truck_id: str,
    trailer_id: str,
    start: datetime,
    end: datetime,
    hard_cap: int,
) -> list[dict[str, Any]]:
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }
    base_url = f"{supabase_url}/rest/v1/assets_history"
    select = "asset_type,asset_id,division,lat,lon,speed,heading_deg,recorded_at,address,provider"
    params_base = {
        "select": select,
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
        "or": f"(and(asset_type.eq.truck,asset_id.eq.{truck_id}),and(asset_type.eq.trailer,asset_id.eq.{trailer_id}))",
        "order": "recorded_at.asc",
    }

    out: list[dict[str, Any]] = []
    offset = 0
    while offset < hard_cap:
        params = {**params_base, "limit": PAGE_SIZE, "offset": offset}
        response = requests.get(base_url, headers=headers, params=params, timeout=60)
        if not response.ok:
            raise RuntimeError(f"Supabase assets_history query failed: HTTP {response.status_code} {response.text[:500]}")
        page = response.json()
        if not isinstance(page, list):
            break
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        print(f"  loaded {len(out):,} rows...", flush=True)
        time.sleep(0.03)
    if offset >= hard_cap:
        print(f"WARNING: hard cap of {hard_cap:,} rows reached; report may be incomplete.")
    return out


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
        speed=_to_float(row.get("speed")),
        heading_deg=_to_float(row.get("heading_deg")),
        division=str(row.get("division") or ""),
        provider=str(row.get("provider") or ""),
        address=str(row.get("address") or ""),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an hour-by-hour truck/trailer GPS history report from Supabase.")
    parser.add_argument("--truck", required=True, help="Truck unit id, e.g. 129")
    parser.add_argument("--trailer", required=True, help="Trailer unit id, e.g. 759012")
    parser.add_argument("--days", type=int, default=60, help="Days back from now if --start/--end are omitted (default: 60)")
    parser.add_argument("--start", help="Start date/datetime UTC, e.g. 2026-06-01 or 2026-06-01T00:00:00Z")
    parser.add_argument("--end", help="End date/datetime UTC, e.g. 2026-06-30 or 2026-06-30T23:59:59Z")
    parser.add_argument("--max-distance", type=float, default=0.5, help="Distance threshold in miles for paired evidence (default: 0.5)")
    parser.add_argument("--max-ping-gap-minutes", type=float, default=45, help="Max time gap between nearest pings in an hour (default: 45)")
    parser.add_argument("--include-same-yard", action="store_true", help="Count same-yard close hours as paired instead of same_yard")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for CSV outputs")
    parser.add_argument("--hard-cap", type=int, default=250000, help="Max REST rows to load before stopping")
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
    if not text:
        raise ValueError("Empty date")
    if "T" not in text and len(text) == 10:
        d = date.fromisoformat(text)
        if is_end:
            return datetime.combine(d, datetime.max.time(), tzinfo=timezone.utc)
        return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
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
        except Exception as exc:  # noqa: BLE001 - continue to env fallback
            print(f"WARNING: could not read {path.name}: {exc}", file=sys.stderr)

    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    to_rad = math.pi / 180
    d_lat = (lat2 - lat1) * to_rad
    d_lon = (lon2 - lon1) * to_rad
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1 * to_rad) * math.cos(lat2 * to_rad) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_MILES * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def yard_name(lat: float, lon: float) -> str:
    for name, (yard_lat, yard_lon, radius) in YARD_GEOFENCES.items():
        if haversine_miles(lat, lon, yard_lat, yard_lon) <= radius:
            return name
    return ""


def _floor_hour(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(minute=0, second=0, microsecond=0)


def _hour_range(start: datetime, end: datetime) -> list[datetime]:
    hours: list[datetime] = []
    current = _floor_hour(start)
    final = _floor_hour(end)
    while current <= final:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _round_or_none(value: float | None, ndigits: int) -> float | None:
    return round(float(value), ndigits) if value is not None else None


def _mode(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value)) or "unit"


def _print_summary(hourly: pl.DataFrame, daily: pl.DataFrame, hourly_path: Path, daily_path: Path) -> None:
    paired_hours = int(hourly.filter(pl.col("status") == "paired").height)
    same_yard_hours = int(hourly.filter(pl.col("status") == "same_yard").height)
    close_async_hours = int(hourly.filter(pl.col("status") == "close_but_async").height)
    both_data_hours = int(hourly.filter((pl.col("truck_pings") > 0) & (pl.col("trailer_pings") > 0)).height)
    print("\nSummary")
    print(f"  Hours in report:        {hourly.height:,}")
    print(f"  Hours with both data:   {both_data_hours:,}")
    print(f"  Paired evidence hours:  {paired_hours:,}")
    print(f"  Same-yard close hours:  {same_yard_hours:,}")
    print(f"  Close but async hours:  {close_async_hours:,}")
    print("\nDaily summary preview:")
    print(daily.tail(14))
    print("\nWrote:")
    print(f"  {hourly_path}")
    print(f"  {daily_path}")


if __name__ == "__main__":
    main()