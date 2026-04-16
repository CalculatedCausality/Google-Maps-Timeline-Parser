from math import atan2, cos, radians, sin, sqrt
from typing import List

from models.location import Location


class DistanceService:
    """Calculates distances between a sequence of Location points."""

    EARTH_RADIUS_KM = 6371.0

    def calculate_total(self, locations: List[Location]) -> float:
        """Return the total distance (km) along the ordered location sequence."""
        total = 0.0
        prev: Location | None = None
        for loc in locations:
            if prev is not None:
                total += self._haversine(
                    prev.latitude, prev.longitude,
                    loc.latitude, loc.longitude,
                )
            prev = loc
        return total

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
