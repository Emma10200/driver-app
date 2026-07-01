#!/usr/bin/env python3
"""One-off: delete sparse legacy GPS rows from assets_history.

Removes rows with source in ('truck_publish', 'trailer_publish', 'gps_update')
which came from the dispatch board 15-min snapshots. These are too sparse for
pairing logic. Dense backfill data (gpstab_backfill, anytrek_backfill, etc.)
remains untouched.

assets_current table is NOT touched — it continues to serve live map positions.
"""
from __future__ import annotations
import requests
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def secrets():
    s = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
        l = line.strip()
        if not l or l.startswith("#") or "=" not in l:
            continue
        k, _, v = l.partition("=")
        s[k.strip()] = v.strip().strip('"').strip("'")
    return s

def main():
    sec = secrets()
    url = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_KEY") or sec.get("SUPABASE_KEY")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Prefer": "count=exact",
    }

    legacy_sources = ["truck_publish", "trailer_publish", "gps_update"]

    for source in legacy_sources:
        # Count first
        r = requests.get(
            f"{url}/rest/v1/assets_history",
            headers={**headers, "Range-Unit": "items", "Range": "0-0"},
            params={"source": f"eq.{source}"},
            timeout=30,
        )
        count = r.headers.get("Content-Range", "?")
        print(f"source={source}: {count}")

    print("\nDeleting legacy sparse rows...")
    for source in legacy_sources:
        r = requests.delete(
            f"{url}/rest/v1/assets_history",
            headers={**headers, "Content-Type": "application/json"},
            params={"source": f"eq.{source}"},
            timeout=120,
        )
        if r.ok:
            cr = r.headers.get("Content-Range", "?")
            print(f"  Deleted source={source}: {cr}")
        else:
            print(f"  FAILED source={source}: HTTP {r.status_code} {r.text[:300]}")

    # Verify what remains
    print("\n=== Remaining data ===")
    r = requests.get(
        f"{url}/rest/v1/assets_history",
        headers={**headers, "Range-Unit": "items", "Range": "0-0"},
        params={},
        timeout=30,
    )
    print(f"Total remaining rows: {r.headers.get('Content-Range', '?')}")

    # Breakdown by source
    for source in ["gpstab_backfill", "anytrek_backfill", "track888_backfill", "eroad_backfill"]:
        r = requests.get(
            f"{url}/rest/v1/assets_history",
            headers={**headers, "Range-Unit": "items", "Range": "0-0"},
            params={"source": f"eq.{source}"},
            timeout=30,
        )
        print(f"  {source}: {r.headers.get('Content-Range', '?')}")

if __name__ == "__main__":
    main()
