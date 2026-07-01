"""Report active GPS units that need historical last-known location fallback.

This is a dispatcher/data-quality helper. It uses assets_current as the active
roster, then asks services.gps_data to enrich stale or coordinate-less units
from assets_history. It does not update dense evidence or matching tables.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.gps_data import load_current_assets_with_last_known
from services.gps_matching import Asset, in_yard


def main() -> int:
    parser = argparse.ArgumentParser(description="Report stale/missing GPS units and their last-known historical location.")
    parser.add_argument("--stale-days", type=int, default=30, help="Mark current GPS pings older than this as stale.")
    parser.add_argument(
        "--types",
        default="trailer,truck",
        help="Comma-separated asset types to check. Default: trailer,truck",
    )
    parser.add_argument("--all", action="store_true", help="Include current/live units too, not only stale/missing units.")
    parser.add_argument("--output", type=Path, help="Optional CSV path to write the report.")
    args = parser.parse_args()

    asset_types = tuple(t.strip().lower() for t in args.types.split(",") if t.strip())
    assets = load_current_assets_with_last_known(stale_after_days=args.stale_days, asset_types=asset_types)
    rows = [_row_for_asset(asset) for asset in assets]
    if not args.all:
        rows = [row for row in rows if row["location_status"] != "Current GPS"]

    rows.sort(key=lambda row: (row["asset_type"], row["unit_sort"], row["asset_id"]))

    print(f"Active GPS roster rows checked: {len(assets):,}")
    print(f"Rows in report: {len(rows):,}")
    print(f"Historical last-known: {sum(1 for row in rows if row['historical_last_known']):,}")
    print(f"No usable coordinates: {sum(1 for row in rows if not row['coords']):,}")
    print()

    if rows:
        _print_table(rows[:100])
        if len(rows) > 100:
            print(f"... {len(rows) - 100:,} more rows not shown. Use --output to export all rows.")
    else:
        print("No stale/missing active units found with the current threshold.")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        export_rows = [{k: v for k, v in row.items() if k != "unit_sort"} for row in rows]
        with args.output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(export_rows[0].keys()) if export_rows else _csv_fields())
            writer.writeheader()
            writer.writerows(export_rows)
        print(f"\nWrote CSV: {args.output}")

    return 0


def _row_for_asset(asset: Asset) -> dict[str, Any]:
    raw = asset.raw or {}
    last_ping = asset.last_ping.astimezone(timezone.utc) if asset.last_ping else None
    coords = _coords(asset)
    return {
        "asset_type": asset.asset_type,
        "asset_id": asset.asset_id,
        "unit_sort": _unit_sort(asset.asset_id),
        "location_status": str(raw.get("locationStatus") or ("Current GPS" if coords else "No coordinates")),
        "historical_last_known": bool(raw.get("historicalLastKnown")),
        "lookup_reason": str(raw.get("historicalLookupReason") or ""),
        "last_ping_utc": last_ping.isoformat() if last_ping else "",
        "age_days": _age_days(last_ping),
        "coords": coords,
        "google_maps": f"https://www.google.com/maps/search/?api=1&query={coords.replace(' ', '')}" if coords else "",
        "yard": in_yard(float(asset.lat), float(asset.lon)) if _has_coords(asset) else "",
        "provider": asset.provider,
        "division": asset.division,
        "address": asset.address,
        "history_source": str(raw.get("historySource") or ""),
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = ["type", "unit", "status", "age_days", "coords", "provider", "division", "reason"]
    print(" | ".join(headers))
    print(" | ".join("-" * len(h) for h in headers))
    for row in rows:
        print(" | ".join([
            str(row["asset_type"]),
            str(row["asset_id"]),
            str(row["location_status"]),
            str(row["age_days"]),
            str(row["coords"]),
            str(row["provider"]),
            str(row["division"]),
            str(row["lookup_reason"]),
        ]))


def _csv_fields() -> list[str]:
    return [
        "asset_type",
        "asset_id",
        "location_status",
        "historical_last_known",
        "lookup_reason",
        "last_ping_utc",
        "age_days",
        "coords",
        "google_maps",
        "yard",
        "provider",
        "division",
        "address",
        "history_source",
    ]


def _coords(asset: Asset) -> str:
    if not _has_coords(asset):
        return ""
    return f"{float(asset.lat):.6f}, {float(asset.lon):.6f}"


def _has_coords(asset: Asset) -> bool:
    if asset.lat is None or asset.lon is None:
        return False
    try:
        lat = float(asset.lat)
        lon = float(asset.lon)
    except (TypeError, ValueError):
        return False
    return not (lat == 0 and lon == 0)


def _age_days(ping: datetime | None) -> str:
    if ping is None:
        return ""
    days = max(0.0, (datetime.now(timezone.utc) - ping).total_seconds() / 86400)
    return f"{days:.1f}"


def _unit_sort(unit: str) -> int:
    digits = "".join(ch for ch in str(unit) if ch.isdigit())
    return int(digits) if digits else 999999999


if __name__ == "__main__":
    raise SystemExit(main())
