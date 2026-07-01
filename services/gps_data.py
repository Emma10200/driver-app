"""
GPS data access: reads assets_current, assets_history, and dispatch_assignments
from Supabase. Uses the same SupabaseRestClient pattern as the rest of the app.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
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

    rows = client.select_all("assets_current", filters=filters, order="asset_type.asc,asset_id.asc", page_size=1000, hard_cap=50000)
    return [_row_to_asset(r) for r in rows]


def load_current_assets_with_last_known(
    division: str | None = None,
    *,
    stale_after_days: int = 30,
    asset_types: tuple[str, ...] = ("truck", "trailer"),
) -> list[Asset]:
    """Load active/current assets and enrich missing/stale map rows from assets_history.

    `assets_current` is still the roster of active units shown on the dispatcher
    map. If an active unit has no usable coordinates or its ping is older than
    `stale_after_days`, this function looks up that unit's latest historical
    ping in `assets_history` and uses it as a map-only fallback.

    Important: this is intentionally *not* used by dense matching/evidence. It
    is only a dispatcher visibility aid for dead batteries / stale trailer GPS.
    """
    assets = load_current_assets(division=division)
    if not assets:
        return []

    try:
        client = _get_client()
    except Exception as e:
        logger.info("Last-known GPS lookup unavailable: %s", e)
        return assets

    now = datetime.now(timezone.utc)
    stale_after_days = max(1, int(stale_after_days))
    eligible_types = {t.lower() for t in asset_types}
    enriched: list[Asset] = []

    for asset in assets:
        if asset.asset_type.lower() not in eligible_types:
            enriched.append(asset)
            continue

        missing_coords = not _asset_has_coords(asset)
        stale_ping = _asset_is_stale(asset, now=now, stale_after_days=stale_after_days)
        if not missing_coords and not stale_ping:
            enriched.append(_mark_location_status(asset, "Current GPS"))
            continue

        historical = _load_latest_history_point(client, asset.asset_type, asset.asset_id)
        if historical is None:
            reason = "Missing coordinates" if missing_coords else f"Stale > {stale_after_days} days"
            enriched.append(_mark_location_status(asset, "No historical location found", reason=reason))
            continue

        enriched.append(_merge_last_known(asset, historical, stale_after_days=stale_after_days, now=now))

    return enriched


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
    # PostgREST range filter — single 'and' key avoids dict duplicate-key issues.
    # Use accepted historical sources only: dense backfills plus blank-source
    # historical imports, but not sparse live publisher snapshots.
    filters: dict[str, Any] = {
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
        "or": "(source.eq.gpstab_backfill,source.eq.anytrek_backfill,source.eq.track888_backfill,source.eq.eroad_backfill,source.eq.)",
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

    rows = client.select_all("dispatch_assignments", select="truck_id,trailer_id", page_size=1000, hard_cap=50000)
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
        "or": "(source.eq.gpstab_backfill,source.eq.anytrek_backfill,source.eq.track888_backfill,source.eq.eroad_backfill,source.eq.)",
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

    rows = client.select_all("assets_current", select="asset_type,asset_id", page_size=1000, hard_cap=50000)
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
        "source": "eq.auto",
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
        elif status != "paired":
            # Near/review evidence should remain in the detail table, but it should
            # not draw assignment timeline runs or make a truck look matched.
            continue

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


def load_hourly_evidence_rows(
    unit_id: str,
    unit_type: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Load raw hourly evidence rows for one unit, paged past PostgREST caps."""
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Hourly evidence details unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    unit_col = "truck_id" if unit_type == "truck" else "trailer_id"
    filters: dict[str, Any] = {
        unit_col: f"eq.{unit_id}",
        "source": "eq.auto",
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }

    try:
        return client.select_all(
            "asset_pair_hourly_evidence",
            filters=filters,
            order="hour_start.asc,trailer_id.asc,truck_id.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Hourly evidence detail query unavailable: %s", e)
        return []


# ---------------------------------------------------------------------------
# Yard Mode — aggressive fine-grained proximity analysis
# ---------------------------------------------------------------------------


def load_yard_proximity_pings(
    yard_name: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Load ALL raw GPS pings within a yard's bounding box for a time range.

    No source filter — every provider, every ping type.  Designed for the
    aggressive "Yard Mode" toggle on the Unit Assignment Timeline.
    """
    import math

    from services.gps_matching import YARD_GEOFENCES

    fence = YARD_GEOFENCES.get(yard_name)
    if not fence:
        logger.warning("Unknown yard for yard-mode query: %s", yard_name)
        return []

    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Supabase unavailable for yard pings: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)

    # Convert circular geofence → bounding-box for efficient PostgREST filter.
    radius = fence["radius_miles"]
    lat_delta = radius / 69.0
    lon_delta = radius / (69.0 * max(math.cos(math.radians(fence["lat"])), 0.01))

    lat_min = fence["lat"] - lat_delta
    lat_max = fence["lat"] + lat_delta
    lon_min = fence["lon"] - lon_delta
    lon_max = fence["lon"] + lon_delta

    filters: dict[str, Any] = {
        "and": (
            f"(recorded_at.gte.{start.isoformat()}"
            f",recorded_at.lte.{end.isoformat()}"
            f",lat.gte.{lat_min:.6f}"
            f",lat.lte.{lat_max:.6f}"
            f",lon.gte.{lon_min:.6f}"
            f",lon.lte.{lon_max:.6f})"
        ),
    }

    try:
        return client.select_all(
            "assets_history",
            select=(
                "asset_type,asset_id,lat,lon,speed,heading_deg,"
                "provider,recorded_at,address,source"
            ),
            filters=filters,
            order="recorded_at.asc",
            page_size=1000,
            hard_cap=500000,
        )
    except Exception as e:
        logger.warning("Yard proximity ping query failed: %s", e)
        return []


def build_yard_mode_timeline(
    unit_id: str,
    unit_type: str,
    yard_pings: list[dict[str, Any]],
    bucket_minutes: int = 5,
) -> tuple[list[TimelineSegment], list[dict[str, Any]]]:
    """Build fine-grained yard proximity timeline from raw pings.

    Returns ``(segments, detail_rows)``.

    *segments* are ``TimelineSegment`` objects at ``bucket_minutes`` resolution
    for use with the existing timeline bar renderer.

    *detail_rows* is a list of dicts (one per bucket) with extra fields useful
    for the raw-detail table: distance in feet, provider, ping counts, etc.
    """
    import math
    from collections import defaultdict

    from services.gps_matching import haversine_miles

    # -- Parse pings --------------------------------------------------------
    unit_pings: list[dict[str, Any]] = []
    # other_pings keyed by (asset_type, asset_id) — only opposite type
    other_pings: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    opposite_type = "truck" if unit_type == "trailer" else "trailer"

    for row in yard_pings:
        lat = row.get("lat")
        lon = row.get("lon")
        recorded_at = _parse_dt(row.get("recorded_at"))
        if lat is None or lon is None or recorded_at is None:
            continue

        ping = {
            "asset_type": row.get("asset_type", ""),
            "asset_id": str(row.get("asset_id", "")),
            "lat": float(lat),
            "lon": float(lon),
            "speed": float(row.get("speed") or 0),
            "provider": str(row.get("provider") or ""),
            "recorded_at": recorded_at,
            "address": str(row.get("address") or ""),
        }

        if ping["asset_type"] == unit_type and ping["asset_id"] == unit_id:
            unit_pings.append(ping)
        elif ping["asset_type"] == opposite_type:
            key = (ping["asset_type"], ping["asset_id"])
            other_pings[key].append(ping)

    if not unit_pings:
        return [], []

    # -- Bucket unit pings --------------------------------------------------
    bucket_secs = bucket_minutes * 60
    half_bucket = bucket_secs / 2.0

    # Determine bucket boundaries spanning the unit's presence
    first_ts = unit_pings[0]["recorded_at"].timestamp()
    last_ts = unit_pings[-1]["recorded_at"].timestamp()
    bucket_start_epoch = int(first_ts) // bucket_secs * bucket_secs

    # Build a flat list of other pings sorted by time for scanning
    all_others: list[dict[str, Any]] = []
    for plist in other_pings.values():
        all_others.extend(plist)
    all_others.sort(key=lambda p: p["recorded_at"])

    segments: list[TimelineSegment] = []
    detail_rows: list[dict[str, Any]] = []

    epoch = bucket_start_epoch
    other_idx = 0  # sliding window start for other pings

    while epoch <= last_ts + bucket_secs:
        bucket_end_epoch = epoch + bucket_secs
        bucket_center = epoch + half_bucket

        # Unit pings in this bucket
        bucket_unit = [
            p for p in unit_pings
            if epoch <= p["recorded_at"].timestamp() < bucket_end_epoch
        ]
        if not bucket_unit:
            epoch = bucket_end_epoch
            continue

        # Average unit position in this bucket
        avg_lat = sum(p["lat"] for p in bucket_unit) / len(bucket_unit)
        avg_lon = sum(p["lon"] for p in bucket_unit) / len(bucket_unit)

        # Find nearby other-asset pings in this time window
        # (expand window slightly for ping alignment tolerance)
        window_start = epoch - half_bucket
        window_end = bucket_end_epoch + half_bucket
        candidates: dict[tuple[str, str], list[float]] = defaultdict(list)

        # Advance sliding index past stale pings
        while other_idx < len(all_others) and all_others[other_idx]["recorded_at"].timestamp() < window_start:
            other_idx += 1

        scan = other_idx
        while scan < len(all_others) and all_others[scan]["recorded_at"].timestamp() < window_end:
            op = all_others[scan]
            dist = haversine_miles(avg_lat, avg_lon, op["lat"], op["lon"])
            key = (op["asset_type"], op["asset_id"])
            candidates[key].append(dist)
            scan += 1

        # Pick the nearest partner for this bucket
        best_key: tuple[str, str] | None = None
        best_dist = float("inf")
        best_pings = 0
        for key, dists in candidates.items():
            min_d = min(dists)
            if min_d < best_dist:
                best_dist = min_d
                best_key = key
                best_pings = len(dists)

        bucket_start_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        bucket_end_dt = datetime.fromtimestamp(bucket_end_epoch, tz=timezone.utc)

        if best_key is not None:
            partner_type, partner_id = best_key
            dist_feet = round(best_dist * 5280, 1)

            segments.append(TimelineSegment(
                unit_id=unit_id,
                unit_type=unit_type,
                partner_id=partner_id,
                partner_type=partner_type,
                start=bucket_start_dt,
                end=bucket_end_dt,
                duration_minutes=float(bucket_minutes),
                avg_distance_miles=round(best_dist, 4),
                bucket_count=len(bucket_unit) + best_pings,
                confidence=1.0,
            ))

            detail_rows.append({
                "time": bucket_start_dt.strftime("%H:%M"),
                "partner_type": partner_type,
                "partner_id": partner_id,
                "distance_ft": dist_feet,
                "distance_mi": round(best_dist, 4),
                "unit_pings": len(bucket_unit),
                "partner_pings": best_pings,
                "unit_providers": ", ".join(sorted({p["provider"] for p in bucket_unit if p["provider"]})),
                "avg_speed": round(sum(p["speed"] for p in bucket_unit) / len(bucket_unit), 1),
            })
        else:
            segments.append(TimelineSegment(
                unit_id=unit_id,
                unit_type=unit_type,
                partner_id="ALONE",
                partner_type="gap",
                start=bucket_start_dt,
                end=bucket_end_dt,
                duration_minutes=float(bucket_minutes),
                avg_distance_miles=0.0,
                bucket_count=len(bucket_unit),
                confidence=0.0,
            ))

            detail_rows.append({
                "time": bucket_start_dt.strftime("%H:%M"),
                "partner_type": "—",
                "partner_id": "No nearby units",
                "distance_ft": 0,
                "distance_mi": 0,
                "unit_pings": len(bucket_unit),
                "partner_pings": 0,
                "unit_providers": ", ".join(sorted({p["provider"] for p in bucket_unit if p["provider"]})),
                "avg_speed": round(sum(p["speed"] for p in bucket_unit) / len(bucket_unit), 1),
            })

        epoch = bucket_end_epoch

    return segments, detail_rows


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


def load_trailer_activity_summary(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Load trailer activity summary rows for unmatched-moving-trailer alerting."""
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Trailer activity summary unavailable: %s", e)
        return []
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {
        "service_date": f"gte.{start.date().isoformat()}",
        "and": f"(service_date.lte.{end.date().isoformat()})",
    }
    try:
        return client.select_all(
            "trailer_activity_summary",
            filters=filters,
            order="service_date.desc,trailer_id.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Trailer activity summary query unavailable: %s", e)
        return []


def load_manual_pair_assignments(active_only: bool = True) -> list[dict[str, Any]]:
    """Load manual truck/trailer pair assignments."""
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Manual pair assignments unavailable: %s", e)
        return []
    filters: dict[str, Any] = {}
    if active_only:
        filters["active"] = "eq.true"
    try:
        return client.select_all(
            "manual_pair_assignments",
            filters=filters,
            order="assigned_at.desc",
            page_size=500,
            hard_cap=5000,
        )
    except Exception as e:
        logger.info("Manual pair assignments query unavailable: %s", e)
        return []


def save_manual_pair_assignment(row: dict[str, Any]) -> bool:
    """Upsert a manual pair assignment."""
    try:
        client = _get_client()
    except Exception:
        return False
    try:
        client.upsert(
            "manual_pair_assignments",
            [row],
            on_conflict="truck_id,trailer_id,start_date",
        )
        return True
    except Exception as e:
        logger.warning("Failed to save manual pair assignment: %s", e)
        return False


def deactivate_manual_pair_assignment(assignment_id: int) -> bool:
    """Mark a manual assignment as inactive (unassign)."""
    try:
        client = _get_client()
    except Exception:
        return False
    try:
        from datetime import datetime as dt, timezone as tz
        client.patch(
            "manual_pair_assignments",
            {"active": False, "unassigned_at": dt.now(tz.utc).isoformat()},
            filters={"id": f"eq.{assignment_id}"},
        )
        return True
    except Exception as e:
        logger.warning("Failed to deactivate manual pair assignment: %s", e)
        return False


def load_trailer_gps_trail(
    trailer_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Load raw GPS pings for a single trailer from assets_history.

    Returns lightweight dicts with lat, lon, recorded_at for route plotting.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Trailer GPS trail unavailable: %s", e)
        return []
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {
        "asset_id": f"eq.{trailer_id}",
        "asset_type": "eq.trailer",
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
    }
    try:
        rows = client.select_all(
            "assets_history",
            select="lat,lon,recorded_at,speed",
            filters=filters,
            order="recorded_at.asc",
            page_size=1000,
            hard_cap=20000,
        )
        return [r for r in rows if r.get("lat") and r.get("lon")]
    except Exception as e:
        logger.info("Trailer GPS trail query failed: %s", e)
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


def _load_latest_history_point(client: Any, asset_type: str, asset_id: str) -> Asset | None:
    """Return the newest historical point with usable coordinates for one asset."""
    if not asset_type or not asset_id:
        return None
    try:
        rows = client.select(
            "assets_history",
            select=(
                "asset_type,asset_id,division,lat,lon,address,zip,speed,heading_deg,"
                "provider,recorded_at,source,raw"
            ),
            filters={"asset_type": f"eq.{asset_type}", "asset_id": f"eq.{asset_id}"},
            order="recorded_at.desc",
            limit=25,
        )
    except Exception as e:
        logger.info("Latest history lookup failed for %s %s: %s", asset_type, asset_id, e)
        return None

    for row in rows:
        asset = _history_row_to_asset(row)
        if _asset_has_coords(asset) and asset.last_ping is not None:
            raw = dict(asset.raw or {})
            raw["historySource"] = row.get("source") or ""
            asset = replace(asset, raw=raw)
            return asset
    return None


def _merge_last_known(current: Asset, historical: Asset, *, stale_after_days: int, now: datetime) -> Asset:
    """Overlay latest historical coordinates onto an active current-roster asset."""
    current_has_coords = _asset_has_coords(current)
    historical_is_newer = (
        historical.last_ping is not None
        and (current.last_ping is None or historical.last_ping > current.last_ping)
    )
    use_history_position = not current_has_coords or historical_is_newer
    reason = "Missing current coordinates" if not current_has_coords else f"Current ping is > {stale_after_days} days old"

    raw = dict(current.raw or {})
    raw.update({
        "historicalLastKnown": True,
        "locationStatus": "Historical last known",
        "historicalLookupReason": reason,
        "currentLastPing": current.last_ping.isoformat() if current.last_ping else "",
        "historyLastPing": historical.last_ping.isoformat() if historical.last_ping else "",
        "historySource": (historical.raw or {}).get("historySource", ""),
    })

    if not use_history_position:
        return replace(current, raw=raw)

    return replace(
        current,
        lat=historical.lat,
        lon=historical.lon,
        speed=historical.speed if historical.speed is not None else current.speed,
        heading_deg=historical.heading_deg if historical.heading_deg is not None else current.heading_deg,
        last_ping=historical.last_ping or current.last_ping,
        address=historical.address or current.address,
        zip=historical.zip or current.zip,
        provider=historical.provider or current.provider,
        division=current.division or historical.division,
        raw=raw,
    )


def _mark_location_status(asset: Asset, status: str, *, reason: str = "") -> Asset:
    raw = dict(asset.raw or {})
    raw["locationStatus"] = status
    raw["historicalLastKnown"] = status == "Historical last known"
    if reason:
        raw["historicalLookupReason"] = reason
    return replace(asset, raw=raw)


def _asset_is_stale(asset: Asset, *, now: datetime, stale_after_days: int) -> bool:
    if asset.last_ping is None:
        return True
    ping = asset.last_ping.astimezone(timezone.utc)
    return now - ping > timedelta(days=stale_after_days)


def _asset_has_coords(asset: Asset) -> bool:
    if asset.lat is None or asset.lon is None:
        return False
    try:
        lat = float(asset.lat)
        lon = float(asset.lon)
    except (TypeError, ValueError):
        return False
    return not (lat == 0 and lon == 0)


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
