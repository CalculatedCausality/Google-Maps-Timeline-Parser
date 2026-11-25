"""Record types produced by the parsers.

Each kind of evidence stays in its own type (and its own database table):
a sensor reading, a route breadcrumb, a visit Google inferred and a movement
Google inferred are different things even when they describe the same trip.

All times are UTC, in milliseconds since 1970 (`*_ms`).
"""
from typing import NamedTuple, Optional


class Position(NamedTuple):
    """One individual location reading (not necessarily GPS)."""
    record_type: str            # legacy_record | raw_signal | edits_raw_signal
    t_ms: int
    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
    source: Optional[str] = None
    device_tag: Optional[str] = None
    altitude_m: Optional[float] = None
    speed_mps: Optional[float] = None


class PathPoint(NamedTuple):
    """One breadcrumb on a route Google kept for a movement."""
    t_ms: int
    latitude: float
    longitude: float


class Visit(NamedTuple):
    """A stay at a place, as inferred (or edited) in Timeline."""
    start_ms: int
    end_ms: int
    utc_offset_min: Optional[int]
    latitude: Optional[float]
    longitude: Optional[float]
    place_id: Optional[str] = None
    semantic_type: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    probability: Optional[float] = None
    confidence: Optional[str] = None


class Activity(NamedTuple):
    """A movement between places, with Google's travel mode and distance."""
    start_ms: int
    end_ms: int
    utc_offset_min: Optional[int]
    activity_type: str
    distance_m: Optional[float]
    probability: Optional[float] = None
    confidence: Optional[str] = None
    start_latitude: Optional[float] = None
    start_longitude: Optional[float] = None
    end_latitude: Optional[float] = None
    end_longitude: Optional[float] = None


class FrequentPlace(NamedTuple):
    """A labelled place (for example HOME or WORK) from the phone's profile."""
    place_id: Optional[str]
    label: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
