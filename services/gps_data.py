"""
GPS data access: reads assets_current, assets_history, and dispatch_assignments
from Supabase. Uses the same SupabaseRestClient pattern as the rest of the app.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from services.gps_matching import Asset, TimelineSegment

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

    With dense backfill data (~50K pings/day), this uses server-side time
    range filtering and a 500K hard cap to avoid truncating the dataset.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("GPS history unavailable (Supabase not configured): %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    # PostgREST range filter — single 'and' key avoids dict duplicate-key issues
    # Only use dense backfill sources (not sparse dispatch board snapshots)
    filters: dict[str, Any] = {
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
        "or": "(source.eq.gpstab_backfill,source.eq.anytrek_backfill,source.eq.track888_backfill,source.eq.eroad_backfill)",
    }

    rows = client.select_all(
        "assets_history",
        filters=filters,
        order="recorded_at.asc",
        page_size=1000,
        hard_cap=500000,
    )
    history = [_history_row_to_asset(r) for r in rows]
    history = [p for p in history if p.last_ping is not None]
    if division:
        history = [p for p in history if not p.division or p.division == division]
    return history


def load_assignments() -> dict[str, str]:
    """Load dispatch_assignments as a dict of truck_id -> trailer_id."""
    try:
        client = _get_client()
    except Exception:
        return {}

    rows = client.select("dispatch_assignments", select="truck_id,trailer_id")
    return {r["truck_id"]: r["trailer_id"] for r in rows if r.get("truck_id")}


def load_unit_timeline_history(
    unit_id: str,
    start: datetime,
    end: datetime,
) -> list[Asset]:
    """Load history for a specific unit AND all potential partners in its time range.

    This is optimized for the timeline view: we need the selected unit's full
    trail plus all other assets in the same time window to determine pairings.
    Uses server-side time filtering for speed.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("GPS history unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
        "or": "(source.eq.gpstab_backfill,source.eq.anytrek_backfill,source.eq.track888_backfill,source.eq.eroad_backfill)",
    }

    rows = client.select_all(
        "assets_history",
        filters=filters,
        order="recorded_at.asc",
        page_size=1000,
        hard_cap=500000,
    )
    history = [_history_row_to_asset(r) for r in rows]
    return [p for p in history if p.last_ping is not None]


def load_all_unit_ids() -> dict[str, list[str]]:
    """Return all known unit IDs grouped by type from assets_current."""
    try:
        client = _get_client()
    except Exception:
        return {"truck": [], "trailer": []}

    rows = client.select("assets_current", select="asset_type,asset_id")
    result: dict[str, list[str]] = {"truck": [], "trailer": []}
    for r in rows:
        atype = r.get("asset_type", "")
        aid = r.get("asset_id", "")
        if atype in result and aid:
            result[atype].append(aid)
    for v in result.values():
        v.sort()
    return result


def load_asset_pairing_timeline(
    unit_id: str,
    unit_type: str,
    start: datetime,
    end: datetime,
) -> list[TimelineSegment]:
    """Load a pre-computed timeline from asset_pairings for one unit.

    This is the fast path for the Unit Timeline tab. It avoids loading hundreds
    of thousands of raw GPS pings and instead reads the compact pairing table.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Asset pairings unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    unit_col = "truck_id" if unit_type == "truck" else "trailer_id"
    filters: dict[str, Any] = {
        unit_col: f"eq.{unit_id}",
        "start_time": f"lte.{end.isoformat()}",
        "or": f"(end_time.gte.{start.isoformat()},end_time.is.null)",
    }

    try:
        rows = client.select_all(
            "asset_pairings",
            filters=filters,
            order="start_time.asc",
            page_size=1000,
            hard_cap=10000,
        )
    except Exception as e:
        logger.info("Asset pairings timeline unavailable: %s", e)
        return []

    segments: list[TimelineSegment] = []
    for row in rows:
        start_time = _parse_dt(row.get("start_time"))
        end_time = _parse_dt(row.get("end_time")) or end
        if start_time is None:
            continue
        # Clip display range to the selected window.
        seg_start = max(start_time, start)
        seg_end = min(end_time, end)
        if seg_end <= seg_start:
            continue

        if unit_type == "truck":
            partner_type = "trailer"
            partner_id = str(row.get("trailer_id") or "")
        else:
            partner_type = "truck"
            partner_id = str(row.get("truck_id") or "")
        if not partner_id:
            continue

        duration = (seg_end - seg_start).total_seconds() / 60.0
        segments.append(TimelineSegment(
            unit_id=unit_id,
            unit_type=unit_type,
            partner_id=partner_id,
            partner_type=partner_type,
            start=seg_start,
            end=seg_end,
            duration_minutes=round(float(row.get("duration_minutes") or duration), 1),
            avg_distance_miles=round(float(row.get("avg_distance_miles") or 0), 3),
            bucket_count=int(row.get("bucket_count") or 0),
            confidence=round(float(row.get("confidence") or 0), 3),
        ))
    return segments


def load_hourly_evidence_timeline(
    unit_id: str,
    unit_type: str,
    start: datetime,
    end: datetime,
) -> list[TimelineSegment]:
    """Load hour-by-hour evidence from asset_pair_hourly_evidence for one unit.

    This is the new detailed path for the Unit Timeline tab. Each hourly row
    becomes a 1-hour TimelineSegment, giving a full 24-hour view per day.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Hourly evidence unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    unit_col = "truck_id" if unit_type == "truck" else "trailer_id"
    filters: dict[str, Any] = {
        unit_col: f"eq.{unit_id}",
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }

    try:
        rows = client.select_all(
            "asset_pair_hourly_evidence",
            filters=filters,
            order="hour_start.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Hourly evidence timeline unavailable: %s", e)
        return []

    from datetime import timedelta as _td

    segments: list[TimelineSegment] = []
    for row in rows:
        hour_start = _parse_dt(row.get("hour_start"))
        if hour_start is None:
            continue
        hour_end = hour_start + _td(hours=1)

        status = str(row.get("status") or "")
        if unit_type == "truck":
            partner_type = "trailer" if status != "same_yard" else "yard"
            partner_id = str(row.get("trailer_id") or "")
        else:
            partner_type = "truck" if status != "same_yard" else "yard"
            partner_id = str(row.get("truck_id") or "")
        if not partner_id:
            continue

        if status == "same_yard":
            partner_type = "yard"
            yard = str(row.get("truck_yard") or row.get("trailer_yard") or "Yard")
            partner_id = yard

        segments.append(TimelineSegment(
            unit_id=unit_id,
            unit_type=unit_type,
            partner_id=partner_id,
            partner_type=partner_type,
            start=hour_start,
            end=hour_end,
            duration_minutes=60.0,
            avg_distance_miles=round(float(row.get("best_distance_miles") or 0), 3),
            bucket_count=int(row.get("truck_pings") or 0) + int(row.get("trailer_pings") or 0),
            confidence=round(float(row.get("confidence") or 0), 3),
        ))
    return segments


def load_usage_daily_summary(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Load precomputed dense truck↔trailer daily usage summaries."""
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Daily usage summary unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {
        "service_date": f"gte.{start.date().isoformat()}",
        "and": f"(service_date.lte.{end.date().isoformat()},source.eq.auto)",
    }
    try:
        return client.select_all(
            "asset_pair_daily_summary",
            filters=filters,
            order="service_date.desc,trailer_id.asc,truck_id.asc",
            page_size=1000,
            hard_cap=100000,
        )
    except Exception as e:
        logger.info("Daily usage summary query unavailable: %s", e)
        return []


def load_latest_pairing_job() -> dict[str, Any] | None:
    """Return the latest dense hourly-evidence job metadata."""
    try:
        client = _get_client()
    except Exception:
        return None
    try:
        rows = client.select(
            "gps_pairing_job_runs",
            filters={"job_type": "eq.hourly_evidence"},
            order="started_at.desc",
            limit=1,
        )
    except Exception as e:
        logger.info("Pairing job metadata unavailable: %s", e)
        return None
    return rows[0] if rows else None


def load_match_reviews(match_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load saved review decisions keyed by match_id."""
    if not match_ids:
        return {}
    try:
        client = _get_client()
    except Exception:
        return {}

    safe_ids = [str(mid) for mid in match_ids if mid]
    if not safe_ids:
        return {}
    # PostgREST in-filter; match IDs use only unit ids plus '__', no commas.
    in_value = "in.(" + ",".join(quote(mid, safe="") for mid in safe_ids) + ")"
    try:
        rows = client.select("gps_match_reviews", filters={"match_id": in_value})
    except Exception as e:
        logger.info("GPS match reviews unavailable: %s", e)
        return {}
    return {str(row.get("match_id", "")): row for row in rows if row.get("match_id")}


def save_match_reviews(rows: list[dict[str, Any]]) -> int:
    """Upsert review decisions. Returns number of rows saved."""
    if not rows:
        return 0
    client = _get_client()
    client.upsert("gps_match_reviews", rows, on_conflict="match_id")
    return len(rows)


def load_recent_match_reviews(limit: int = 1000) -> list[dict[str, Any]]:
    """Load recent saved reviews for export/debugging."""
    try:
        client = _get_client()
    except Exception:
        return []
    try:
        return client.select(
            "gps_match_reviews",
            order="reviewed_at.desc",
            limit=max(1, min(int(limit), 5000)),
        )
    except Exception as e:
        logger.info("GPS match review export unavailable: %s", e)
        return []


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


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _ensure_aware(parsed)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
