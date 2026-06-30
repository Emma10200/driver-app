#!/usr/bin/env python3
"""
Compute asset pairings from GPS history and populate the asset_pairings table.

Reads all GPS history from Supabase assets_history, runs the timeline
computation for every trailer, and inserts the truck↔trailer pairing
segments into asset_pairings.

Run locally:
    python scripts/compute_pairings.py --days 7
    python scripts/compute_pairings.py --days 30 --dry-run

Safe to re-run: truncates existing pairings in the date range before inserting.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.gps_matching import Asset, compute_unit_timeline, _build_time_index


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPABASE_BATCH_SIZE = 500


def _load_secrets() -> dict[str, str]:
    """Load secrets from .streamlit/secrets.toml, .env, or environment."""
    secrets: dict[str, str] = {}
    try:
        secrets_path = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
        if secrets_path.exists():
            import tomllib
            with open(secrets_path, "rb") as f:
                data = tomllib.load(f)
            for key, value in data.items():
                if isinstance(value, dict):
                    for k, v in value.items():
                        secrets[k] = str(v)
                else:
                    secrets[key] = str(value)
    except Exception:
        pass

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

    for key in ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]:
        env_val = os.environ.get(key)
        if env_val:
            secrets[key] = env_val
    return secrets


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def _supabase_headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _load_history(
    supabase_url: str,
    supabase_key: str,
    start: datetime,
    end: datetime,
) -> list[Asset]:
    """Load all GPS history rows from Supabase in the given time range."""
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "return=representation"
    base_url = f"{supabase_url}/rest/v1/assets_history"

    all_rows: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000

    # Use PostgREST range filter
    params_base = {
        "select": "asset_type,asset_id,division,lat,lon,speed,heading_deg,recorded_at,provider",
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
        "order": "recorded_at.asc",
    }

    print(f"  Loading history from {start.date()} to {end.date()}...", flush=True)
    while True:
        params = {**params_base, "limit": page_size, "offset": offset}
        resp = requests.get(base_url, headers=headers, params=params, timeout=60)
        if not resp.ok:
            print(f"  Error loading history: HTTP {resp.status_code}")
            break
        page = resp.json()
        if not isinstance(page, list):
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        if offset % 10000 == 0:
            print(f"    ... {offset:,} rows loaded", flush=True)
        time.sleep(0.05)

    print(f"  Loaded {len(all_rows)} history rows")

    # Convert to Asset objects
    assets: list[Asset] = []
    for row in all_rows:
        lat = row.get("lat")
        lon = row.get("lon")
        if not lat or not lon:
            continue
        recorded_at = row.get("recorded_at")
        if not recorded_at:
            continue
        try:
            last_ping = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        assets.append(Asset(
            asset_type=row.get("asset_type", ""),
            asset_id=row.get("asset_id", ""),
            division=row.get("division", ""),
            lat=float(lat),
            lon=float(lon),
            speed=float(row["speed"]) if row.get("speed") is not None else None,
            heading_deg=float(row["heading_deg"]) if row.get("heading_deg") is not None else None,
            last_ping=last_ping,
            provider=row.get("provider", ""),
        ))

    return assets


def _delete_pairings_in_range(
    supabase_url: str,
    supabase_key: str,
    start: datetime,
    end: datetime,
) -> int:
    """Delete existing auto-computed pairings in the date range (idempotent re-run)."""
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "return=representation"
    url = f"{supabase_url}/rest/v1/asset_pairings"
    params = {
        "and": f"(start_time.gte.{start.isoformat()},start_time.lte.{end.isoformat()})",
        "source": "eq.auto",
    }
    resp = requests.delete(url, headers=headers, params=params, timeout=30)
    if resp.ok:
        deleted = resp.json() if resp.content else []
        count = len(deleted) if isinstance(deleted, list) else 0
        return count
    return 0


def _insert_pairings(
    supabase_url: str,
    supabase_key: str,
    rows: list[dict[str, Any]],
) -> int:
    """Insert pairing rows into asset_pairings in batches."""
    headers = _supabase_headers(supabase_key)
    url = f"{supabase_url}/rest/v1/asset_pairings"
    total = 0

    for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
        batch = rows[i:i + SUPABASE_BATCH_SIZE]
        resp = requests.post(url, headers=headers, json=batch, timeout=30)
        if resp.ok:
            total += len(batch)
        else:
            print(f"  Insert failed: HTTP {resp.status_code} — {resp.text[:300]}")
            break

    return total


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------
def compute_and_store_pairings(
    supabase_url: str,
    supabase_key: str,
    days: int,
    max_distance_miles: float,
    min_duration_minutes: int,
    dry_run: bool,
) -> int:
    """Load history, compute timelines for all trailers, store pairings."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # Load all history
    history = _load_history(supabase_url, supabase_key, start, now)
    if not history:
        print("No history data found. Nothing to compute.")
        return 0

    # Identify all trailers in the history
    trailer_ids = sorted({a.asset_id for a in history if a.asset_type == "trailer"})
    truck_ids = sorted({a.asset_id for a in history if a.asset_type == "truck"})
    print(f"\n  Found {len(trailer_ids)} trailers, {len(truck_ids)} trucks in history")

    print("  Building time index once for all trailers...", flush=True)
    time_index = _build_time_index(history)
    print(f"  Built {len(time_index):,} time buckets")

    # Compute timeline for each trailer
    all_pairing_rows: list[dict[str, Any]] = []
    computed_at = now.isoformat()

    for i, trailer_id in enumerate(trailer_ids):
        print(f"  [{i + 1}/{len(trailer_ids)}] Trailer {trailer_id}:", end=" ", flush=True)

        segments = compute_unit_timeline(
            trailer_id, "trailer", history,
            max_distance_miles=max_distance_miles,
            time_index=time_index,
        )

        # Only store truck-paired segments above minimum duration
        truck_segments = [
            s for s in segments
            if s.partner_type == "truck" and s.duration_minutes >= min_duration_minutes
        ]
        print(f"{len(segments)} segments ({len(truck_segments)} with trucks >= {min_duration_minutes}min)")

        for seg in truck_segments:
            # Determine what ended this segment
            seg_idx = segments.index(seg)
            if seg_idx + 1 < len(segments):
                next_seg = segments[seg_idx + 1]
                if next_seg.partner_type == "yard":
                    ended_by = "yard_entry"
                elif next_seg.partner_type == "truck":
                    ended_by = "new_pairing"
                else:
                    ended_by = "signal_loss"
            else:
                ended_by = None  # Still ongoing or end of data

            all_pairing_rows.append({
                "truck_id": seg.partner_id,
                "trailer_id": trailer_id,
                "start_time": seg.start.isoformat(),
                "end_time": seg.end.isoformat(),
                "duration_minutes": seg.duration_minutes,
                "avg_distance_miles": seg.avg_distance_miles,
                "confidence": seg.confidence,
                "bucket_count": seg.bucket_count,
                "ended_by": ended_by,
                "division": "",
                "computed_at": computed_at,
                "source": "auto",
            })

    print(f"\n  Total pairing segments to store: {len(all_pairing_rows)}")

    if not all_pairing_rows:
        print("  No truck-paired segments found.")
        return 0

    if dry_run:
        print(f"  DRY RUN: would insert {len(all_pairing_rows)} rows into asset_pairings")
        # Print sample
        for row in all_pairing_rows[:10]:
            dur = f"{row['duration_minutes']:.0f}min"
            print(f"    Truck {row['truck_id']:6s} ↔ Trailer {row['trailer_id']:6s} | "
                  f"{row['start_time'][:16]} → {row['end_time'][:16]} | {dur} | "
                  f"conf={row['confidence']:.0%} | ended_by={row['ended_by']}")
        if len(all_pairing_rows) > 10:
            print(f"    ... and {len(all_pairing_rows) - 10} more")
        return 0

    # Delete existing auto pairings in this range (idempotent)
    deleted = _delete_pairings_in_range(supabase_url, supabase_key, start, now)
    if deleted:
        print(f"  Deleted {deleted} existing auto pairings in range")

    # Insert new pairings
    inserted = _insert_pairings(supabase_url, supabase_key, all_pairing_rows)
    print(f"  Inserted {inserted} pairing rows into asset_pairings")
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute and store asset pairings from GPS history")
    parser.add_argument("--days", type=int, default=7, help="How many days back to analyze (default: 7)")
    parser.add_argument("--max-dist", type=float, default=0.5, help="Max distance for co-location (miles, default: 0.5)")
    parser.add_argument("--min-minutes", type=int, default=15, help="Min segment duration to store (default: 15)")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write to Supabase")
    args = parser.parse_args()

    secrets = _load_secrets()
    supabase_url = secrets.get("SUPABASE_URL", "")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY", "")

    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not found.")
        sys.exit(1)

    print(f"Asset pairings computation: {args.days} days, max_dist={args.max_dist} mi, min_duration={args.min_minutes} min")
    print(f"Supabase: {supabase_url}\n")

    total = compute_and_store_pairings(
        supabase_url, supabase_key, args.days, args.max_dist, args.min_minutes, args.dry_run,
    )
    print(f"\nDone. {total} pairing rows stored.")


if __name__ == "__main__":
    main()
