"""
GPS data access: reads assets_current, assets_history, and dispatch_assignments
from Supabase. Uses the same SupabaseRestClient pattern as the rest of the app.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from services.gps_matching import Asset

logger = logging.getLogger(__name__)

_CARDINAL_TO_DEG = {
    "N": 0,
    "NE": 45,
    "E": 90,
    "SE": 135,
    "S": 180,
    "SW": 225,
    "W": 270,
    "NW": 315,
}


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


def load_asset_history(hours: int = 48, division: str | None = None) -> list[Asset]:
    """Load recent GPS history for evidence-based route/co-location matching."""
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    return load_asset_history_range(since, datetime.now(timezone.utc), division=division)


def load_asset_history_range(
    start: datetime,
    end: datetime,
    division: str | None = None,
) -> list[Asset]:
    """Load GPS history in an explicit time range.

    Division filtering is intentionally done after loading so older backfilled
    GPS_HISTORY rows with blank division can still contribute evidence.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("GPS history unavailable (Supabase not configured): %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {"recorded_at": f"gte.{start.isoformat()}"}

    rows = client.select_all(
        "assets_history",
        filters=filters,
        order="recorded_at.asc",
        page_size=1000,
        hard_cap=20000,
    )
    history = [point for point in (_history_row_to_asset(r) for r in rows) if point.last_ping and point.last_ping <= end]
    if division:
        history = [point for point in history if not point.division or point.division == division]
    return history


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

    raw = _parse_raw(row.get("raw"))

    return Asset(
        asset_type=row.get("asset_type", ""),
        asset_id=row.get("asset_id", ""),
        division=row.get("division", ""),
        lat=row.get("lat"),
        lon=row.get("lon"),
        speed=row.get("speed"),
        heading_deg=_parse_heading(row),
        last_ping=last_ping,
        address=row.get("address", ""),
        zip=row.get("zip", ""),
        provider=row.get("provider", ""),
        raw=raw,
    )


def _history_row_to_asset(row: dict[str, Any]) -> Asset:
    last_ping = None
    raw_ping = row.get("recorded_at")
    if raw_ping:
        try:
            last_ping = datetime.fromisoformat(str(raw_ping).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    raw = _parse_raw(row.get("raw"))
    return Asset(
        asset_type=row.get("asset_type", ""),
        asset_id=row.get("asset_id", ""),
        division=row.get("division", ""),
        lat=row.get("lat"),
        lon=row.get("lon"),
        speed=row.get("speed"),
        heading_deg=_to_float(row.get("heading_deg")),
        last_ping=last_ping,
        address=row.get("address", ""),
        zip=row.get("zip", ""),
        provider=row.get("provider", ""),
        raw=raw,
    )


def _parse_raw(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _parse_heading(row: dict[str, Any]) -> float | None:
    numeric = _to_float(row.get("heading_deg"))
    if numeric is not None:
        return numeric

    cardinal = str(row.get("heading_cardinal") or "").strip().upper()
    if cardinal in _CARDINAL_TO_DEG:
        return float(_CARDINAL_TO_DEG[cardinal])
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
