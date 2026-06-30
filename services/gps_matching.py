"""
GPS matching engine: auto-pairs trailers to trucks based on proximity,
freshness, and co-movement (speed/heading agreement).

Pure functions — no Supabase I/O here. The Streamlit page calls these with
data already loaded from Supabase.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Yard geofences.
# Existing dispatch-board code used bounding boxes; these circular fences use the
# requested yard centers with an approximate two-block radius.
# ---------------------------------------------------------------------------
YARD_RADIUS_MILES = 0.25
YARD_GEOFENCES = {
    "California Yard": {"lat": 34.09686, "lon": -117.47642, "radius_miles": YARD_RADIUS_MILES},
    "Illinois Yard": {"lat": 41.896873, "lon": -87.86982, "radius_miles": YARD_RADIUS_MILES},
}

# Backwards-compatible alias for imports/UI that referenced the previous name.
YARD_BOXES = YARD_GEOFENCES


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Asset:
    asset_type: str  # 'truck' | 'trailer'
    asset_id: str
    division: str = ""
    lat: float | None = None
    lon: float | None = None
    speed: float | None = None  # kph or provider unit
    heading_deg: float | None = None
    last_ping: datetime | None = None
    address: str = ""
    zip: str = ""
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchResult:
    trailer: Asset
    truck: Asset
    distance_miles: float
    confidence: float  # 0.0 – 1.0
    reasons: list[str] = field(default_factory=list)
    on_board: bool = False  # True if this pairing matches dispatch_assignments
    history_hits: int = 0
    history_score: float = 0.0
    trailer_yard: str = ""
    truck_yard: str = ""
    segment_count: int = 0
    segment_hours: float = 0.0
    unique_days: int = 0


@dataclass
class HistoricalUsageResult:
    truck_id: str
    trailer_id: str
    hits: int
    days: list[str]
    first_seen: datetime | None
    last_seen: datetime | None
    min_distance_miles: float
    avg_distance_miles: float
    confidence: float
    segment_count: int = 0
    segment_hours: float = 0.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
_EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in miles between two lat/lon points."""
    to_rad = math.pi / 180
    d_lat = (lat2 - lat1) * to_rad
    d_lon = (lon2 - lon1) * to_rad
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1 * to_rad) * math.cos(lat2 * to_rad) * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_MILES * c


def in_yard(lat: float, lon: float) -> str | None:
    """Return yard name if coordinates fall within a known yard geofence."""
    for name, fence in YARD_GEOFENCES.items():
        if haversine_miles(lat, lon, fence["lat"], fence["lon"]) <= fence["radius_miles"]:
            return name
    return None


def _valid_coords(asset: Asset) -> bool:
    if asset.lat is None or asset.lon is None:
        return False
    try:
        lat = float(asset.lat)
        lon = float(asset.lon)
    except (TypeError, ValueError):
        return False
    return not (lat == 0 and lon == 0)


# ---------------------------------------------------------------------------
# Co-movement scoring
# ---------------------------------------------------------------------------
def _heading_agreement(h1: float | None, h2: float | None) -> float:
    """Return 0.0–1.0 score for how similar two headings are (degrees)."""
    if h1 is None or h2 is None:
        return 0.5  # neutral when data missing
    diff = abs(h1 - h2) % 360
    if diff > 180:
        diff = 360 - diff
    # 0 degrees apart = 1.0, 180 apart = 0.0
    return 1.0 - (diff / 180.0)


def _speed_agreement(s1: float | None, s2: float | None) -> float:
    """Return 0.0–1.0 score for how similar two speeds are."""
    if s1 is None or s2 is None:
        return 0.5  # neutral
    if s1 == 0 and s2 == 0:
        return 0.8  # both stopped: mild positive
    max_s = max(s1, s2, 1.0)
    diff_ratio = abs(s1 - s2) / max_s
    return max(0.0, 1.0 - diff_ratio)


def _freshness_score(
    ping: datetime | None,
    now: datetime,
    max_stale_minutes: float = 60,
) -> float:
    """1.0 if ping is very recent, decays to 0.0 at max_stale_minutes."""
    if ping is None:
        return 0.0
    delta_min = (now - ping).total_seconds() / 60.0
    if delta_min < 0:
        delta_min = 0
    return max(0.0, 1.0 - delta_min / max_stale_minutes)


# ---------------------------------------------------------------------------
# Time-bucketed spatial index — O(n) build, replaces O(n²) brute force
# ---------------------------------------------------------------------------
_BUCKET_MINUTES = 5  # 5-minute time buckets

# Type alias for the time index
_AssetSummary = tuple[float, float, float | None, float | None]  # (lat, lon, speed, heading)
_TimeIndex = dict[str, dict[str, _AssetSummary]]


def _build_time_index(
    history: Sequence[Asset],
    division_filter: str | None = None,
) -> _TimeIndex:
    """Build a time-bucketed spatial index from GPS history.

    Returns ``{bucket_key: {asset_key: (lat, lon, speed, heading)}}``
    where bucket_key is like ``"2026-06-25 14:30"`` and asset_key is
    ``"truck:808"``.  Multiple pings in the same bucket are position-averaged.
    """
    accum: dict[str, dict[str, list[Asset]]] = defaultdict(lambda: defaultdict(list))

    for point in history:
        if division_filter and point.division and point.division != division_filter:
            continue
        if not _valid_coords(point) or point.last_ping is None:
            continue
        ts = point.last_ping
        minute = (ts.minute // _BUCKET_MINUTES) * _BUCKET_MINUTES
        bucket = f"{ts.strftime('%Y-%m-%d %H')}:{minute:02d}"
        asset_key = f"{point.asset_type}:{point.asset_id}"
        accum[bucket][asset_key].append(point)

    index: _TimeIndex = {}
    for bucket, assets in accum.items():
        index[bucket] = {}
        for asset_key, points in assets.items():
            lats = [float(p.lat) for p in points]
            lons = [float(p.lon) for p in points]
            speeds = [float(p.speed) for p in points if p.speed is not None]
            headings = [float(p.heading_deg) for p in points if p.heading_deg is not None]
            index[bucket][asset_key] = (
                sum(lats) / len(lats),
                sum(lons) / len(lons),
                sum(speeds) / len(speeds) if speeds else None,
                sum(headings) / len(headings) if headings else None,
            )
    return index


def _pair_co_location_buckets(
    trailer_id: str,
    truck_id: str,
    time_index: _TimeIndex,
    max_distance_miles: float,
) -> list[tuple[str, float]]:
    """Find all time buckets where *trailer_id* and *truck_id* are co-located.

    Returns sorted ``[(bucket_key, distance_miles), ...]``.  Yard-only
    co-locations (both assets inside the same yard) are excluded.
    """
    t_key = f"trailer:{trailer_id}"
    u_key = f"truck:{truck_id}"
    co_buckets: list[tuple[str, float]] = []

    for bucket, assets in time_index.items():
        if t_key not in assets or u_key not in assets:
            continue
        t_lat, t_lon, _, _ = assets[t_key]
        u_lat, u_lon, _, _ = assets[u_key]
        # Yard-only co-location is weak evidence (many assets parked nearby)
        if in_yard(t_lat, t_lon) and in_yard(u_lat, u_lon):
            continue
        dist = haversine_miles(t_lat, t_lon, u_lat, u_lon)
        if dist <= max_distance_miles:
            co_buckets.append((bucket, dist))

    co_buckets.sort(key=lambda x: x[0])
    return co_buckets


def _bucket_to_datetime(bucket_key: str) -> datetime:
    """Parse ``'2026-06-25 14:30'`` → aware datetime."""
    return datetime.strptime(bucket_key, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _detect_co_travel_segments(
    co_buckets: list[tuple[str, float]],
    gap_tolerance_minutes: int = 15,
    min_segment_minutes: int = 20,
) -> list[dict[str, Any]]:
    """Identify continuous co-travel segments from co-located time buckets.

    A *segment* is a run of consecutive buckets (allowing small gaps for
    tunnel / signal loss) where truck and trailer stayed close.  A 2-hour
    trip segment is much stronger evidence than scattered 5-minute hits.

    Returns ``[{start, end, duration_minutes, avg_distance, bucket_count}]``.
    """
    if not co_buckets:
        return []

    segments: list[dict[str, Any]] = []

    def _close_segment(run: list[tuple[str, float]]) -> None:
        if len(run) < 2:
            return
        start_t = _bucket_to_datetime(run[0][0])
        end_t = _bucket_to_datetime(run[-1][0])
        duration = (end_t - start_t).total_seconds() / 60 + _BUCKET_MINUTES
        if duration >= min_segment_minutes:
            dists = [d for _, d in run]
            segments.append({
                "start": start_t,
                "end": end_t,
                "duration_minutes": duration,
                "avg_distance": sum(dists) / len(dists),
                "bucket_count": len(run),
            })

    current: list[tuple[str, float]] = [co_buckets[0]]
    for i in range(1, len(co_buckets)):
        prev_time = _bucket_to_datetime(current[-1][0])
        curr_time = _bucket_to_datetime(co_buckets[i][0])
        gap = (curr_time - prev_time).total_seconds() / 60
        if gap <= gap_tolerance_minutes:
            current.append(co_buckets[i])
        else:
            _close_segment(current)
            current = [co_buckets[i]]
    _close_segment(current)
    return segments


# ---------------------------------------------------------------------------
# Main matching logic
# ---------------------------------------------------------------------------
def compute_matches(
    trucks: Sequence[Asset],
    trailers: Sequence[Asset],
    assignments: dict[str, str] | None = None,
    history: Sequence[Asset] | None = None,
    *,
    max_distance_miles: float = 0.5,
    max_stale_minutes: float = 60,
    min_history_hits: int = 2,
    history_time_window_minutes: float = 45,
    division_filter: str | None = None,
    now: datetime | None = None,
) -> list[MatchResult]:
    """
    Auto-match trailers to trucks.

    Uses a time-bucketed index for O(n) history comparison and trip-segment
    detection for high-confidence forensic matching.

    Parameters
    ----------
    trucks : list of Asset (type='truck') with valid lat/lon
    trailers : list of Asset (type='trailer') with valid lat/lon
    assignments : optional dict of truck_id -> trailer_id (board pairings)
    history : GPS history points for evidence-based matching
    max_distance_miles : max radius for pairing consideration
    max_stale_minutes : pings older than this get freshness=0
    min_history_hits : historical co-location hours for full history confidence
    history_time_window_minutes : (unused — kept for API compat; buckets replace this)
    division_filter : if set, only consider assets with this division
    now : reference time (defaults to utcnow)

    Returns
    -------
    List of MatchResult, sorted by confidence descending.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    assignments = assignments or {}
    time_index = _build_time_index(history or [], division_filter)

    valid_trucks = [
        t for t in trucks
        if _valid_coords(t)
        and (division_filter is None or t.division == division_filter)
    ]

    valid_trailers = [
        tr for tr in trailers
        if _valid_coords(tr) and (not division_filter or tr.division == division_filter)
    ]

    # Score every trailer × truck pair.  With the time index, _history_agreement
    # is fast (bucket-scan, not O(T×U) per pair).
    scored_pairs: list[tuple[float, int, int, float, int, float, int, float, int]] = []
    # (neg_conf, ti, ui, dist, history_hits, history_score, seg_count, seg_hours, unique_days)

    for ti, trailer in enumerate(valid_trailers):
        trailer_yard = in_yard(float(trailer.lat), float(trailer.lon)) or ""
        for ui, truck in enumerate(valid_trucks):
            truck_yard = in_yard(float(truck.lat), float(truck.lon)) or ""
            dist = haversine_miles(
                float(trailer.lat), float(trailer.lon),
                float(truck.lat), float(truck.lon),
            )

            history_hits, history_score, seg_count, seg_hours, unique_days = (
                _history_agreement(
                    trailer, truck, time_index,
                    max_distance_miles=max_distance_miles,
                    min_history_hits=min_history_hits,
                )
            )

            board_trailer = assignments.get(truck.asset_id, "")
            board_agrees = board_trailer == trailer.asset_id

            if dist > max_distance_miles and history_hits == 0 and not board_agrees:
                continue

            # --- Component scores ---
            dist_score = max(0.0, 1.0 - (dist / max_distance_miles))
            if trailer_yard or truck_yard:
                dist_score *= 0.25

            truck_fresh = _freshness_score(truck.last_ping, now, max_stale_minutes)
            trailer_fresh = _freshness_score(trailer.last_ping, now, max_stale_minutes)
            freshness = min(truck_fresh, trailer_fresh)

            heading_score = _heading_agreement(trailer.heading_deg, truck.heading_deg)
            speed_score = _speed_agreement(trailer.speed, truck.speed)
            co_movement = (heading_score + speed_score) / 2.0
            board_score = 1.0 if board_agrees else 0.0

            # --- Weighted confidence ---
            # With dense history data, history evidence dominates. Trip segments
            # (continuous co-travel) are the strongest possible signal.
            confidence = (
                0.15 * dist_score
                + 0.10 * freshness
                + 0.10 * co_movement
                + 0.55 * history_score
                + 0.10 * board_score
            )

            scored_pairs.append((
                -confidence, ti, ui, round(dist, 3),
                history_hits, history_score, seg_count, seg_hours, unique_days,
            ))

    # Greedy 1:1 assignment, highest confidence first
    scored_pairs.sort()
    matched_trucks: set[str] = set()
    matched_trailers: set[str] = set()
    results: list[MatchResult] = []

    for neg_conf, ti, ui, dist, history_hits, history_score, seg_count, seg_hours, unique_days in scored_pairs:
        trailer = valid_trailers[ti]
        truck = valid_trucks[ui]
        if trailer.asset_id in matched_trailers or truck.asset_id in matched_trucks:
            continue

        confidence = -neg_conf
        trailer_yard = in_yard(float(trailer.lat), float(trailer.lon)) or ""
        truck_yard = in_yard(float(truck.lat), float(truck.lon)) or ""

        reasons: list[str] = []
        if seg_count:
            total_seg_min = seg_hours * 60
            reasons.append(f"{seg_count} trip segment{'s' if seg_count > 1 else ''} ({total_seg_min:.0f} min co-travel)")
        if history_hits >= min_history_hits:
            reasons.append(f"{history_hits}h co-located across {unique_days} day{'s' if unique_days != 1 else ''}")
        elif history_hits:
            reasons.append(f"{history_hits} historical hour{'s' if history_hits > 1 else ''}")
        if trailer_yard:
            reasons.append(f"trailer in {trailer_yard}")
        if truck_yard and truck_yard != trailer_yard:
            reasons.append(f"truck in {truck_yard}")
        if confidence >= 0.7:
            reasons.append("strong match")
        elif confidence >= 0.4:
            reasons.append("moderate match")
        else:
            reasons.append("weak signal")

        board_trailer = assignments.get(truck.asset_id, "")
        on_board = board_trailer == trailer.asset_id
        if on_board:
            reasons.append("matches dispatch board")

        results.append(MatchResult(
            trailer=trailer,
            truck=truck,
            distance_miles=dist,
            confidence=round(confidence, 3),
            reasons=reasons,
            on_board=on_board,
            history_hits=history_hits,
            history_score=round(history_score, 3),
            trailer_yard=trailer_yard,
            truck_yard=truck_yard,
            segment_count=seg_count,
            segment_hours=round(seg_hours, 2),
            unique_days=unique_days,
        ))
        matched_trucks.add(truck.asset_id)
        matched_trailers.add(trailer.asset_id)

    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


def _history_agreement(
    trailer: Asset,
    truck: Asset,
    time_index: _TimeIndex,
    *,
    max_distance_miles: float,
    min_history_hits: int,
) -> tuple[int, float, int, float, int]:
    """Score historical co-location evidence between a trailer and truck.

    Uses the pre-built time index for O(B) scanning (B = number of buckets)
    instead of O(T×U) brute force.

    Returns
    -------
    (unique_hours, score, segment_count, segment_hours, unique_days)
    """
    co_buckets = _pair_co_location_buckets(
        trailer.asset_id, truck.asset_id, time_index, max_distance_miles,
    )
    if not co_buckets:
        return 0, 0.0, 0, 0.0, 0

    # Unique hours and days of co-location
    hour_hits: set[str] = set()
    day_hits: set[str] = set()
    for bucket_key, _ in co_buckets:
        hour_hits.add(bucket_key[:13])   # "2026-06-25 14"
        day_hits.add(bucket_key[:10])    # "2026-06-25"

    unique_hours = len(hour_hits)
    unique_days = len(day_hits)

    # Detect trip segments (continuous co-travel runs)
    segments = _detect_co_travel_segments(co_buckets)
    segment_count = len(segments)
    segment_hours = sum(s["duration_minutes"] for s in segments) / 60.0

    # --- Composite score ---
    # Trip segments are the strongest evidence: a 2+ hour trip together
    # is near-certain.  Unique hours provide breadth, unique days prove
    # a regular pattern (not a one-off).
    segment_score = min(1.0, segment_hours / 2.0)    # 2h travel → full credit
    hour_score = min(1.0, unique_hours / max(1, min_history_hits * 3))
    day_score = min(1.0, unique_days / 3.0)          # 3+ days → full credit

    # Recency bonus: was the most recent co-location within 24h?
    most_recent_bucket = co_buckets[-1][0]
    try:
        most_recent_dt = _bucket_to_datetime(most_recent_bucket)
        hours_ago = (datetime.now(timezone.utc) - most_recent_dt).total_seconds() / 3600
        recency = max(0.0, 1.0 - hours_ago / 48.0)  # decays over 48h
    except Exception:
        recency = 0.0

    score = (
        0.40 * segment_score
        + 0.25 * hour_score
        + 0.20 * day_score
        + 0.15 * recency
    )

    return unique_hours, score, segment_count, segment_hours, unique_days


def compute_historical_usage(
    history: Sequence[Asset],
    *,
    max_distance_miles: float = 0.5,
    time_window_minutes: float = 45,
    min_hits: int = 1,
    division_filter: str | None = None,
) -> list[HistoricalUsageResult]:
    """Find truck/trailer usage over a historical date range.

    Uses the time-bucketed index for O(n) performance instead of
    O(trailers × trucks × T × U) brute force.  Detects continuous
    co-travel segments for high-confidence forensic matching.

    This is intentionally many-to-many: a single truck can show multiple
    trailers in the same day/week (drop/hook).  Yard-only co-locations are
    excluded so parking-lot proximity doesn't create false pairings.
    """
    time_index = _build_time_index(history, division_filter)

    # Identify all unique trailer and truck IDs present in the index
    trailer_ids: set[str] = set()
    truck_ids: set[str] = set()
    for assets_in_bucket in time_index.values():
        for key in assets_in_bucket:
            atype, aid = key.split(":", 1)
            if atype == "trailer":
                trailer_ids.add(aid)
            elif atype == "truck":
                truck_ids.add(aid)

    out: list[HistoricalUsageResult] = []

    for trailer_id in trailer_ids:
        for truck_id in truck_ids:
            co_buckets = _pair_co_location_buckets(
                trailer_id, truck_id, time_index, max_distance_miles,
            )
            if not co_buckets:
                continue

            # Unique hours and days
            hour_hits: set[str] = set()
            day_hits: set[str] = set()
            distances: list[float] = []
            first_seen: datetime | None = None
            last_seen: datetime | None = None

            for bucket_key, dist in co_buckets:
                hour_hits.add(bucket_key[:13])
                day_hits.add(bucket_key[:10])
                distances.append(dist)
                try:
                    dt = _bucket_to_datetime(bucket_key)
                    if first_seen is None or dt < first_seen:
                        first_seen = dt
                    if last_seen is None or dt > last_seen:
                        last_seen = dt
                except Exception:
                    pass

            unique_hours = len(hour_hits)
            if unique_hours < min_hits:
                continue

            unique_days = len(day_hits)
            segments = _detect_co_travel_segments(co_buckets)
            segment_count = len(segments)
            segment_hours = sum(s["duration_minutes"] for s in segments) / 60.0
            avg_dist = sum(distances) / len(distances)

            # Confidence: segment-heavy scoring
            segment_score = min(1.0, segment_hours / 2.0)
            hour_score = min(1.0, unique_hours / max(1, min_hits * 3))
            day_score = min(1.0, unique_days / 3.0)
            dist_quality = max(0.0, 1.0 - (avg_dist / max_distance_miles))

            confidence = (
                0.35 * segment_score
                + 0.25 * hour_score
                + 0.20 * day_score
                + 0.20 * dist_quality
            )

            out.append(HistoricalUsageResult(
                truck_id=truck_id,
                trailer_id=trailer_id,
                hits=unique_hours,
                days=sorted(day_hits),
                first_seen=first_seen,
                last_seen=last_seen,
                min_distance_miles=round(min(distances), 3),
                avg_distance_miles=round(avg_dist, 3),
                confidence=round(confidence, 3),
                segment_count=segment_count,
                segment_hours=round(segment_hours, 2),
            ))

    out.sort(key=lambda r: (-r.confidence, -r.hits, r.truck_id, r.trailer_id))
    return out


# ---------------------------------------------------------------------------
# Unit timeline: assignment history for a single truck or trailer
# ---------------------------------------------------------------------------
@dataclass
class TimelineSegment:
    """One segment in a unit's assignment timeline."""
    unit_id: str           # The selected unit
    unit_type: str         # 'truck' or 'trailer'
    partner_id: str        # Matched partner ID (or 'YARD' or 'UNMATCHED')
    partner_type: str      # 'truck', 'trailer', 'yard', 'gap'
    start: datetime
    end: datetime
    duration_minutes: float
    avg_distance_miles: float
    bucket_count: int
    confidence: float


def compute_unit_timeline(
    unit_id: str,
    unit_type: str,
    history: Sequence[Asset],
    *,
    max_distance_miles: float = 0.5,
    division_filter: str | None = None,
    time_index: _TimeIndex | None = None,
) -> list[TimelineSegment]:
    """Compute the assignment timeline for a single unit.

    Returns a time-ordered list of segments showing which partner the unit
    was paired with at each period, with yard visits as explicit breaks.

    Logic:
    - For each time bucket where the unit exists, find the closest opposite-type
      asset within *max_distance_miles*.
    - If the unit is in a yard, mark as 'YARD' (assignment boundary).
    - Consecutive buckets with the same partner merge into one segment.
    - Gaps (no nearby partner, not in yard) merge as 'UNMATCHED'.
    """
    if time_index is None:
        time_index = _build_time_index(history, division_filter)
    unit_key = f"{unit_type}:{unit_id}"
    opposite_type = "truck" if unit_type == "trailer" else "trailer"
    opposite_prefix = f"{opposite_type}:"

    # Gather all buckets where this unit appears, sorted chronologically
    unit_buckets: list[tuple[str, str, float]] = []
    # (bucket_key, partner_id_or_YARD/UNMATCHED, distance)

    for bucket_key in sorted(time_index.keys()):
        assets = time_index[bucket_key]
        if unit_key not in assets:
            continue
        u_lat, u_lon, _, _ = assets[unit_key]

        # Check if unit is in a yard
        yard = in_yard(u_lat, u_lon)
        if yard:
            unit_buckets.append((bucket_key, f"YARD:{yard}", 0.0))
            continue

        # Find closest opposite-type asset
        best_partner: str | None = None
        best_dist: float = max_distance_miles + 1

        for asset_key, (a_lat, a_lon, _, _) in assets.items():
            if not asset_key.startswith(opposite_prefix):
                continue
            # Skip partners that are also in a yard
            if in_yard(a_lat, a_lon):
                continue
            dist = haversine_miles(u_lat, u_lon, a_lat, a_lon)
            if dist <= max_distance_miles and dist < best_dist:
                best_dist = dist
                best_partner = asset_key.split(":", 1)[1]

        if best_partner:
            unit_buckets.append((bucket_key, best_partner, best_dist))
        else:
            unit_buckets.append((bucket_key, "UNMATCHED", 0.0))

    if not unit_buckets:
        return []

    # Merge consecutive buckets with the same partner into segments
    segments: list[TimelineSegment] = []
    current_partner = unit_buckets[0][1]
    current_start = unit_buckets[0][0]
    current_dists: list[float] = [unit_buckets[0][2]]
    current_count = 1

    def _flush_segment(partner: str, start_key: str, end_key: str, dists: list[float], count: int) -> None:
        start_dt = _bucket_to_datetime(start_key)
        end_dt = _bucket_to_datetime(end_key) + timedelta(minutes=_BUCKET_MINUTES)
        duration = (end_dt - start_dt).total_seconds() / 60

        if partner.startswith("YARD:"):
            p_type = "yard"
            p_id = partner.split(":", 1)[1]
        elif partner == "UNMATCHED":
            p_type = "gap"
            p_id = "UNMATCHED"
        else:
            p_type = opposite_type
            p_id = partner

        avg_dist = sum(dists) / len(dists) if dists else 0.0
        # Confidence for partnered segments
        if p_type in ("yard", "gap"):
            conf = 0.0
        else:
            # More buckets + tighter distance = higher confidence
            dist_quality = max(0.0, 1.0 - (avg_dist / max_distance_miles))
            duration_quality = min(1.0, duration / 60.0)  # 1h+ = full credit
            conf = 0.5 * duration_quality + 0.5 * dist_quality

        segments.append(TimelineSegment(
            unit_id=unit_id,
            unit_type=unit_type,
            partner_id=p_id,
            partner_type=p_type,
            start=start_dt,
            end=end_dt,
            duration_minutes=round(duration, 1),
            avg_distance_miles=round(avg_dist, 3),
            bucket_count=count,
            confidence=round(conf, 3),
        ))

    for i in range(1, len(unit_buckets)):
        bucket_key, partner, dist = unit_buckets[i]
        prev_time = _bucket_to_datetime(unit_buckets[i - 1][0])
        curr_time = _bucket_to_datetime(bucket_key)
        gap_minutes = (curr_time - prev_time).total_seconds() / 60

        # Same partner and no large time gap → extend current segment
        if partner == current_partner and gap_minutes <= 30:
            current_dists.append(dist)
            current_count += 1
        else:
            # Close current segment
            _flush_segment(current_partner, current_start, unit_buckets[i - 1][0], current_dists, current_count)
            current_partner = partner
            current_start = bucket_key
            current_dists = [dist]
            current_count = 1

    # Close final segment
    _flush_segment(current_partner, current_start, unit_buckets[-1][0], current_dists, current_count)

    return segments
