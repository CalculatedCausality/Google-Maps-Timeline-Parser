from math import atan2, cos, radians, sin, sqrt
from typing import Iterable, Iterator, List

from models.location import Location


class DistanceService:
    """Splits a track into believable segments and measures them.

    A straight line across a long silence, or an impossible jump caused by a
    bad Wi-Fi or mobile-tower fix, is not somewhere you travelled. The track
    is cut at those points instead of being joined up.
    """

    EARTH_RADIUS_KM = 6371.0

    def __init__(self, max_gap_minutes: float = 30.0, max_speed_kmh: float = 1200.0) -> None:
        self._max_gap_ms = max_gap_minutes * 60_000
        self._max_speed_kmh = max_speed_kmh

    def split_segments(self, locations: Iterable[Location]) -> Iterator[List[Location]]:
        """Yield runs of consecutive points with no long gap or impossible jump.

        Runs of a single point are dropped: one point is not a track.
        """
        segment: List[Location] = []
        for loc in locations:
            if segment:
                prev = segment[-1]
                elapsed_ms = loc.timestamp - prev.timestamp
                if elapsed_ms <= 0:
                    continue  # repeated or out-of-order time: keep the first reading
                km = self._haversine(prev.latitude, prev.longitude,
                                     loc.latitude, loc.longitude)
                speed_kmh = km / (elapsed_ms / 3_600_000)
                if elapsed_ms > self._max_gap_ms or speed_kmh > self._max_speed_kmh:
                    if len(segment) > 1:
                        yield segment
                    segment = []
            segment.append(loc)
        if len(segment) > 1:
            yield segment

    def calculate_total(self, locations: Iterable[Location]) -> float:
        """Return the distance (km) along the track, not counting gaps and jumps."""
        return sum(self.segment_length(segment) for segment in self.split_segments(locations))

    def segment_length(self, segment: List[Location]) -> float:
        return sum(
            self._haversine(a.latitude, a.longitude, b.latitude, b.longitude)
            for a, b in zip(segment, segment[1:])
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _haversine(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        )
        return self.EARTH_RADIUS_KM * 2 * atan2(sqrt(a), sqrt(1 - a))
