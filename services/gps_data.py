"""
GPS data access: reads assets_current, assets_history, and dispatch_assignments
from Supabase. Uses the same SupabaseRestClient pattern as the rest of the app.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from services.gps_matching import Asset

logger = logging.getLogger(__name__)


def _get_client():
    """Lazily import to avoid circular deps and allow the app to run without Supabase configured."""
    from services.qbo_supabase import SupabaseRestClient
    return SupabaseRestClient()


def load_current_assets(division: str | None = None) -> list[Asset]:
    """Load all rows from assets_current, optionally filtered by division."""
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("GPS data unavailable (Supabase not configured): %s", e)
        return []

    filters: dict[str, Any] = {}
    if division:
        filters["division"] = f"eq.{division}"

    rows = client.select("assets_current", filters=filters, order="asset_type.asc,asset_id.asc")
    return [_row_to_asset(r) for r in rows]


def load_assignments() -> dict[str, str]:
    """Load dispatch_assignments as a dict of truck_id -> trailer_id."""
    try:
        client = _get_client()
    except Exception:
        return {}

    rows = client.select("dispatch_assignments", select="truck_id,trailer_id")
    return {r["truck_id"]: r["trailer_id"] for r in rows if r.get("truck_id")}


def _row_to_asset(row: dict[str, Any]) -> Asset:
    last_ping = None
    raw_ping = row.get("last_ping")
    if raw_ping:
        try:
            last_ping = datetime.fromisoformat(raw_ping.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    return Asset(
        asset_type=row.get("asset_type", ""),
        asset_id=row.get("asset_id", ""),
        division=row.get("division", ""),
        lat=row.get("lat"),
        lon=row.get("lon"),
        speed=row.get("speed"),
        heading_deg=row.get("heading_deg"),
        last_ping=last_ping,
        address=row.get("address", ""),
        zip=row.get("zip", ""),
        provider=row.get("provider", ""),
    )
