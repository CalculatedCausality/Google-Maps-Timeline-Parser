from math import atan2, cos, radians, sin, sqrt
from typing import Iterable, Iterator, List

from models.location import Location

from .track_cleaner import CleanerSettings, TrackCleaner, segment_length_km


class DistanceService:
    """Splits a track into believable segments and measures them.

    A straight line across a long silence, or an impossible jump caused by a
    bad Wi-Fi or mobile-tower fix, is not somewhere you travelled. The track
    is cut at those points instead of being joined up.
    """

    EARTH_RADIUS_KM = 6371.0

    def __init__(self, max_gap_minutes: float = 30.0, max_speed_kmh: float = 1200.0) -> None:
        self._cleaner = TrackCleaner(CleanerSettings(
            max_gap_minutes=max_gap_minutes, max_speed_kmh=max_speed_kmh))

    def split_segments(self, locations: Iterable[Location]) -> Iterator[List[Location]]:
        """Yield runs of consecutive points with no long gap or impossible jump."""
        return self._cleaner.split(locations)

    def calculate_total(self, locations: Iterable[Location]) -> float:
        """Return the distance (km) along the track, not counting gaps and jumps."""
        return self.total_of_segments(self.split_segments(locations))

    @staticmethod
    def total_of_segments(segments: Iterable[List[Location]]) -> float:
        return sum(segment_length_km(segment) for segment in segments)

    def _haversine(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        )
        return self.EARTH_RADIUS_KM * 2 * atan2(sqrt(a), sqrt(1 - a))
