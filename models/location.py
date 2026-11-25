from typing import NamedTuple


class Location(NamedTuple):
    """A point on the combined track used for maps, GPX and distance.

    `timestamp` is UTC, in milliseconds since 1970.
    """
    timestamp: int
    latitude: float
    longitude: float
