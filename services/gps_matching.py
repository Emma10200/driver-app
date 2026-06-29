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
from typing import Sequence


# ---------------------------------------------------------------------------
# Yard bounding boxes (from dispatch-board GpsDashboard.js)
# ---------------------------------------------------------------------------
YARD_BOXES = {
    "IL_Melrose": {
        "min_lat": 41.895866,
        "max_lat": 41.898397,
        "min_lon": -87.871693,
        "max_lon": -87.868356,
    },
    "CA_Fontana": {
        "min_lat": 34.095885,
        "max_lat": 34.098320,
        "min_lon": -117.479931,
        "max_lon": -117.475650,
    },
}


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


@dataclass
class MatchResult:
    trailer: Asset
    truck: Asset
    distance_miles: float
    confidence: float  # 0.0 – 1.0
    reasons: list[str] = field(default_factory=list)
    on_board: bool = False  # True if this pairing matches dispatch_assignments


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
    """Return yard name if coordinates fall within a known yard, else None."""
    for name, box in YARD_BOXES.items():
        if box["min_lat"] <= lat <= box["max_lat"] and box["min_lon"] <= lon <= box["max_lon"]:
            return name
    return None


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
    *,
    max_distance_miles: float = 0.5,
    max_stale_minutes: float = 60,
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
    division_filter : if set, only consider assets with this division
    now : reference time (defaults to utcnow)

    Returns
    -------
    List of MatchResult, sorted by confidence descending.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    assignments = assignments or {}

    # Filter and prepare trucks
    valid_trucks = [
        t for t in trucks
        if t.lat is not None
        and t.lon is not None
        and (division_filter is None or t.division == division_filter)
    ]

    # Filter trailers: exclude those in yards, require valid coords
    valid_trailers = []
    for tr in trailers:
        if tr.lat is None or tr.lon is None:
            continue
        if division_filter and tr.division != division_filter:
            continue
        if in_yard(tr.lat, tr.lon):
            continue
        valid_trailers.append(tr)

    results: list[MatchResult] = []
    # Track which trucks have already been matched (1:1 greedy)
    matched_trucks: set[str] = set()

    # For each trailer, find nearest truck within radius and score
    scored_pairs: list[tuple[float, int, int]] = []  # (neg_confidence, trailer_idx, truck_idx)

    for ti, trailer in enumerate(valid_trailers):
        for ui, truck in enumerate(valid_trucks):
            dist = haversine_miles(trailer.lat, trailer.lon, truck.lat, truck.lon)
            if dist > max_distance_miles:
                continue

            # Distance score: closer = higher (linear within radius)
            dist_score = 1.0 - (dist / max_distance_miles)

            # Freshness: both truck and trailer should be fresh
            truck_fresh = _freshness_score(truck.last_ping, now, max_stale_minutes)
            trailer_fresh = _freshness_score(trailer.last_ping, now, max_stale_minutes)
            freshness = min(truck_fresh, trailer_fresh)

            # Co-movement
            heading_score = _heading_agreement(trailer.heading_deg, truck.heading_deg)
            speed_score = _speed_agreement(trailer.speed, truck.speed)
            co_movement = (heading_score + speed_score) / 2.0

            # Weighted confidence
            confidence = (
                0.40 * dist_score
                + 0.25 * freshness
                + 0.35 * co_movement
            )

            scored_pairs.append((-confidence, ti, ui))

    # Greedy assignment: sort by descending confidence, assign 1:1
    scored_pairs.sort()
    matched_trailers: set[str] = set()

    for neg_conf, ti, ui in scored_pairs:
        trailer = valid_trailers[ti]
        truck = valid_trucks[ui]
        if trailer.asset_id in matched_trailers or truck.asset_id in matched_trucks:
            continue

        confidence = -neg_conf
        dist = haversine_miles(trailer.lat, trailer.lon, truck.lat, truck.lon)

        reasons = []
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
        ))
        matched_trucks.add(truck.asset_id)
        matched_trailers.add(trailer.asset_id)

    results.sort(key=lambda r: r.confidence, reverse=True)
    return results
