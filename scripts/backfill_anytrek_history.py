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
CHUNK_DAYS = 1  # Pull in 1-day chunks; Anytrek can time out on larger historical windows.
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
        "EROAD_PRESTIGE_KEY", "EROAD_XPRESS_KEY",
        "TRACK888_USER", "TRACK888_PASSWORD",
        "TRACK888_PRESTIGE_USER", "TRACK888_PRESTIGE_PASSWORD", "TRACK888_PRESTIGE_KEY",
        "TRACK888_PRESTIGE_COMPANY",
        "TRACK888_XPRESS_USER", "TRACK888_XPRESS_PASSWORD", "TRACK888_XPRESS_KEY",
        "TRACK888_XPRESS_COMPANY",
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


# ---------------------------------------------------------------------------
# 888 ELD (Track Mile Portal / HOSQL) history
# ---------------------------------------------------------------------------
TRACK888_AUTH_URL = "https://myportal.eldtrackmile.com/auth-service/auth/authentication"
TRACK888_HOSQL_URL = "https://myportal.eldtrackmile.com/hosql/api"


def _track888_companies(secrets: dict[str, str]) -> list[dict[str, str]]:
    """Build company configs from secrets."""
    companies = []
    for label, company_keys, user_keys, pass_keys in [
        ("Prestige",
         ["TRACK888_PRESTIGE_COMPANY", "TRACK888_COMPANY"],
         ["TRACK888_PRESTIGE_USER", "TRACK888_USER"],
         ["TRACK888_PRESTIGE_PASSWORD", "TRACK888_PRESTIGE_KEY", "TRACK888_PASSWORD"]),
        ("Xpress",
         ["TRACK888_XPRESS_COMPANY"],
         ["TRACK888_XPRESS_USER", "TRACK888_USER"],
         ["TRACK888_XPRESS_PASSWORD", "TRACK888_XPRESS_KEY", "TRACK888_PASSWORD"]),
    ]:
        company_id = next((secrets.get(k, "").strip() for k in company_keys if secrets.get(k, "").strip()), "")
        if not company_id:
            continue
        user = next((secrets.get(k, "").strip() for k in user_keys if secrets.get(k, "").strip()), "")
        password = next((secrets.get(k, "").strip() for k in pass_keys if secrets.get(k, "").strip()), "")
        if user and password:
            companies.append({"name": label, "company_id": company_id, "user": user, "password": password})
    return companies


def _track888_authenticate(user: str, password: str) -> str | None:
    """Authenticate to Track Mile portal, return access token. Tries multiple endpoints."""
    endpoints = [
        ("portal", TRACK888_AUTH_URL),
        ("portal-twofa", "https://myportal.eldtrackmile.com/auth-service/auth/twofa/authentication"),
        ("hosconnect", "https://api.hosconnect.com/v1/authentication"),
    ]
    for label, url in endpoints:
        try:
            if "hosconnect" in url:
                payload = {"user": user, "password": password, "company": ""}
            else:
                payload = {"username": user, "password": password}
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
            if resp.status_code in (200, 201):
                data = resp.json()
                token = data.get("access_token") or data.get("accessToken")
                if token:
                    print(f"  Authenticated via {label}")
                    return token
        except Exception as exc:
            print(f"  Auth attempt ({label}) error: {exc}")
    return None


def _track888_auth_with_company(user: str, password: str, company_id: str) -> str | None:
    """Try HOSconnect auth with company ID in payload (legacy flow)."""
    # Try both raw company ID and normalized
    for cid in [company_id, company_id.replace("Company:", "")]:
        try:
            resp = requests.post(
                "https://api.hosconnect.com/v1/authentication",
                json={"user": user, "password": password, "company": cid},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                token = data.get("accessToken") or data.get("access_token")
                if token:
                    print(f"  Authenticated via HOSconnect (company={cid[:12]}...)")
                    return token
        except Exception:
            continue
    return None


def _track888_fetch_collection(token: str, user: str, collection: str, company_id: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Fetch a HOSQL collection. Tries with and without companyId filter."""
    headers = {
        "Authorization": f"Bearer {token}",
        "user": user,
        "Content-Type": "application/json",
    }
    # Try with companyId first, then without (user context may be enough)
    urls_to_try = []
    if company_id:
        urls_to_try.append(f"{TRACK888_HOSQL_URL}/{collection}?%24limit={limit}&companyId={company_id}")
    urls_to_try.append(f"{TRACK888_HOSQL_URL}/{collection}?%24limit={limit}")

    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            if not resp.ok:
                continue
            data = resp.json()
            items: list[dict[str, Any]] = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for key in ("data", "items", "docs", "Data", "Items"):
                    if isinstance(data.get(key), list):
                        items = data[key]
                        break
            if items:
                return items
        except Exception:
            continue
    return []


def backfill_track888(secrets: dict[str, str], days: int, supabase_url: str, supabase_key: str, dry_run: bool) -> int:
    """Pull 888 ELD vehicle statuses (potentially historical) from Track Mile portal."""
    companies = _track888_companies(secrets)
    if not companies:
        print("No 888 ELD company configs found. Skipping.")
        return 0

    total_inserted = 0
    for company in companies:
        print(f"\n[888 ELD] Company: {company['name']} (company_id: {company['company_id'][:15]}...)")
        token = _track888_authenticate(company["user"], company["password"])
        if not token:
            # Also try with company in the payload (HOSconnect style)
            token = _track888_auth_with_company(company["user"], company["password"], company["company_id"])
        if not token:
            print("  Authentication failed on all endpoints. Skipping.")
            continue
        print("  Authenticated OK")

        vehicles = _track888_fetch_collection(token, company["user"], "vehicles", company["company_id"], limit=1000)
        print(f"  Found {len(vehicles)} vehicles")

        # Try vehicle_statuses (historical) — HOSQL may return all statuses
        statuses = _track888_fetch_collection(token, company["user"], "vehicle_statuses", company["company_id"], limit=1000)
        if not statuses:
            statuses = _track888_fetch_collection(token, company["user"], "latest_vehicle_statuses", company["company_id"], limit=1000)
        print(f"  Fetched {len(statuses)} status records")

        # Build vehicle name lookup
        vehicle_names: dict[str, str] = {}
        for v in vehicles:
            vid = v.get("_id") or v.get("id") or ""
            name = v.get("name") or v.get("vehicleName") or v.get("number") or v.get("unit") or ""
            if vid and name:
                vehicle_names[str(vid)] = str(name).strip()

        rows: list[dict[str, Any]] = []
        for status in statuses:
            lat = status.get("lat") or status.get("latitude") or status.get("Latitude")
            lon = status.get("lon") or status.get("lng") or status.get("longitude") or status.get("Longitude")
            if not lat or not lon:
                continue
            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except (ValueError, TypeError):
                continue
            if lat_f == 0 and lon_f == 0:
                continue

            timestamp = (
                status.get("timestamp") or status.get("time") or status.get("stime")
                or status.get("updatedAt") or status.get("createdAt")
            )
            if not timestamp:
                continue

            # Resolve vehicle name
            vehicle_ref = (
                status.get("v") or status.get("vehicleId") or status.get("vehicle_id") or ""
            )
            truck_id = vehicle_names.get(str(vehicle_ref), "")
            if not truck_id:
                # Try VIN match
                vin = status.get("vin") or ""
                for v in vehicles:
                    if v.get("vin") == vin and vin:
                        truck_id = v.get("name") or v.get("number") or ""
                        break
            if not truck_id:
                continue

            # Convert epoch timestamps
            recorded_at = timestamp
            if isinstance(timestamp, (int, float)) and timestamp > 1000000000:
                from datetime import datetime as dt_cls
                if timestamp > 10000000000:
                    timestamp = timestamp / 1000
                recorded_at = dt_cls.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

            address = status.get("c") or status.get("address") or status.get("location") or ""
            speed = status.get("speed") or status.get("speedKph")

            rows.append({
                "asset_type": "truck",
                "asset_id": str(truck_id).strip(),
                "division": company["name"],
                "lat": lat_f,
                "lon": lon_f,
                "address": str(address),
                "zip": "",
                "speed": float(speed) if speed is not None else None,
                "heading_deg": None,
                "provider": f"888 ELD ({company['name']})",
                "recorded_at": recorded_at,
                "source": "track888_backfill",
                "raw": {
                    "vin": status.get("vin"),
                    "vehicle_ref": vehicle_ref,
                    "company": company["name"],
                },
            })

        print(f"  → {len(rows)} valid truck pings")
        if rows and not dry_run:
            inserted = upsert_to_supabase(supabase_url, supabase_key, rows)
            total_inserted += inserted
            print(f"  → {inserted} rows inserted")
        elif rows and dry_run:
            print(f"  → DRY RUN: would insert {len(rows)} rows")

    return total_inserted


# ---------------------------------------------------------------------------
# EROAD history (vehicleCurrentState — snapshot; no known bulk history endpoint)
# ---------------------------------------------------------------------------
EROAD_BASE = "https://api.na.eroad.com/v1"


def backfill_eroad(secrets: dict[str, str], supabase_url: str, supabase_key: str, dry_run: bool) -> int:
    """Pull EROAD current state for all vehicles. EROAD doesn't expose bulk history,
    but each 10-min trigger cycle already appends to assets_history.
    This function grabs the current snapshot as a single data point."""
    total_inserted = 0
    for label in ["EROAD_PRESTIGE_KEY", "EROAD_XPRESS_KEY"]:
        api_key = secrets.get(label, "").strip()
        if not api_key:
            continue
        print(f"\n[EROAD] Account: {label}")
        headers = {"ApiKey": api_key, "Accept": "application/json"}

        # Fetch current state (paginated)
        all_results: list[dict[str, Any]] = []
        first_result = 0
        page_size = 200
        for _ in range(50):
            url = f"{EROAD_BASE}/vehicleCurrentState?firstResult={first_result}&maxResult={page_size}"
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                if not resp.ok:
                    print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                    break
                data = resp.json()
                results = data.get("results") or []
                if not results:
                    break
                all_results.extend(results)
                if len(results) < page_size:
                    break
                first_result += page_size
                time.sleep(0.3)
            except Exception as exc:
                print(f"  Error: {exc}")
                break

        print(f"  Fetched {len(all_results)} vehicle states")
        rows: list[dict[str, Any]] = []
        for item in all_results:
            gps_fix = item.get("gpsFix") or {}
            coord = gps_fix.get("coordinate") or {}
            lat = coord.get("latitude")
            lon = coord.get("longitude")
            if not lat or not lon:
                continue

            timestamp = gps_fix.get("timestamp") or gps_fix.get("time")
            if not timestamp:
                continue

            # Try to get display name
            display_name = item.get("readableLocation") or ""
            speed = gps_fix.get("speedKph")
            heading = gps_fix.get("courseOverGround")
            vehicle_id = item.get("id") or ""

            rows.append({
                "asset_type": "truck",
                "asset_id": str(vehicle_id),  # We'll use EROAD UUID; matching happens by coords
                "division": "",
                "lat": float(lat),
                "lon": float(lon),
                "address": display_name,
                "zip": "",
                "speed": float(speed) if speed is not None else None,
                "heading_deg": float(heading) if heading is not None else None,
                "provider": f"EROAD ({label})",
                "recorded_at": timestamp,
                "source": "eroad_backfill",
                "raw": {
                    "eroad_id": vehicle_id,
                    "account": label,
                    "status": item.get("status"),
                    "engineHours": item.get("engineHours"),
                },
            })

        print(f"  → {len(rows)} valid pings")
        if rows and not dry_run:
            inserted = upsert_to_supabase(supabase_url, supabase_key, rows)
            total_inserted += inserted
            print(f"  → {inserted} rows inserted")
        elif rows and dry_run:
            print(f"  → DRY RUN: would insert {len(rows)} rows")

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

    last_error = ""
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, timeout=180)
            if resp.status_code != 200:
                print(f"HTTP {resp.status_code}")
                return []
            break
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < 3:
                print(f"timeout/error attempt {attempt}; retrying...", end=" ", flush=True)
                time.sleep(2 * attempt)
                continue
            print(f"failed after retries: {last_error[:180]}")
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


def source_rows_exist(
    supabase_url: str,
    supabase_key: str,
    source: str,
    start: datetime,
    end: datetime,
) -> bool:
    """Return True if a dense backfill source already has rows in a time chunk."""
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }
    resp = requests.get(
        f"{supabase_url}/rest/v1/assets_history",
        headers=headers,
        params={
            "select": "id",
            "source": f"eq.{source}",
            "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
            "limit": "1",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"    WARNING: existing-row check failed: HTTP {resp.status_code} {resp.text[:120]}")
        return False
    rows = resp.json()
    return isinstance(rows, list) and bool(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill GPS history to Supabase (Anytrek trailers + GPSTab trucks)")
    parser.add_argument("--days", type=int, default=60, help="How many days back to pull (default: 60)")
    parser.add_argument("--provider", choices=["all", "anytrek", "gpstab", "track888", "eroad"], default="all", help="Which provider to backfill")
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
                if source_rows_exist(supabase_url, supabase_key, "anytrek_backfill", chunk_start, chunk_end):
                    print(f"  Skipping {chunk_start.date()} → {chunk_end.date()} (Anytrek rows already exist)")
                    chunk_start = chunk_end
                    continue

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

    # --- 888 ELD (trucks via Track Mile portal) ---
    if args.provider in ("all", "track888"):
        print()
        print("=" * 60)
        print("888 ELD TRUCK BACKFILL (Track Mile Portal)")
        print("=" * 60)
        inserted = backfill_track888(secrets, args.days, supabase_url, supabase_key, args.dry_run)
        print(f"\n888 ELD done: {inserted} rows inserted.")
        grand_total += inserted

    # --- EROAD (trucks) ---
    if args.provider in ("all", "eroad"):
        print()
        print("=" * 60)
        print("EROAD TRUCK BACKFILL")
        print("=" * 60)
        inserted = backfill_eroad(secrets, supabase_url, supabase_key, args.dry_run)
        print(f"\nEROAD done: {inserted} rows inserted.")
        grand_total += inserted

    print()
    print(f"Grand total: {grand_total} rows inserted into Supabase assets_history.")


if __name__ == "__main__":
    main()
