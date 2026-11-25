"""Turns a scatter of location readings into lines that follow where you went.

Raw readings wobble: a phone left on a desk reports a cloud of points tens of
metres wide, a bad Wi-Fi or mobile-tower fix throws a single point kilometres
away, and every reading is off by its accuracy radius. Joining them up as they
are gives a scribble. The steps here, in order, are:

1. Drop spikes: a single point that jumps away and straight back.
2. Cut the track where there is a long silence or an impossible speed.
3. Collapse stays: a cloud of readings while you were not moving becomes one spot.
4. Smooth: a Kalman filter with a backward pass, trusting each reading
   according to its accuracy, so the line runs through the middle of the wobble.
5. Simplify: drop points that add no shape (Douglas-Peucker), so long periods
   stay light enough to draw.

This does not snap lines to roads; that needs a road map and a routing engine.
"""
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from typing import Iterable, Iterator, List, Optional, Tuple

from models.location import Location

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class CleanerSettings:
    max_gap_minutes: float = 30.0       # cut the line after this long with no reading
    max_speed_kmh: float = 1200.0       # faster than this is a bad reading, not travel
    spike_speed_kmh: float = 150.0      # out-and-back faster than this is a spike
    stay_radius_m: float = 60.0         # readings this close together count as one spot
    stay_minutes: float = 4.0           # ... if you stayed at least this long
    default_accuracy_m: float = 25.0    # assumed when a reading gives no accuracy
    min_accuracy_m: float = 5.0         # never trust a reading more than this
    acceleration_mps2: float = 0.05     # how sharply the smoother lets speed change ...
    acceleration_per_speed: float = 0.12  # ... plus this much per metre-per-second of speed
    simplify_m: float = 5.0             # Douglas-Peucker tolerance; 0 turns it off


def distance_m(a: Location, b: Location) -> float:
    lat1, lat2 = radians(a.latitude), radians(b.latitude)
    dlat = lat2 - lat1
    dlng = radians(b.longitude - a.longitude)
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return EARTH_RADIUS_M * 2 * atan2(sqrt(h), sqrt(1 - h))


def _accuracy(loc: Location, settings: CleanerSettings) -> float:
    value = loc.accuracy_m if loc.accuracy_m is not None else settings.default_accuracy_m
    return max(value, settings.min_accuracy_m)


class TrackCleaner:
    def __init__(self, settings: Optional[CleanerSettings] = None) -> None:
        self.settings = settings or CleanerSettings()

    # ------------------------------------------------------------------
    # The whole pipeline
    # ------------------------------------------------------------------

    def clean(self, locations: Iterable[Location]) -> Iterator[List[Location]]:
        """Yield cleaned line segments, in time order."""
        for segment in self.split(self.drop_spikes(locations)):
            segment = self.collapse_stays(segment)
            segment = self.smooth(segment)
            segment = self.simplify(segment)
            if len(segment) > 1:
                yield segment

    def raw_segments(self, locations: Iterable[Location]) -> Iterator[List[Location]]:
        """Only cut at gaps and impossible jumps; change nothing else."""
        yield from self.split(locations)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def drop_spikes(self, locations: Iterable[Location]) -> Iterator[Location]:
        """Remove a point that leaps away and straight back.

        Works on a sliding window of three, so it streams over any length.
        """
        limit_mps = self.settings.spike_speed_kmh / 3.6
        window: List[Location] = []
        for loc in locations:
            if window and loc.timestamp <= window[-1].timestamp:
                continue  # repeated or out-of-order time: keep the first reading
            window.append(loc)
            if len(window) < 3:
                continue
            before, middle, after = window
            if self._is_spike(before, middle, after, limit_mps):
                window = [before, after]
                continue
            yield window.pop(0)
        yield from window

    @staticmethod
    def _is_spike(before: Location, middle: Location, after: Location, limit_mps: float) -> bool:
        out = distance_m(before, middle)
        back = distance_m(middle, after)
        direct = distance_m(before, after)
        if min(out, back) < 200.0 or direct > 0.5 * min(out, back):
            return False
        seconds_out = max((middle.timestamp - before.timestamp) / 1000.0, 1.0)
        seconds_back = max((after.timestamp - middle.timestamp) / 1000.0, 1.0)
        return out / seconds_out > limit_mps or back / seconds_back > limit_mps

    def split(self, locations: Iterable[Location]) -> Iterator[List[Location]]:
        """Runs of points with no long gap or impossible jump (at least two points)."""
        max_gap_ms = self.settings.max_gap_minutes * 60_000
        max_mps = self.settings.max_speed_kmh / 3.6
        segment: List[Location] = []
        for loc in locations:
            if segment:
                prev = segment[-1]
                elapsed_ms = loc.timestamp - prev.timestamp
                if elapsed_ms <= 0:
                    continue
                speed = distance_m(prev, loc) / (elapsed_ms / 1000.0)
                if elapsed_ms > max_gap_ms or speed > max_mps:
                    if len(segment) > 1:
                        yield segment
                    segment = []
            segment.append(loc)
        if len(segment) > 1:
            yield segment

    def collapse_stays(self, segment: List[Location]) -> List[Location]:
        """Replace each cloud of readings taken while still with one spot.

        The spot is kept twice, at arrival and departure time, so the line
        still shows when you got there and when you left.
        """
        radius = self.settings.stay_radius_m
        min_ms = self.settings.stay_minutes * 60_000
        result: List[Location] = []
        i = 0
        while i < len(segment):
            sum_lat = segment[i].latitude
            sum_lng = segment[i].longitude
            j = i + 1
            while j < len(segment):
                count = j - i
                centre = Location(0, sum_lat / count, sum_lng / count)
                if distance_m(centre, segment[j]) > radius:
                    break
                sum_lat += segment[j].latitude
                sum_lng += segment[j].longitude
                j += 1
            count = j - i
            duration = segment[j - 1].timestamp - segment[i].timestamp
            if count >= 3 and duration >= min_ms:
                best = min(_accuracy(loc, self.settings) for loc in segment[i:j])
                lat, lng = sum_lat / count, sum_lng / count
                result.append(Location(segment[i].timestamp, lat, lng, best))
                result.append(Location(segment[j - 1].timestamp, lat, lng, best))
                i = j
            else:
                result.append(segment[i])
                i += 1
        return result

    def smooth(self, segment: List[Location]) -> List[Location]:
        """Smooth the wobble with a Kalman filter and a backward (RTS) pass.

        The filter assumes you keep moving at roughly the same speed and
        direction, changing it gradually, and trusts each reading according
        to its accuracy. Dense, wobbly readings are pulled onto a smooth line;
        readings far apart in time are kept almost exactly, because a lot can
        happen between them. The backward pass uses later readings too, so the
        line does not lag behind at corners.
        """
        if len(segment) < 3:
            return list(segment)
        origin = segment[0]
        metres_per_lat = radians(1.0) * EARTH_RADIUS_M
        metres_per_lng = metres_per_lat * cos(radians(origin.latitude))
        times = [(loc.timestamp - origin.timestamp) / 1000.0 for loc in segment]
        noise = [_accuracy(loc, self.settings) ** 2 for loc in segment]
        east = [(loc.longitude - origin.longitude) * metres_per_lng for loc in segment]
        north = [(loc.latitude - origin.latitude) * metres_per_lat for loc in segment]
        # Faster travel turns harder (a car at a corner versus a walker), so
        # the smoother allows more change when the readings show more speed.
        acceleration = []
        for k in range(len(segment)):
            first, last = max(k - 2, 0), min(k + 2, len(segment) - 1)
            seconds = max(times[last] - times[first], 1.0)
            speed = distance_m(segment[first], segment[last]) / seconds
            acceleration.append(self.settings.acceleration_mps2
                                + self.settings.acceleration_per_speed * speed)
        east = _smooth_axis(times, east, noise, acceleration)
        north = _smooth_axis(times, north, noise, acceleration)
        return [
            Location(loc.timestamp, origin.latitude + y / metres_per_lat,
                     origin.longitude + x / metres_per_lng, loc.accuracy_m)
            for loc, x, y in zip(segment, east, north)
        ]

    def simplify(self, segment: List[Location], tolerance_m: Optional[float] = None) -> List[Location]:
        """Drop points that sit within `tolerance_m` of the line through their neighbours."""
        tolerance = self.settings.simplify_m if tolerance_m is None else tolerance_m
        if tolerance <= 0 or len(segment) < 3:
            return list(segment)
        origin = segment[0]
        metres_per_lat = radians(1.0) * EARTH_RADIUS_M
        metres_per_lng = metres_per_lat * cos(radians(origin.latitude))
        xy = [((loc.longitude - origin.longitude) * metres_per_lng,
               (loc.latitude - origin.latitude) * metres_per_lat) for loc in segment]
        keep = [False] * len(segment)
        keep[0] = keep[-1] = True
        stack = [(0, len(segment) - 1)]
        while stack:
            first, last = stack.pop()
            if last - first < 2:
                continue
            ax, ay = xy[first]
            bx, by = xy[last]
            dx, dy = bx - ax, by - ay
            length_sq = dx * dx + dy * dy
            worst, worst_index = -1.0, first
            for k in range(first + 1, last):
                px, py = xy[k]
                if length_sq == 0:
                    d = sqrt((px - ax) ** 2 + (py - ay) ** 2)
                else:
                    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
                    d = sqrt((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2)
                if d > worst:
                    worst, worst_index = d, k
            if worst > tolerance:
                keep[worst_index] = True
                stack.append((first, worst_index))
                stack.append((worst_index, last))
        return [loc for loc, kept in zip(segment, keep) if kept]


def _smooth_axis(times: List[float], measured: List[float], noise: List[float],
                 acceleration: List[float]) -> List[float]:
    """Constant-velocity Kalman filter plus Rauch-Tung-Striebel smoother, one axis.

    State is (position, speed) in metres and metres per second.
    """
    count = len(measured)
    # Filtered state and covariance, and the predictions they came from.
    pos, vel = measured[0], 0.0
    p00, p01, p11 = noise[0], 0.0, 2500.0   # speed unknown at first: up to ~50 m/s
    filtered = [(pos, vel, p00, p01, p11)]
    predicted: List[Tuple[float, float, float, float, float]] = [filtered[0]]
    for k in range(1, count):
        dt = max(times[k] - times[k - 1], 0.001)
        q = acceleration[k] * acceleration[k]
        # Predict.
        pp = pos + vel * dt
        pv = vel
        a00 = p00 + 2 * dt * p01 + dt * dt * p11 + q * dt ** 4 / 4
        a01 = p01 + dt * p11 + q * dt ** 3 / 2
        a11 = p11 + q * dt * dt
        predicted.append((pp, pv, a00, a01, a11))
        # Update with the reading.
        total = a00 + noise[k]
        k0, k1 = a00 / total, a01 / total
        innovation = measured[k] - pp
        pos, vel = pp + k0 * innovation, pv + k1 * innovation
        p00, p01, p11 = (1 - k0) * a00, (1 - k0) * a01, a11 - k1 * a01
        filtered.append((pos, vel, p00, p01, p11))

    smoothed_pos, smoothed_vel = filtered[-1][0], filtered[-1][1]
    result = [0.0] * count
    result[-1] = smoothed_pos
    for k in range(count - 2, -1, -1):
        pos, vel, p00, p01, p11 = filtered[k]
        pp, pv, a00, a01, a11 = predicted[k + 1]
        dt = max(times[k + 1] - times[k], 0.001)
        # Gain C = P F^T inverse(A), with F = [[1, dt], [0, 1]].
        determinant = a00 * a11 - a01 * a01
        if determinant <= 1e-12:
            smoothed_pos, smoothed_vel = pos, vel
            result[k] = pos
            continue
        f00, f01 = p00 + dt * p01, p01      # first row of P F^T
        f10, f11 = p01 + dt * p11, p11      # second row
        i00, i01, i11 = a11 / determinant, -a01 / determinant, a00 / determinant
        c00, c01 = f00 * i00 + f01 * i01, f00 * i01 + f01 * i11
        c10, c11 = f10 * i00 + f11 * i01, f10 * i01 + f11 * i11
        d_pos, d_vel = smoothed_pos - pp, smoothed_vel - pv
        smoothed_pos = pos + c00 * d_pos + c01 * d_vel
        smoothed_vel = vel + c10 * d_pos + c11 * d_vel
        result[k] = smoothed_pos
    return result


def segment_length_km(segment: List[Location]) -> float:
    return sum(distance_m(a, b) for a, b in zip(segment, segment[1:])) / 1000.0
