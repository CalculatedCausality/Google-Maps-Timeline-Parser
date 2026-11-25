from typing import NamedTuple, Optional


class Location(NamedTuple):
    """A point on the combined track used for maps, GPX and distance.

    `timestamp` is UTC, in milliseconds since 1970. `accuracy_m` is the
    reading's own accuracy radius, when it has one.
    """
    timestamp: int
    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
