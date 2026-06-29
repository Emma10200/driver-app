"""
GPS matching engine: auto-pairs trailers to trucks based on proximity,
freshness, and co-movement (speed/heading agreement).

Pure functions — no Supabase I/O here. The Streamlit page calls these with
data already loaded from Supabase.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    Parameters
    ----------
    trucks : list of Asset (type='truck') with valid lat/lon
    trailers : list of Asset (type='trailer') with valid lat/lon
    assignments : optional dict of truck_id -> trailer_id (board pairings)
    max_distance_miles : max radius for pairing consideration
    max_stale_minutes : pings older than this get freshness=0
    min_history_hits : historical co-location hits needed for full history confidence
    history_time_window_minutes : max time delta between truck/trailer history pings
    division_filter : if set, only consider assets with this division
    now : reference time (defaults to utcnow)

    Returns
    -------
    List of MatchResult, sorted by confidence descending.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    assignments = assignments or {}
    history_by_asset = _group_history(history or [], division_filter)

    # Filter and prepare trucks
    valid_trucks = [
        t for t in trucks
        if _valid_coords(t)
        and (division_filter is None or t.division == division_filter)
    ]

    # Filter trailers: require valid coords, but do not exclude yards. Yard
    # matches are downweighted unless historical route evidence supports them.
    valid_trailers = []
    for tr in trailers:
        if not _valid_coords(tr):
            continue
        if division_filter and tr.division != division_filter:
            continue
        valid_trailers.append(tr)

    results: list[MatchResult] = []
    # Track which trucks have already been matched (1:1 greedy)
    matched_trucks: set[str] = set()

    # For each trailer, find nearest truck within radius and score. Historical
    # route evidence can create a candidate even when current positions are no
    # longer close, especially when the trailer is parked in a yard.
    scored_pairs: list[tuple[float, int, int, float, int, float]] = []
    # (neg_confidence, trailer_idx, truck_idx, current_dist, history_hits, history_score)

    for ti, trailer in enumerate(valid_trailers):
        trailer_yard = in_yard(float(trailer.lat), float(trailer.lon)) or ""
        for ui, truck in enumerate(valid_trucks):
            truck_yard = in_yard(float(truck.lat), float(truck.lon)) or ""
            dist = haversine_miles(float(trailer.lat), float(trailer.lon), float(truck.lat), float(truck.lon))

            history_hits, history_score = _history_agreement(
                trailer,
                truck,
                history_by_asset,
                max_distance_miles=max_distance_miles,
                min_history_hits=min_history_hits,
                time_window_minutes=history_time_window_minutes,
            )

            board_trailer = assignments.get(truck.asset_id, "")
            board_agrees = board_trailer == trailer.asset_id

            if dist > max_distance_miles and history_hits == 0 and not board_agrees:
                continue

            # Distance score: closer = higher (linear within radius)
            dist_score = max(0.0, 1.0 - (dist / max_distance_miles))

            # Current yard co-location is weak evidence: dozens of assets can be
            # parked inside the same small area. Historical movement evidence is
            # much stronger and remains fully counted below.
            if trailer_yard or truck_yard:
                dist_score *= 0.25

            # Freshness: both truck and trailer should be fresh
            truck_fresh = _freshness_score(truck.last_ping, now, max_stale_minutes)
            trailer_fresh = _freshness_score(trailer.last_ping, now, max_stale_minutes)
            freshness = min(truck_fresh, trailer_fresh)

            # Co-movement
            heading_score = _heading_agreement(trailer.heading_deg, truck.heading_deg)
            speed_score = _speed_agreement(trailer.speed, truck.speed)
            co_movement = (heading_score + speed_score) / 2.0
            board_score = 1.0 if board_agrees else 0.0

            # Weighted confidence
            confidence = (
                0.25 * dist_score
                + 0.20 * freshness
                + 0.20 * co_movement
                + 0.30 * history_score
                + 0.05 * board_score
            )

            scored_pairs.append((-confidence, ti, ui, round(dist, 3), history_hits, history_score))

    # Greedy assignment: sort by descending confidence, assign 1:1
    scored_pairs.sort()
    matched_trailers: set[str] = set()

    for neg_conf, ti, ui, dist, history_hits, history_score in scored_pairs:
        trailer = valid_trailers[ti]
        truck = valid_trucks[ui]
        if trailer.asset_id in matched_trailers or truck.asset_id in matched_trucks:
            continue

        confidence = -neg_conf
        trailer_yard = in_yard(float(trailer.lat), float(trailer.lon)) or ""
        truck_yard = in_yard(float(truck.lat), float(truck.lon)) or ""

        reasons = []
        if history_hits >= min_history_hits:
            reasons.append(f"{history_hits} historical co-location hits")
        elif history_hits:
            reasons.append(f"{history_hits} historical hit")
        if trailer_yard:
            reasons.append(f"trailer currently in {trailer_yard}")
        if truck_yard and truck_yard != trailer_yard:
            reasons.append(f"truck currently in {truck_yard}")
        if confidence >= 0.7:
            reasons.append("strong proximity + co-movement")
        elif confidence >= 0.4:
            reasons.append("moderate proximity")
        else:
            reasons.append("weak signal")

        # Check if this matches the board
        board_trailer = assignments.get(truck.asset_id, "")
        on_board = board_trailer == trailer.asset_id

        if on_board:
            reasons.append("matches dispatch board")

        results.append(MatchResult(
            trailer=trailer,
            truck=truck,
            distance_miles=round(dist, 3),
            confidence=round(confidence, 3),
            reasons=reasons,
            on_board=on_board,
            history_hits=history_hits,
            history_score=round(history_score, 3),
            trailer_yard=trailer_yard,
            truck_yard=truck_yard,
        ))
        matched_trucks.add(truck.asset_id)
        matched_trailers.add(trailer.asset_id)

    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


def _group_history(history: Sequence[Asset], division_filter: str | None) -> dict[tuple[str, str], list[Asset]]:
    grouped: dict[tuple[str, str], list[Asset]] = {}
    for point in history:
        if division_filter and point.division and point.division != division_filter:
            continue
        if not _valid_coords(point) or point.last_ping is None:
            continue
        grouped.setdefault((point.asset_type, point.asset_id), []).append(point)
    for points in grouped.values():
        points.sort(key=lambda p: p.last_ping or datetime.min.replace(tzinfo=timezone.utc))
    return grouped


def _history_agreement(
    trailer: Asset,
    truck: Asset,
    history_by_asset: dict[tuple[str, str], list[Asset]],
    *,
    max_distance_miles: float,
    min_history_hits: int,
    time_window_minutes: float,
) -> tuple[int, float]:
    trailer_points = history_by_asset.get(("trailer", trailer.asset_id), [])
    truck_points = history_by_asset.get(("truck", truck.asset_id), [])
    if not trailer_points or not truck_points:
        return 0, 0.0

    hits = 0
    bucket_hits: set[str] = set()
    max_delta_seconds = time_window_minutes * 60
    for tp in trailer_points:
        if tp.last_ping is None:
            continue
        if in_yard(float(tp.lat), float(tp.lon)):
            continue
        best_dist = None
        for up in truck_points:
            if up.last_ping is None:
                continue
            if in_yard(float(up.lat), float(up.lon)):
                continue
            delta = abs((tp.last_ping - up.last_ping).total_seconds())
            if delta > max_delta_seconds:
                continue
            dist = haversine_miles(float(tp.lat), float(tp.lon), float(up.lat), float(up.lon))
            if dist <= max_distance_miles and (best_dist is None or dist < best_dist):
                best_dist = dist
        if best_dist is not None:
            hits += 1
            bucket_hits.add(tp.last_ping.strftime("%Y-%m-%d %H"))

    unique_hits = len(bucket_hits)
    if unique_hits == 0:
        return 0, 0.0
    return unique_hits, min(1.0, unique_hits / max(1, min_history_hits))
