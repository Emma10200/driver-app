#!/usr/bin/env python3
"""
Bulk GPS history backfill into Supabase assets_history.

Supports:
  - Anytrek trailers (transactions.json with startTime/endTime)
  - GPSTab trucks (/api/location/vehicle with startTime/endTime, paginated)

Run locally on your work PC (not in Streamlit Cloud):

    # Backfill both providers, 60 days
    python scripts/backfill_anytrek_history.py --days 60

    # Anytrek only
    python scripts/backfill_anytrek_history.py --days 60 --provider anytrek

    # GPSTab only
    python scripts/backfill_anytrek_history.py --days 60 --provider gpstab

Uses secrets from .streamlit/secrets.toml, .env, or environment variables.
Safe to re-run: inserts are append-only into assets_history.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ANYTREK_TX_URL = "https://api.anytrek.com/v2/api/transactions.json"
CHUNK_DAYS = 3  # Pull in 3-day chunks (Anytrek caps at 10K per call)
MAX_COUNT_PER_CALL = 10000
SUPABASE_BATCH_SIZE = 500
YARD_FENCES = {
    "California Yard": {"lat": 34.09686, "lon": -117.47642, "radius_miles": 0.25},
    "Illinois Yard": {"lat": 41.896873, "lon": -87.86982, "radius_miles": 0.25},
}


def _load_secrets() -> dict[str, str]:
    """Load secrets from Streamlit secrets.toml, .env, or environment."""
    secrets: dict[str, str] = {}

    # Try Streamlit secrets
    try:
        secrets_path = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
        if secrets_path.exists():
            import tomllib
            with open(secrets_path, "rb") as f:
                data = tomllib.load(f)
            # Flatten sections
            for key, value in data.items():
                if isinstance(value, dict):
                    for k, v in value.items():
                        secrets[k] = str(v)
                else:
                    secrets[key] = str(value)
    except Exception:
        pass

    # Try .env
    try:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and value:
                        secrets[key] = value
    except Exception:
        pass

    # Environment overrides everything
    for key in [
        "ANYTREK_API_KEY",
        "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
        "GPSTAB_PRESTIGE_KEY", "GPSTAB_PRESTIG_KEY", "GPSTAB_XPRESS_KEY",
    ]:
        env_val = os.environ.get(key)
        if env_val:
            secrets[key] = env_val

    return secrets


# ---------------------------------------------------------------------------
# GPSTab history
# ---------------------------------------------------------------------------
GPSTAB_BASE = "https://app.gpstab.com"
GPSTAB_HISTORY_PAGE_SIZE = 5000


def _gpstab_api_keys(secrets: dict[str, str]) -> list[tuple[str, str]]:
    """Return (account_label, api_key) pairs for all GPSTab accounts."""
    keys = []
    for label in ["GPSTAB_PRESTIGE_KEY", "GPSTAB_PRESTIG_KEY", "GPSTAB_XPRESS_KEY"]:
        val = secrets.get(label, "").strip()
        if val:
            keys.append((label, val))
    return keys


def _gpstab_get_vehicles(api_key: str) -> list[dict[str, Any]]:
    """Fetch all vehicles from GPSTab to get internal IDs."""
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    for path in ["/api/v1/vehicle/get/", "/api/v1/vehicle/get"]:
        try:
            resp = requests.get(f"{GPSTAB_BASE}{path}", headers=headers, timeout=30)
            if resp.ok:
                data = resp.json()
                items = data.get("Items") or data.get("items") or []
                if items:
                    return items
        except Exception:
            continue
    return []


def _gpstab_fetch_vehicle_history(
    api_key: str,
    vehicle_id: int,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Fetch location history for a single GPSTab vehicle."""
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    all_items: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "truckId": vehicle_id,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "page": page,
            "size": GPSTAB_HISTORY_PAGE_SIZE,
        }
        try:
            resp = requests.get(
                f"{GPSTAB_BASE}/api/location/vehicle",
                headers=headers,
                params=params,
                timeout=60,
            )
            if not resp.ok:
                break
            data = resp.json()
            items = data.get("Items") or data.get("items") or []
            all_items.extend(items)
            total_pages = data.get("TotalPages", 1)
            if page >= total_pages or not items:
                break
            page += 1
            time.sleep(0.3)
        except Exception as exc:
            print(f"    GPSTab history error: {exc}")
            break
    return all_items


def backfill_gpstab(secrets: dict[str, str], days: int, supabase_url: str, supabase_key: str, dry_run: bool) -> int:
    """Pull GPSTab truck location history and insert into Supabase."""
    accounts = _gpstab_api_keys(secrets)
    if not accounts:
        print("No GPSTab API keys found. Skipping GPSTab backfill.")
        return 0

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    total_inserted = 0

    for account_label, api_key in accounts:
        print(f"\n[GPSTab] Account: {account_label}")
        vehicles = _gpstab_get_vehicles(api_key)
        print(f"  Found {len(vehicles)} vehicles")

        for vehicle in vehicles:
            v_id = vehicle.get("id")
            display_id = str(vehicle.get("vehicleid") or vehicle.get("vehicleId") or "").strip()
            if not v_id or not display_id:
                continue

            print(f"  Truck {display_id} (internal {v_id}):", end=" ", flush=True)

            # Pull in weekly chunks to stay under page limits
            chunk_start = start
            vehicle_rows: list[dict[str, Any]] = []
            while chunk_start < now:
                chunk_end = min(chunk_start + timedelta(days=7), now)
                items = _gpstab_fetch_vehicle_history(api_key, v_id, chunk_start, chunk_end)
                for item in items:
                    lat_str = item.get("Latitude") or item.get("latitude")
                    lon_str = item.get("Longitude") or item.get("longitude")
                    if not lat_str or not lon_str:
                        continue
                    try:
                        lat = float(lat_str)
                        lon = float(lon_str)
                    except (ValueError, TypeError):
                        continue
                    if lat == 0 and lon == 0:
                        continue

                    recorded_at = item.get("Time") or item.get("time")
                    if not recorded_at:
                        continue

                    speed = item.get("Speed") or item.get("speed")
                    heading = item.get("Bearing") or item.get("bearing")

                    vehicle_rows.append({
                        "asset_type": "truck",
                        "asset_id": display_id,
                        "division": "",
                        "lat": lat,
                        "lon": lon,
                        "address": item.get("Address") or item.get("address") or "",
                        "zip": "",
                        "speed": float(speed) if speed is not None else None,
                        "heading_deg": float(heading) if heading is not None else None,
                        "provider": f"GPSTab ({account_label})",
                        "recorded_at": recorded_at,
                        "source": "gpstab_backfill",
                        "raw": {
                            "VehicleId": v_id,
                            "DriverId": item.get("DriverId") or item.get("driverId"),
                            "account": account_label,
                        },
                    })
                chunk_start = chunk_end
                time.sleep(0.3)

            print(f"{len(vehicle_rows)} pings")

            if vehicle_rows and not dry_run:
                inserted = upsert_to_supabase(supabase_url, supabase_key, vehicle_rows)
                total_inserted += inserted
            elif vehicle_rows and dry_run:
                print(f"    DRY RUN: would insert {len(vehicle_rows)} rows")

    return total_inserted


def _format_anytrek_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _canonical_trailer_id(vehicle_name: str) -> str:
    """Extract 6-digit canonical trailer ID from Anytrek vehicleName."""
    match = re.search(r"\d{6}", str(vehicle_name or ""))
    return match.group(0) if match else ""


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    to_rad = math.pi / 180
    d_lat = (lat2 - lat1) * to_rad
    d_lon = (lon2 - lon1) * to_rad
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1 * to_rad) * math.cos(lat2 * to_rad) * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 3958.8 * c


def _in_yard(lat: float, lon: float) -> str:
    for name, fence in YARD_FENCES.items():
        if _haversine_miles(lat, lon, fence["lat"], fence["lon"]) <= fence["radius_miles"]:
            return name
    return ""


def fetch_anytrek_chunk(api_key: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Fetch Anytrek transactions for a date range."""
    params = {
        "key": api_key,
        "startTime": _format_anytrek_time(start),
        "endTime": _format_anytrek_time(end),
        "count": MAX_COUNT_PER_CALL,
    }
    url = f"{ANYTREK_TX_URL}?{urlencode(params)}"
    print(f"  Fetching {start.date()} → {end.date()} ...", end=" ", flush=True)

    resp = requests.post(url, timeout=60)
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}")
        return []

    data = resp.json()
    if not isinstance(data, list):
        print("unexpected response shape")
        return []

    print(f"{len(data)} transactions")
    return data


def transform_transactions(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anytrek transactions to Supabase assets_history rows."""
    rows: list[dict[str, Any]] = []
    for tx in transactions:
        vehicle_name = str(tx.get("vehicleName") or "").strip()
        canonical_id = _canonical_trailer_id(vehicle_name)
        if not canonical_id:
            continue

        lat = tx.get("lat")
        lon = tx.get("lng")
        if lat is None or lon is None or (lat == 0 and lon == 0):
            continue

        recorded_at = tx.get("createTime") or tx.get("reportTime")
        if not recorded_at:
            continue

        speed = tx.get("speed")
        heading = tx.get("heading")
        yard = _in_yard(float(lat), float(lon))

        rows.append({
            "asset_type": "trailer",
            "asset_id": canonical_id,
            "division": "",  # Anytrek doesn't carry division
            "lat": float(lat),
            "lon": float(lon),
            "address": " ".join(filter(None, [
                tx.get("streetAddress"),
                tx.get("city"),
                tx.get("state"),
            ])),
            "zip": tx.get("zip") or "",
            "speed": float(speed) if speed is not None else None,
            "heading_deg": float(heading) if heading is not None else None,
            "provider": "anytrek",
            "recorded_at": recorded_at,
            "source": "anytrek_backfill",
            "raw": {
                "deviceId": tx.get("deviceId"),
                "vehicleName": vehicle_name,
                "canonicalId": canonical_id,
                "battery": tx.get("battery"),
                "voltage": tx.get("voltage"),
                "temp": tx.get("temp"),
                "totalMileage": tx.get("totalMileage"),
                "yard": yard,
            },
        })

    return rows


def upsert_to_supabase(
    supabase_url: str,
    supabase_key: str,
    rows: list[dict[str, Any]],
) -> int:
    """Insert rows into assets_history in batches."""
    total = 0
    url = f"{supabase_url}/rest/v1/assets_history"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
        batch = rows[i : i + SUPABASE_BATCH_SIZE]
        resp = requests.post(url, headers=headers, json=batch, timeout=30)
        if resp.ok:
            total += len(batch)
        else:
            print(f"  Supabase insert failed: HTTP {resp.status_code} — {resp.text[:300]}")
            break

    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill GPS history to Supabase (Anytrek trailers + GPSTab trucks)")
    parser.add_argument("--days", type=int, default=60, help="How many days back to pull (default: 60)")
    parser.add_argument("--provider", choices=["all", "anytrek", "gpstab"], default="all", help="Which provider to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and transform but don't write to Supabase")
    args = parser.parse_args()

    secrets = _load_secrets()
    supabase_url = secrets.get("SUPABASE_URL", "")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY", "")

    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not found.")
        sys.exit(1)

    print(f"GPS history backfill: {args.days} days, provider={args.provider}")
    print(f"Supabase: {supabase_url}")
    print()

    grand_total = 0

    # --- Anytrek (trailers) ---
    if args.provider in ("all", "anytrek"):
        api_key = secrets.get("ANYTREK_API_KEY", "")
        if not api_key:
            print("WARNING: ANYTREK_API_KEY not found. Skipping Anytrek backfill.")
        else:
            print("=" * 60)
            print("ANYTREK TRAILER BACKFILL")
            print("=" * 60)
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=args.days)
            total_inserted = 0
            total_fetched = 0

            chunk_start = start
            while chunk_start < now:
                chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
                transactions = fetch_anytrek_chunk(api_key, chunk_start, chunk_end)
                total_fetched += len(transactions)

                if transactions:
                    rows = transform_transactions(transactions)
                    print(f"    → {len(rows)} valid trailer pings (from {len(transactions)} raw)")

                    if not args.dry_run and rows:
                        inserted = upsert_to_supabase(supabase_url, supabase_key, rows)
                        total_inserted += inserted
                        print(f"    → {inserted} rows inserted into Supabase")
                    elif args.dry_run and rows:
                        print(f"    → DRY RUN: would insert {len(rows)} rows")

                chunk_start = chunk_end
                time.sleep(0.5)

            print(f"\nAnytrek done: {total_fetched} transactions fetched, {total_inserted} rows inserted.")
            grand_total += total_inserted

    # --- GPSTab (trucks) ---
    if args.provider in ("all", "gpstab"):
        print()
        print("=" * 60)
        print("GPSTAB TRUCK BACKFILL")
        print("=" * 60)
        inserted = backfill_gpstab(secrets, args.days, supabase_url, supabase_key, args.dry_run)
        print(f"\nGPSTab done: {inserted} rows inserted.")
        grand_total += inserted

    print()
    print(f"Grand total: {grand_total} rows inserted into Supabase assets_history.")


if __name__ == "__main__":
    main()
