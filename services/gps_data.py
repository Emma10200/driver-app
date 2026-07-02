"""
GPS data access: reads assets_current, assets_history, and dispatch_assignments
from Supabase. Uses the same SupabaseRestClient pattern as the rest of the app.
"""
from __future__ import annotations

import json
import logging
import re
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

_DASHED_TRAILER_RE = re.compile(r"^(\d{2,})-\d+")


def _unit_sort_key(unit: str) -> tuple[int, str]:
    text = str(unit or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    return (int(digits) if digits else 999999999, text)


def _trailer_alias_map_from_ids(ids: list[str] | set[str]) -> dict[str, str]:
    """Return {numeric_base: unique_dashed_id} for safe trailer alias folding."""
    dashed_by_prefix: dict[str, set[str]] = {}
    for raw in ids:
        value = str(raw or "").strip()
        if not value:
            continue
        match = _DASHED_TRAILER_RE.match(value)
        if match:
            dashed_by_prefix.setdefault(match.group(1), set()).add(value)
    return {prefix: next(iter(values)) for prefix, values in dashed_by_prefix.items() if len(values) == 1}


def _canonicalize_trailer_id(asset_id: str, alias_map: dict[str, str]) -> str:
    text = str(asset_id or "").strip()
    if re.fullmatch(r"\d{2,}", text):
        return alias_map.get(text, text)
    return text


def _load_provider_trailer_alias_map(client: Any) -> dict[str, str]:
    """Load canonical trailer aliases from the independent provider-unit archive."""
    try:
        rows = client.select_all(
            "gps_provider_units",
            select="asset_id,canonical_asset_id,display_name",
            filters={"asset_type": "eq.trailer"},
            order="asset_id.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Provider trailer alias lookup unavailable: %s", e)
        return {}
    ids: set[str] = set()
    for row in rows:
        ids.add(str(row.get("asset_id") or "").strip())
        ids.add(str(row.get("canonical_asset_id") or "").strip())
        ids.add(str(row.get("display_name") or "").strip())
    return _trailer_alias_map_from_ids(ids)


def _expand_asset_query_ids(asset_type: str, asset_ids: list[str], alias_map: dict[str, str] | None = None) -> list[str]:
    """Include safe trailer base/canonical variants for history/evidence reads."""
    alias_map = alias_map or {}
    out: set[str] = set()
    for raw in asset_ids:
        value = str(raw or "").strip()
        if not value:
            continue
        out.add(value)
        if str(asset_type).lower() == "trailer":
            match = _DASHED_TRAILER_RE.match(value)
            if match:
                out.add(match.group(1))
            elif value in alias_map:
                out.add(alias_map[value])
    return sorted(out, key=_unit_sort_key)


def _asset_id_filter(client: Any, asset_type: str, asset_id: str) -> str:
    alias_map = _load_provider_trailer_alias_map(client) if str(asset_type).lower() == "trailer" else {}
    ids = _expand_asset_query_ids(asset_type, [asset_id], alias_map)
    if len(ids) <= 1:
        return f"eq.{quote(ids[0] if ids else str(asset_id or ''), safe='')}"
    return "in.(" + ",".join(quote(value, safe="") for value in ids) + ")"


def _canonicalize_trailer_rows(
    rows: list[dict[str, Any]],
    column: str,
    alias_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    alias_map = dict(alias_map or {})
    if not alias_map:
        alias_map = _trailer_alias_map_from_ids([str(row.get(column) or "") for row in rows])
    if not alias_map:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        current = str(row.get(column) or "").strip()
        canonical = _canonicalize_trailer_id(current, alias_map)
        if canonical == current:
            out.append(row)
            continue
        updated = dict(row)
        updated[column] = canonical
        updated[f"original_{column}"] = current
        out.append(updated)
    return out


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


def load_assignments() -> dict[str, str]:
    """Load dispatch_assignments as a dict of truck_id -> trailer_id."""
    try:
        client = _get_client()
    except Exception:
        return {}

    rows = client.select_all("dispatch_assignments", select="truck_id,trailer_id", page_size=1000, hard_cap=50000)
    return {r["truck_id"]: r["trailer_id"] for r in rows if r.get("truck_id")}


def load_analysis_unit_options(days: int = 180) -> list[dict[str, Any]]:
    """Return units available in detailed GPS history/evidence tables.

    This intentionally does **not** depend on ``assets_current``. The map and
    dispatch board use current rows; history/timeline/usage analysis needs to
    include units that are inactive, deactivated, or present only in historical
    backfills/derived tables.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Analysis unit list unavailable: %s", e)
        return []

    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    rows: list[dict[str, Any]] = []
    try:
        rows.extend(client.select_all(
            "asset_hour_tracks",
            select="asset_type,asset_id,hour_start,provider,source",
            filters={"hour_start": f"gte.{since.isoformat()}"},
            order="asset_type.asc,asset_id.asc,hour_start.desc",
            page_size=1000,
            hard_cap=200000,
        ))
    except Exception as e:
        logger.info("Analysis unit lookup from asset_hour_tracks failed: %s", e)

    provider_rows: list[dict[str, Any]] = []
    try:
        provider_rows = client.select_all(
            "gps_provider_units",
            select="asset_type,asset_id,canonical_asset_id,last_seen_at,last_history_at,provider,source",
            order="asset_type.asc,asset_id.asc,last_history_at.desc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Analysis unit lookup from gps_provider_units failed: %s", e)

    trailer_ids = {
        str(row.get("asset_id") or "").strip()
        for row in rows + provider_rows
        if str(row.get("asset_type") or "").lower() == "trailer"
    }
    trailer_ids.update(
        str(row.get("canonical_asset_id") or "").strip()
        for row in provider_rows
        if str(row.get("asset_type") or "").lower() == "trailer"
    )
    alias_map = _trailer_alias_map_from_ids(trailer_ids)

    units: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        asset_type = str(row.get("asset_type") or "").strip().lower()
        asset_id = str(row.get("asset_id") or "").strip()
        if asset_type not in {"truck", "trailer"} or not asset_id:
            continue
        if asset_type == "trailer":
            asset_id = _canonicalize_trailer_id(asset_id, alias_map)
        key = (asset_type, asset_id)
        acc = units.setdefault(key, {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "source": "asset_hour_tracks",
            "latest_at": "",
            "providers": set(),
        })
        latest = str(row.get("hour_start") or "")
        if latest > str(acc.get("latest_at") or ""):
            acc["latest_at"] = latest
        provider = str(row.get("provider") or "").strip()
        if provider:
            acc["providers"].add(provider)

    for row in provider_rows:
        asset_type = str(row.get("asset_type") or "").strip().lower()
        asset_id = str(row.get("canonical_asset_id") or row.get("asset_id") or "").strip()
        if asset_type not in {"truck", "trailer"} or not asset_id:
            continue
        if asset_type == "trailer":
            asset_id = _canonicalize_trailer_id(asset_id, alias_map)
        key = (asset_type, asset_id)
        acc = units.setdefault(key, {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "source": str(row.get("source") or "gps_provider_units") or "gps_provider_units",
            "latest_at": "",
            "providers": set(),
        })
        latest = str(row.get("last_history_at") or row.get("last_seen_at") or "")
        if latest > str(acc.get("latest_at") or ""):
            acc["latest_at"] = latest
        provider = str(row.get("provider") or "").strip()
        if provider:
            acc["providers"].add(provider)

    out: list[dict[str, Any]] = []
    for row in units.values():
        clean = dict(row)
        clean["providers"] = ", ".join(sorted(clean.get("providers") or []))
        out.append(clean)
    out.sort(key=lambda row: (str(row.get("asset_type") or ""), _unit_sort_key(str(row.get("asset_id") or ""))))
    return out


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
        "source": "eq.auto",
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }
    filters[unit_col] = _asset_id_filter(client, unit_type, unit_id)

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
        "source": "eq.auto",
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }
    filters[unit_col] = _asset_id_filter(client, unit_type, unit_id)

    try:
        rows = client.select_all(
            "asset_pair_hourly_evidence",
            filters=filters,
            order="hour_start.asc,trailer_id.asc,truck_id.asc",
            page_size=1000,
            hard_cap=50000,
        )
        if unit_type == "trailer":
            alias_map = _trailer_alias_map_from_ids([unit_id] + [str(row.get("trailer_id") or "") for row in rows])
            rows = _canonicalize_trailer_rows(rows, "trailer_id", alias_map)
        return rows
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
        rows = client.select_all(
            "asset_pair_daily_summary",
            filters=filters,
            order="service_date.desc,trailer_id.asc,truck_id.asc",
            page_size=1000,
            hard_cap=100000,
        )
        return _canonicalize_trailer_rows(rows, "trailer_id")
    except Exception as e:
        logger.info("Daily usage summary query unavailable: %s", e)
        return []


def load_evidence_truck_map(
    start: datetime, end: datetime,
) -> dict[str, dict[str, int]]:
    """Return {trailer_id: {truck_id: paired_hours}} from daily summary.

    Lightweight query used by the unmatched-trailer alert to determine which
    truck actually pulled each trailer according to GPS evidence — regardless
    of whether that truck is still on the dispatch board.
    """
    rows = load_usage_daily_summary(start, end)
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        paired = int(row.get("paired_hours") or 0)
        if paired <= 0:
            continue
        tid = str(row.get("trailer_id") or "")
        truck = str(row.get("truck_id") or "")
        if tid and truck:
            result.setdefault(tid, {})
            result[tid][truck] = result[tid].get(truck, 0) + paired
    return result


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


def load_trailer_drop_events(
    start: datetime,
    end: datetime,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    """Load compact operational dropped-trailer events.

    These rows come from ``trailer_drop_events`` and are intentionally separate
    from billing/paired-hour summaries. Missing table/migration is treated as
    no data so older deployments keep working until migration 0022 is applied.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Trailer drop events unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {
        "drop_started_at": f"gte.{start.isoformat()}",
        "and": f"(drop_started_at.lte.{end.isoformat()})",
    }
    if active_only:
        filters["status"] = "in.(active_drop,unknown_dropper,yard_drop)"

    try:
        return client.select_all(
            "trailer_drop_events",
            filters=filters,
            order="drop_started_at.desc,trailer_id.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Trailer drop events query unavailable: %s", e)
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
        rows = client.select_all(
            "trailer_activity_summary",
            filters=filters,
            order="service_date.desc,trailer_id.asc",
            page_size=1000,
            hard_cap=50000,
        )
        return _canonicalize_trailer_rows(rows, "trailer_id")
    except Exception as e:
        logger.info("Trailer activity summary query unavailable: %s", e)
        return []


def load_asset_hour_coverage(
    asset_type: str,
    asset_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, dict[str, Any]]:
    """Return compact GPS-hour coverage for units from ``asset_hour_tracks``.

    Used by the unmatched/mismatch dashboard to show whether the board-assigned
    truck or trailer is the side lacking usable GPS data during the selected
    evidence window.
    """
    ids = [str(asset_id).strip() for asset_id in asset_ids if str(asset_id or "").strip()]
    if not ids:
        return {}
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Asset-hour coverage unavailable: %s", e)
        return {}

    start = _ensure_aware(start)
    end = _ensure_aware(end)
    alias_map = _load_provider_trailer_alias_map(client) if asset_type == "trailer" else {}
    query_ids = _expand_asset_query_ids(asset_type, ids, alias_map)
    safe_ids = "in.(" + ",".join(quote(asset_id, safe="") for asset_id in sorted(set(query_ids))) + ")"
    filters: dict[str, Any] = {
        "asset_type": f"eq.{asset_type}",
        "asset_id": safe_ids,
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }
    try:
        rows = client.select_all(
            "asset_hour_tracks",
            select="asset_id,hour_start,ping_count,miles_traveled,moving,first_ping,last_ping,provider,source",
            filters=filters,
            order="asset_id.asc,hour_start.asc",
            page_size=1000,
            hard_cap=100000,
        )
    except Exception as e:
        logger.info("Asset-hour coverage query unavailable: %s", e)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        asset_id = str(row.get("asset_id") or "")
        if asset_type == "trailer":
            asset_id = _canonicalize_trailer_id(asset_id, alias_map)
        if not asset_id:
            continue
        acc = result.setdefault(asset_id, {
            "asset_id": asset_id,
            "gps_hours": 0,
            "moving_hours": 0,
            "ping_count": 0,
            "miles_traveled": 0.0,
            "first_ping": None,
            "last_ping": None,
            "providers": set(),
        })
        acc["gps_hours"] += 1
        if row.get("moving"):
            acc["moving_hours"] += 1
        acc["ping_count"] += int(row.get("ping_count") or 0)
        acc["miles_traveled"] += float(row.get("miles_traveled") or 0)
        first_ping = _parse_dt(row.get("first_ping")) or _parse_dt(row.get("hour_start"))
        last_ping = _parse_dt(row.get("last_ping")) or _parse_dt(row.get("hour_start"))
        if first_ping and (acc["first_ping"] is None or first_ping < acc["first_ping"]):
            acc["first_ping"] = first_ping
        if last_ping and (acc["last_ping"] is None or last_ping > acc["last_ping"]):
            acc["last_ping"] = last_ping
        provider = str(row.get("provider") or "").strip()
        if provider:
            acc["providers"].add(provider)

    for acc in result.values():
        acc["miles_traveled"] = round(float(acc["miles_traveled"] or 0), 1)
        acc["providers"] = ", ".join(sorted(acc["providers"]))
    return result


def load_gps_provider_unit_map(
    asset_type: str,
    asset_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Return provider roster/archive metadata keyed by fleet-facing asset ID.

    ``gps_provider_units`` is optional until migration 0023 is applied, so this
    loader fails softly. It is used only for labels in billing/mismatch UI; GPS
    evidence continues to come from ``assets_history``/derived evidence tables.
    """
    ids = [str(asset_id).strip() for asset_id in asset_ids if str(asset_id or "").strip()]
    if not ids:
        return {}
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("GPS provider-unit archive unavailable: %s", e)
        return {}

    try:
        alias_map = _load_provider_trailer_alias_map(client) if asset_type == "trailer" else {}
    except Exception:
        alias_map = {}
    query_ids = _expand_asset_query_ids(asset_type, ids, alias_map)
    safe_ids = "in.(" + ",".join(quote(asset_id, safe="") for asset_id in sorted(set(query_ids))) + ")"
    filters: dict[str, Any] = {
        "asset_type": f"eq.{asset_type}",
        "asset_id": safe_ids,
    }
    try:
        rows = client.select_all(
            "gps_provider_units",
            select=(
                "provider,provider_account,provider_unit_id,asset_type,asset_id,canonical_asset_id,"
                "display_name,status,is_active,last_seen_at,last_history_at,source"
            ),
            filters=filters,
            order="asset_id.asc,last_history_at.desc,last_seen_at.desc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("GPS provider-unit archive query unavailable: %s", e)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        asset_id = str(row.get("asset_id") or row.get("canonical_asset_id") or "").strip()
        if asset_type == "trailer":
            asset_id = _canonicalize_trailer_id(asset_id, alias_map)
        if not asset_id:
            continue
        existing = result.get(asset_id)
        if existing is None or _provider_unit_rank(row) > _provider_unit_rank(existing):
            result[asset_id] = row
    return result


def _provider_unit_rank(row: dict[str, Any]) -> tuple[int, str, str]:
    """Prefer explicit active/inactive roster rows, then freshest history rows."""
    source = str(row.get("source") or "")
    roster_score = 1 if source == "provider_roster" else 0
    last_history = str(row.get("last_history_at") or "")
    last_seen = str(row.get("last_seen_at") or "")
    return (roster_score, last_history, last_seen)


def load_trailer_unmatched_windows(
    trailer_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Return merged moving-hour windows where a trailer lacks paired evidence."""
    trailer_id = str(trailer_id or "").strip()
    if not trailer_id:
        return []
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Unmatched-window lookup unavailable: %s", e)
        return []

    start = _ensure_aware(start)
    end = _ensure_aware(end)

    track_filters: dict[str, Any] = {
        "asset_type": "eq.trailer",
        "asset_id": _asset_id_filter(client, "trailer", trailer_id),
        "moving": "eq.true",
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }
    try:
        track_rows = client.select_all(
            "asset_hour_tracks",
            select="hour_start,first_ping,last_ping,ping_count,miles_traveled",
            filters=track_filters,
            order="hour_start.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Trailer moving-hour query unavailable: %s", e)
        return []
    if not track_rows:
        return []

    pair_filters: dict[str, Any] = {
        "trailer_id": _asset_id_filter(client, "trailer", trailer_id),
        "source": "eq.auto",
        "status": "eq.paired",
        "and": f"(hour_start.gte.{start.isoformat()},hour_start.lte.{end.isoformat()})",
    }
    try:
        pair_rows = client.select_all(
            "asset_pair_hourly_evidence",
            select="hour_start,truck_id",
            filters=pair_filters,
            order="hour_start.asc",
            page_size=1000,
            hard_cap=50000,
        )
    except Exception as e:
        logger.info("Paired-hour query unavailable for unmatched windows: %s", e)
        pair_rows = []

    paired_hours = {
        _hour_key(_parse_dt(row.get("hour_start")))
        for row in pair_rows
        if _parse_dt(row.get("hour_start")) is not None
    }

    unmatched: list[dict[str, Any]] = []
    for row in track_rows:
        hour_start = _parse_dt(row.get("hour_start"))
        if hour_start is None or _hour_key(hour_start) in paired_hours:
            continue
        unmatched.append({
            "start": hour_start,
            "end": hour_start + timedelta(hours=1),
            "first_ping": _parse_dt(row.get("first_ping")) or hour_start,
            "last_ping": _parse_dt(row.get("last_ping")) or (hour_start + timedelta(hours=1)),
            "ping_count": int(row.get("ping_count") or 0),
            "miles_traveled": float(row.get("miles_traveled") or 0),
        })
    return _merge_hour_windows(unmatched)


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
    return _load_asset_gps_trail("trailer", trailer_id, start, end)


def load_truck_gps_trail(
    truck_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Load raw GPS pings for a single truck from assets_history."""
    return _load_asset_gps_trail("truck", truck_id, start, end)


def _load_asset_gps_trail(
    asset_type: str, asset_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Shared loader for truck/trailer GPS trail from assets_history."""
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("GPS trail unavailable: %s", e)
        return []
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    filters: dict[str, Any] = {
        "asset_id": _asset_id_filter(client, asset_type, asset_id),
        "asset_type": f"eq.{asset_type}",
        "and": f"(recorded_at.gte.{start.isoformat()},recorded_at.lte.{end.isoformat()})",
    }
    try:
        rows = client.select_all(
            "assets_history",
            select="lat,lon,recorded_at,speed,provider,address,source",
            filters=filters,
            order="recorded_at.asc",
            page_size=1000,
            hard_cap=20000,
        )
        return [r for r in rows if r.get("lat") and r.get("lon")]
    except Exception as e:
        logger.info("GPS trail query failed: %s", e)
        return []


def _hour_key(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def _merge_hour_windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    rows = sorted(rows, key=lambda row: row["start"])
    merged: list[dict[str, Any]] = []
    for row in rows:
        if not merged or row["start"] > merged[-1]["end"] + timedelta(minutes=1):
            merged.append({
                "start": row["start"],
                "end": row["end"],
                "ping_count": int(row.get("ping_count") or 0),
                "miles_traveled": float(row.get("miles_traveled") or 0),
                "hours": 1,
            })
            continue
        merged[-1]["end"] = max(merged[-1]["end"], row["end"])
        merged[-1]["ping_count"] += int(row.get("ping_count") or 0)
        merged[-1]["miles_traveled"] += float(row.get("miles_traveled") or 0)
        merged[-1]["hours"] += 1
    for row in merged:
        row["miles_traveled"] = round(float(row.get("miles_traveled") or 0), 1)
    return merged


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
