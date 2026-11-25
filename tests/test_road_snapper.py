import random
from math import cos, radians

import pytest

from conftest import OPERA_HOUSE
from models.location import Location
from services.road_snapper import (OverpassFetcher, RoadCache, RoadDownloadError, RoadNetwork,
                                   RoadSnapper, parse_overpass, tile_bounds, tile_of, tiles_for)
from services.track_cleaner import distance_m

START = 1_700_000_000_000
METRES_PER_LAT = 111_195.0
METRES_PER_LNG = METRES_PER_LAT * cos(radians(OPERA_HOUSE[0]))


def at(east_m, north_m):
    return (OPERA_HOUSE[0] + north_m / METRES_PER_LAT,
            OPERA_HOUSE[1] + east_m / METRES_PER_LNG)


def grid_ways(size_m=1000, spacing_m=100, step_m=50):
    """A made-up town: streets every 100 m in both directions."""
    ways, next_node, node_ids = [], [1], {}

    def node(east, north):
        key = (east, north)
        if key not in node_ids:
            node_ids[key] = next_node[0]
            next_node[0] += 1
        return node_ids[key]

    way_id = 1000
    for line in range(0, size_m + 1, spacing_m):
        for horizontal in (True, False):
            coords = [(s, line) if horizontal else (line, s) for s in range(0, size_m + 1, step_m)]
            ways.append((way_id, [node(e, n) for e, n in coords], [at(e, n) for e, n in coords]))
            way_id += 1
    return ways


class FakeFetcher:
    def __init__(self, ways, fail=False):
        self.ways, self.fail, self.calls = ways, fail, []

    def __call__(self, tile):
        self.calls.append(tile)
        if self.fail:
            raise RoadDownloadError('offline')
        return self.ways


def on_a_street(loc, spacing_m=100):
    east = (loc.longitude - OPERA_HOUSE[1]) * METRES_PER_LNG
    north = (loc.latitude - OPERA_HOUSE[0]) * METRES_PER_LAT
    off_east = abs(east - round(east / spacing_m) * spacing_m)
    off_north = abs(north - round(north / spacing_m) * spacing_m)
    return min(off_east, off_north) < 1.0


def noisy_l_walk(seed=5, noise_m=15):
    """Along the street at north=200 to east=500, then north up to 600."""
    rng = random.Random(seed)
    points, t, travelled = [], START, 0.0
    while travelled <= 800:
        east, north = (travelled, 200) if travelled < 400 else (400, 200 + travelled - 400)
        east += 100
        lat, lng = at(east + rng.gauss(0, noise_m), north + rng.gauss(0, noise_m))
        points.append(Location(t, lat, lng, 20.0))
        t += 15_000
        travelled += 21
    return points


@pytest.fixture
def cache(tmp_path):
    return RoadCache(str(tmp_path / 'roads.sqlite'))


def test_noisy_walk_is_drawn_along_the_streets(cache):
    fetcher = FakeFetcher(grid_ways())
    walk = noisy_l_walk()
    result = RoadSnapper(cache, fetcher).snap([walk], max_new_tiles=10)

    assert result.missing_tiles == 0 and result.failed_tiles == 0
    assert len(result.segments) == 1
    line = result.segments[0]
    assert all(on_a_street(loc) for loc in line)
    # It turns the corner at (500, 200) instead of cutting across the block.
    corner = Location(0, *at(500, 200))
    assert min(distance_m(loc, corner) for loc in line) < 1
    km = sum(distance_m(a, b) for a, b in zip(line, line[1:])) / 1000
    assert 0.78 < km < 0.9      # true route 0.8 km; the ends wobble a little
    assert result.snapped_km == pytest.approx(km)
    assert result.unsnapped_km == 0
    # Times only ever move forward.
    assert all(b.timestamp > a.timestamp for a, b in zip(line, line[1:]))
    assert line[0].timestamp == walk[0].timestamp and line[-1].timestamp == walk[-1].timestamp


def test_roads_are_downloaded_once_per_square(cache):
    fetcher = FakeFetcher(grid_ways())
    snapper = RoadSnapper(cache, fetcher)
    snapper.snap([noisy_l_walk()], max_new_tiles=10)
    first_calls = len(fetcher.calls)
    assert first_calls >= 1
    snapper.snap([noisy_l_walk(seed=9)], max_new_tiles=10)
    assert len(fetcher.calls) == first_calls


def test_without_roads_the_line_is_kept(cache):
    walk = noisy_l_walk()

    no_budget = RoadSnapper(cache, FakeFetcher(grid_ways())).snap([walk], max_new_tiles=0)
    assert no_budget.missing_tiles >= 1
    assert no_budget.segments == [walk]

    offline = RoadSnapper(cache, FakeFetcher([], fail=True)).snap([walk], max_new_tiles=5)
    assert offline.failed_tiles >= 1
    assert offline.segments == [walk]


def test_readings_away_from_roads_keep_their_line(cache):
    """Leave the town for open country 2 km away, then come back."""
    fetcher = FakeFetcher(grid_ways())
    walk = [Location(START + i * 60_000, *at(500, 100 * i), 10.0) for i in range(6)]
    outside = [Location(START + (6 + i) * 60_000, *at(500, 2600 + 50 * i), 10.0) for i in range(4)]
    result = RoadSnapper(cache, fetcher).snap([walk + outside], max_new_tiles=20)

    line = result.segments[0]
    assert result.unsnapped_km > 0 and result.snapped_km > 0
    assert line[-1] == outside[-1]                       # untouched at the end
    assert all(on_a_street(loc) for loc in line if loc.timestamp <= walk[-1].timestamp)


def test_flights_are_not_snapped(cache):
    fetcher = FakeFetcher(grid_ways())
    flight = [Location(START, *at(0, 0)), Location(START + 3_600_000, -27.4689, 153.0235)]
    result = RoadSnapper(cache, fetcher).snap([flight], max_new_tiles=10)
    assert result.segments == [flight]
    assert fetcher.calls == []


def test_network_candidates_and_routes():
    network = RoadNetwork(grid_ways())
    candidates = network.candidates(*at(130, 205), radius_m=30, limit=6)
    assert candidates and candidates[0].distance_m == pytest.approx(5, abs=0.5)
    start = network.candidates(*at(0, 0), radius_m=5, limit=1)[0]
    end = network.candidates(*at(300, 400), radius_m=5, limit=1)[0]
    routes, tree = network.routes_from(start, [end], cutoff_m=10_000)
    assert routes[0] == pytest.approx(700, rel=0.01)       # around the blocks
    geometry = network.route_points(start, end, tree)
    assert geometry[0] == pytest.approx((start.latitude, start.longitude))
    assert geometry[-1] == pytest.approx((end.latitude, end.longitude))
    too_far, _ = network.routes_from(start, [end], cutoff_m=100)
    assert too_far == [float('inf')]


def test_tiles_cover_the_line_and_its_edges():
    points = [Location(START, -33.95, 151.10), Location(START + 1, -33.80, 151.30)]
    tiles = tiles_for(points)
    assert tile_of(-33.95, 151.10) in tiles and tile_of(-33.80, 151.30) in tiles
    assert len(tiles) < 30
    south, west, north, east = tile_bounds(tile_of(-33.8568, 151.2153))
    assert south <= -33.8568 < north and west <= 151.2153 < east


def test_cache_round_trip(cache):
    ways = grid_ways()[:3]
    assert cache.get((1, 2)) is None
    cache.put((1, 2), ways)
    assert cache.get((1, 2)) == ways


def test_overpass_answer_is_parsed():
    answer = {'elements': [
        {'type': 'way', 'id': 7, 'nodes': [1, 2],
         'geometry': [{'lat': -33.1, 'lon': 151.1}, {'lat': -33.2, 'lon': 151.2}],
         'tags': {'highway': 'footway'}},
        {'type': 'way', 'id': 8, 'nodes': [3], 'geometry': [{'lat': 1, 'lon': 2}]},
        {'type': 'way', 'id': 9, 'nodes': [4, 5], 'geometry': [{'lat': 1, 'lon': 2}, None]},
        {'type': 'node', 'id': 10, 'lat': 0, 'lon': 0},
    ]}
    assert parse_overpass(answer) == [(7, [1, 2], [(-33.1, 151.1), (-33.2, 151.2)])]


def test_overpass_query_asks_only_for_a_box():
    query = OverpassFetcher.query(-33.9, 151.2, -33.85, 151.25)
    assert '(-33.9,151.2,-33.85,151.25)' in query
    assert 'highway' in query and 'railway' in query and 'out geom' in query


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code):
    import urllib.error
    return urllib.error.HTTPError('https://example.invalid', code, 'Busy', {}, None)


def test_busy_service_is_retried_then_succeeds(monkeypatch):
    import json
    import urllib.request
    answers = [http_error(429), http_error(504),
               FakeResponse(json.dumps({'elements': []}).encode())]
    sent = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    fetcher = OverpassFetcher(min_interval_s=0)
    fetcher.retry_waits_s = (0, 0, 0)
    assert fetcher((0, 0)) == []
    assert len(sent) == 3
    assert sent[0].get_header('User-agent') == 'timeline-road-snap/1.0'
    assert b'data=' in sent[0].data


def test_refused_request_is_not_retried_and_reason_is_kept(monkeypatch, cache):
    import urllib.request
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise http_error(400)

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    fetcher = OverpassFetcher(min_interval_s=0)
    fetcher.retry_waits_s = (0, 0, 0)
    result = RoadSnapper(cache, fetcher).snap([noisy_l_walk()], max_new_tiles=5)
    assert len(calls) == result.failed_tiles >= 1      # each square tried once, no retry
    assert '400' in result.failure
