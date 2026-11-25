import random
from math import cos, radians

import pytest

from conftest import CITY_HALL, OPERA_HOUSE
from models.location import Location
from services.track_cleaner import (CleanerSettings, TrackCleaner, distance_m,
                                    segment_length_km)

START = 1_700_000_000_000
METRES_PER_LAT = 111_195.0
METRES_PER_LNG = METRES_PER_LAT * cos(radians(OPERA_HOUSE[0]))


def offset(east_m: float, north_m: float):
    """A point this many metres east and north of the Opera House."""
    return (OPERA_HOUSE[0] + north_m / METRES_PER_LAT,
            OPERA_HOUSE[1] + east_m / METRES_PER_LNG)


def distance_to_true_path(loc: Location) -> float:
    """The true route below runs due east along north = 0."""
    return abs((loc.latitude - OPERA_HOUSE[0]) * METRES_PER_LAT)


def noisy_walk(seed: int = 7):
    """Stand still for 10 minutes, then walk 2 km due east at 1.4 m/s.

    Readings every 15 seconds, each off by up to 20 metres, with one bad
    mobile-tower fix in Brisbane halfway along.
    """
    rng = random.Random(seed)
    points, t = [], START
    for _ in range(40):                                  # 10 minutes standing still
        lat, lng = offset(rng.gauss(0, 12), rng.gauss(0, 12))
        points.append(Location(t, lat, lng, 20.0))
        t += 15_000
    east = 0.0
    while east < 2000:
        east += 1.4 * 15
        lat, lng = offset(east + rng.gauss(0, 10), rng.gauss(0, 10))
        points.append(Location(t, lat, lng, 20.0))
        t += 15_000
    spike_at = 40 + (len(points) - 40) // 2
    points[spike_at] = Location(points[spike_at].timestamp, *CITY_HALL, 1500.0)
    return points, spike_at


def test_spike_is_removed_but_a_real_journey_is_not():
    cleaner = TrackCleaner()
    points, spike_at = noisy_walk()
    kept = list(cleaner.drop_spikes(points))
    assert len(kept) == len(points) - 1
    assert points[spike_at] not in kept

    # Driving away and not coming back is travel, not a spike.
    drive = [Location(START + i * 60_000, *offset(i * 1500, 0)) for i in range(5)]
    assert list(cleaner.drop_spikes(drive)) == drive


def test_repeated_times_keep_the_first_reading():
    cleaner = TrackCleaner()
    a = Location(START, *OPERA_HOUSE)
    b = Location(START, *offset(10, 0))
    c = Location(START + 1000, *offset(20, 0))
    assert list(cleaner.drop_spikes([a, b, c])) == [a, c]


def test_standing_still_becomes_one_spot_with_arrival_and_departure():
    cleaner = TrackCleaner()
    points, _ = noisy_walk()
    segment = points[:60]
    collapsed = cleaner.collapse_stays(segment)

    assert collapsed[0].timestamp == segment[0].timestamp
    assert (collapsed[0].latitude, collapsed[0].longitude) == (
        collapsed[1].latitude, collapsed[1].longitude)
    assert distance_m(collapsed[0], Location(0, *OPERA_HOUSE)) < 10
    # The whole 10-minute cloud (40 readings) is now 2 points.
    assert len(collapsed) < len(segment) - 30

    # A quick pause shorter than the threshold is left alone.
    short = [Location(START + i * 30_000, *offset(i, 0)) for i in range(4)]
    assert cleaner.collapse_stays(short) == short


def test_cleaned_line_follows_the_true_route_better_than_the_readings():
    cleaner = TrackCleaner()
    points, spike_at = noisy_walk()
    walking = [p for i, p in enumerate(points) if i >= 40 and i != spike_at]

    smoothed = cleaner.smooth(walking)
    raw_error = sum(map(distance_to_true_path, walking)) / len(walking)
    smooth_error = sum(map(distance_to_true_path, smoothed)) / len(smoothed)
    assert smooth_error < raw_error * 0.75

    segments = list(cleaner.clean(points))
    assert len(segments) == 1
    line = segments[0]
    # Wobble makes the raw readings look like a much longer walk.
    raw_km = segment_length_km(points[:spike_at] + points[spike_at + 1:])
    assert raw_km > 2.4
    assert 1.9 < segment_length_km(line) < 2.2
    # And far fewer points are needed to draw it.
    assert len(line) < len(points) / 4


def l_shaped_route(speed_mps, every_s, accuracy_m=10.0):
    """500 m east, then a sharp left turn and 500 m north, read perfectly."""
    points, t, travelled = [], START, 0.0
    while travelled < 1000:
        east, north = (travelled, 0) if travelled < 500 else (500, travelled - 500)
        points.append(Location(t, *offset(east, north), accuracy_m))
        t += int(every_s * 1000)
        travelled += speed_mps * every_s
    return points


@pytest.mark.parametrize('speed_mps, every_s', [(1.4, 15), (14, 10), (14, 2), (30, 5)])
def test_corners_are_not_cut_off(speed_mps, every_s):
    route = l_shaped_route(speed_mps, every_s)
    smoothed = TrackCleaner().smooth(route)
    assert max(distance_m(a, b) for a, b in zip(route, smoothed)) < 20


def test_smoothing_hardly_moves_sparse_route_points():
    cleaner = TrackCleaner()
    sparse = [Location(START + i * 600_000, *offset(i * 400, i * 300)) for i in range(4)]
    smoothed = cleaner.smooth(sparse)
    for before, after in zip(sparse, smoothed):
        assert distance_m(before, after) < 3


def test_smoothing_keeps_up_with_a_car():
    cleaner = TrackCleaner()
    car = [Location(START + i * 5_000, *offset(i * 150, 0), 10.0) for i in range(30)]
    for before, after in zip(car, cleaner.smooth(car)):
        assert distance_m(before, after) < 1


def test_simplify_keeps_the_shape_within_tolerance():
    cleaner = TrackCleaner(CleanerSettings(simplify_m=5))
    # An L-shaped route with many points along each leg.
    route = [Location(START + i, *offset(i * 10, 0)) for i in range(50)]
    route += [Location(START + 50 + i, *offset(490, (i + 1) * 10)) for i in range(50)]
    simplified = cleaner.simplify(route)
    assert len(simplified) == 3
    assert simplified[1] == route[49]
    assert cleaner.simplify(route, tolerance_m=0) == route


def test_split_cuts_at_gaps_and_needs_two_points():
    cleaner = TrackCleaner(CleanerSettings(max_gap_minutes=30))
    points = [Location(START, *OPERA_HOUSE), Location(START + 60_000, *offset(50, 0)),
              Location(START + 3_600_000, *offset(60, 0))]
    assert [len(s) for s in cleaner.split(points)] == [2]


def test_empty_and_tiny_inputs():
    cleaner = TrackCleaner()
    assert list(cleaner.clean([])) == []
    assert list(cleaner.clean([Location(START, *OPERA_HOUSE)])) == []
    assert cleaner.smooth([]) == []
    assert cleaner.simplify([]) == []
    two = [Location(START, *OPERA_HOUSE), Location(START + 60_000, *offset(80, 0))]
    assert list(cleaner.clean(two)) == [two]


@pytest.mark.parametrize('seed', range(5))
def test_cleaning_never_invents_far_away_points(seed):
    cleaner = TrackCleaner()
    points, spike_at = noisy_walk(seed)
    real = [p for i, p in enumerate(points) if i != spike_at]
    for segment in cleaner.clean(points):
        for loc in segment:
            nearest = min(distance_m(loc, p) for p in real)
            assert nearest < 40
