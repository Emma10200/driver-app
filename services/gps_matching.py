"""
GPS shared primitives.

Historically this module also held a live trailer↔truck matcher
(``compute_matches``/``compute_historical_usage``/``compute_unit_timeline``).
That live-matching path was retired: all pairing now happens in the batch
pipeline (``scripts/compute_pair_hourly_evidence.py``) and the Streamlit UI reads
precomputed evidence tables. Only the shared data classes and geometry helpers
that the current code still imports remain here.

Kept intentionally I/O-free so both the UI and the scripts can import it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Yard geofences (circular). Centers are the physical yards; ~two-block radius.
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
