"""Snaps a cleaned track onto the roads, paths and railways it most likely used.

Road layouts come from OpenStreetMap through the Overpass service. Only the
outline of each 5 km square of map is sent, never your track or its times,
and every square is kept in a local cache file so it is downloaded once.

Matching follows Newson and Krumm (2009), "Hidden Markov map matching
through noise and sparseness": each reading has a few candidate positions on
nearby roads; a candidate is more likely the closer it is to the reading,
and a step between two candidates is more likely the more its distance by
road agrees with the straight-line distance between the readings. The most
likely chain of candidates is found with the Viterbi algorithm, and the
line is drawn along the roads between them.

Where a reading has no road nearby (a park, the sea, a building) or no
believable route joins two readings, that part keeps its cleaned line.
"""
import heapq
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from math import cos, floor, inf, radians
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from models.location import Location

from .track_cleaner import EARTH_RADIUS_M, distance_m

TILE_DEG = 0.05                       # one download covers about 5.5 by 4.5 km
GRID_DEG = 0.001                      # spatial index cell, about 100 m
METRES_PER_DEG = 111_195.0
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
USER_AGENT = 'timeline-road-snap/1.0'

HIGHWAY_EXCLUDED = ('proposed|construction|abandoned|disused|platform|elevator|'
                    'bus_stop|raceway|corridor|razed|planned')
RAILWAY_KINDS = 'rail|subway|light_rail|tram|narrow_gauge|monorail'

Tile = Tuple[int, int]
Way = Tuple[int, List[int], List[Tuple[float, float]]]


class RoadDownloadError(Exception):
    pass


@dataclass(frozen=True)
class SnapSettings:
    min_search_m: float = 50.0        # look for roads at least this far from a reading
    max_search_m: float = 150.0       # ... and never further than this
    max_candidates: int = 6
    max_speed_kmh: float = 250.0      # faster travel (flights) is not snapped
    route_slack_m: float = 1500.0     # how much longer than straight a route may be ...
    route_factor: float = 2.5         # ... or this many times the straight distance
    match_spacing_m: float = 10.0     # thin the cleaned line to this before matching


def tile_of(lat: float, lng: float) -> Tile:
    return floor(lat / TILE_DEG), floor(lng / TILE_DEG)


def tile_bounds(tile: Tile) -> Tuple[float, float, float, float]:
    """(south, west, north, east) in degrees."""
    south, west = tile[0] * TILE_DEG, tile[1] * TILE_DEG
    return (round(south, 6), round(west, 6),
            round(south + TILE_DEG, 6), round(west + TILE_DEG, 6))


def tiles_for(points: Sequence[Location], margin_deg: float = 0.002) -> List[Tile]:
    """Every map square the line passes through or comes close to."""
    tiles: Set[Tile] = set()

    def add(lat: float, lng: float) -> None:
        for dlat in (-margin_deg, 0.0, margin_deg):
            for dlng in (-margin_deg, 0.0, margin_deg):
                tiles.add(tile_of(lat + dlat, lng + dlng))

    for a, b in zip(points, points[1:]):
        steps = max(1, int(distance_m(a, b) // 1000))
        for k in range(steps):
            f = k / steps
            add(a.latitude + (b.latitude - a.latitude) * f,
                a.longitude + (b.longitude - a.longitude) * f)
    for p in points[-1:]:
        add(p.latitude, p.longitude)
    return sorted(tiles)


# ----------------------------------------------------------------------
# Downloading and caching road layouts
# ----------------------------------------------------------------------

class OverpassFetcher:
    """Downloads the roads, paths and railways inside one map square."""

    def __init__(self, url: str = OVERPASS_URL, timeout_s: float = 120.0,
                 min_interval_s: float = 1.0) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._min_interval_s = min_interval_s
        self._last_request = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def query(south: float, west: float, north: float, east: float) -> str:
        box = f'{south},{west},{north},{east}'
        return (
            '[out:json][timeout:90];('
            f'way["highway"]["highway"!~"^({HIGHWAY_EXCLUDED})$"]({box});'
            f'way["railway"~"^({RAILWAY_KINDS})$"]({box});'
            ');out geom;'
        )

    def __call__(self, tile: Tile) -> List[Way]:
        body = urllib.parse.urlencode({'data': self.query(*tile_bounds(tile))}).encode()
        request = urllib.request.Request(
            self._url, data=body,
            headers={'User-Agent': USER_AGENT,
                     'Content-Type': 'application/x-www-form-urlencoded'})
        with self._lock:  # one request at a time, politely spaced
            wait = self._last_request + self._min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                    payload = response.read()
            except (urllib.error.URLError, OSError) as error:
                raise RoadDownloadError(str(error)) from error
            finally:
                self._last_request = time.monotonic()
        try:
            return parse_overpass(json.loads(payload))
        except (ValueError, KeyError, TypeError) as error:
            raise RoadDownloadError(f'unreadable answer: {error}') from error


def parse_overpass(data: dict) -> List[Way]:
    ways: List[Way] = []
    for element in data.get('elements', []):
        if element.get('type') != 'way':
            continue
        nodes = element.get('nodes') or []
        geometry = element.get('geometry') or []
        if len(nodes) < 2 or len(nodes) != len(geometry):
            continue
        points = []
        for point in geometry:
            if not point or point.get('lat') is None or point.get('lon') is None:
                break
            points.append((float(point['lat']), float(point['lon'])))
        if len(points) == len(nodes):
            ways.append((int(element['id']), [int(n) for n in nodes], points))
    return ways


class RoadCache:
    """Downloaded map squares, kept in their own SQLite file."""

    def __init__(self, path: str) -> None:
        self._path = path
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS road_tiles ('
                         'lat_index INTEGER, lng_index INTEGER, fetched_at TEXT, '
                         'ways BLOB, PRIMARY KEY (lat_index, lng_index))')

    def get(self, tile: Tile) -> Optional[List[Way]]:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute('SELECT ways FROM road_tiles WHERE lat_index = ? '
                               'AND lng_index = ?', tile).fetchone()
        if row is None:
            return None
        return [(w[0], w[1], [tuple(p) for p in w[2]])
                for w in json.loads(zlib.decompress(row[0]))]

    def put(self, tile: Tile, ways: List[Way]) -> None:
        blob = zlib.compress(json.dumps(ways, separators=(',', ':')).encode(), 6)
        with sqlite3.connect(self._path) as conn:
            conn.execute('INSERT OR REPLACE INTO road_tiles VALUES (?, ?, ?, ?)',
                         (tile[0], tile[1], time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                          blob))


# ----------------------------------------------------------------------
# The road network
# ----------------------------------------------------------------------

@dataclass
class Candidate:
    edge: int
    fraction: float          # 0 at the edge's first node, 1 at its second
    distance_m: float        # from the reading
    latitude: float
    longitude: float


class RoadNetwork:
    def __init__(self, ways: Iterable[Way]) -> None:
        self.nodes: Dict[int, Tuple[float, float]] = {}
        self.adjacent: Dict[int, List[Tuple[int, float]]] = {}
        self.edges: List[Tuple[int, int, float]] = []    # (node a, node b, length m)
        self._grid: Dict[Tuple[int, int], List[int]] = {}
        seen_ways: Set[int] = set()
        seen_edges: Set[Tuple[int, int]] = set()
        for way_id, node_ids, points in ways:
            if way_id in seen_ways:
                continue
            seen_ways.add(way_id)
            for node, point in zip(node_ids, points):
                self.nodes[node] = point
            for a, b in zip(node_ids, node_ids[1:]):
                key = (a, b) if a < b else (b, a)
                if a == b or key in seen_edges:
                    continue
                seen_edges.add(key)
                self._add_edge(a, b)

    def _add_edge(self, a: int, b: int) -> None:
        (lat1, lng1), (lat2, lng2) = self.nodes[a], self.nodes[b]
        length = distance_m(Location(0, lat1, lng1), Location(0, lat2, lng2))
        index = len(self.edges)
        self.edges.append((a, b, length))
        self.adjacent.setdefault(a, []).append((b, length))
        self.adjacent.setdefault(b, []).append((a, length))
        for row in range(floor(min(lat1, lat2) / GRID_DEG), floor(max(lat1, lat2) / GRID_DEG) + 1):
            for col in range(floor(min(lng1, lng2) / GRID_DEG),
                             floor(max(lng1, lng2) / GRID_DEG) + 1):
                self._grid.setdefault((row, col), []).append(index)

    def __len__(self) -> int:
        return len(self.edges)

    def candidates(self, lat: float, lng: float, radius_m: float, limit: int) -> List[Candidate]:
        """The nearest points on roads within `radius_m`, closest first."""
        metres_per_lng = METRES_PER_DEG * cos(radians(lat))
        dlat, dlng = radius_m / METRES_PER_DEG, radius_m / metres_per_lng
        found: Dict[int, Candidate] = {}
        for row in range(floor((lat - dlat) / GRID_DEG), floor((lat + dlat) / GRID_DEG) + 1):
            for col in range(floor((lng - dlng) / GRID_DEG), floor((lng + dlng) / GRID_DEG) + 1):
                for index in self._grid.get((row, col), ()):
                    if index in found:
                        continue
                    a, b, _ = self.edges[index]
                    (lat1, lng1), (lat2, lng2) = self.nodes[a], self.nodes[b]
                    ax, ay = (lng1 - lng) * metres_per_lng, (lat1 - lat) * METRES_PER_DEG
                    bx, by = (lng2 - lng) * metres_per_lng, (lat2 - lat) * METRES_PER_DEG
                    dx, dy = bx - ax, by - ay
                    length_sq = dx * dx + dy * dy
                    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
                    px, py = ax + t * dx, ay + t * dy
                    distance = (px * px + py * py) ** 0.5
                    if distance <= radius_m:
                        found[index] = Candidate(index, t, distance,
                                                 lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t)
        return sorted(found.values(), key=lambda c: c.distance_m)[:limit]

    def routes_from(self, start: Candidate, targets: List[Candidate],
                    cutoff_m: float) -> Tuple[List[float], Dict[int, Tuple[float, int]]]:
        """Distances by road from `start` to each target (inf if beyond `cutoff_m`).

        Also returns the search tree, so the chosen route can be drawn.
        """
        a, b, length = self.edges[start.edge]
        best: Dict[int, Tuple[float, int]] = {}
        heap = [(start.fraction * length, a, -1), ((1 - start.fraction) * length, b, -1)]
        wanted: Set[int] = set()
        for target in targets:
            ta, tb, _ = self.edges[target.edge]
            wanted.update((ta, tb))
        while heap and wanted:
            dist, node, parent = heapq.heappop(heap)
            if node in best:
                continue
            best[node] = (dist, parent)
            wanted.discard(node)
            if dist > cutoff_m:
                break
            for neighbour, step in self.adjacent.get(node, ()):
                if neighbour not in best:
                    heapq.heappush(heap, (dist + step, neighbour, node))

        results = []
        for target in targets:
            ta, tb, t_length = self.edges[target.edge]
            if target.edge == start.edge:
                results.append(abs(target.fraction - start.fraction) * length)
                continue
            options = []
            if ta in best:
                options.append(best[ta][0] + target.fraction * t_length)
            if tb in best:
                options.append(best[tb][0] + (1 - target.fraction) * t_length)
            route = min(options) if options else inf
            results.append(route if route <= cutoff_m else inf)
        return results, best

    def route_points(self, start: Candidate, end: Candidate,
                     tree: Dict[int, Tuple[float, int]]) -> List[Tuple[float, float]]:
        """The road geometry from `start` to `end`, using a tree from routes_from."""
        if start.edge == end.edge:
            return [(start.latitude, start.longitude), (end.latitude, end.longitude)]
        ea, eb, e_length = self.edges[end.edge]
        via_a = tree[ea][0] + end.fraction * e_length if ea in tree else inf
        via_b = tree[eb][0] + (1 - end.fraction) * e_length if eb in tree else inf
        node = ea if via_a <= via_b else eb
        chain = []
        while node != -1:
            chain.append(self.nodes[node])
            node = tree[node][1]
        chain.reverse()
        return [(start.latitude, start.longitude)] + chain + [(end.latitude, end.longitude)]


# ----------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------

@dataclass
class SnapResult:
    segments: List[List[Location]]
    snapped_km: float = 0.0
    unsnapped_km: float = 0.0
    missing_tiles: int = 0
    failed_tiles: int = 0


class RoadSnapper:
    def __init__(self, cache: RoadCache, fetcher: Callable[[Tile], List[Way]],
                 settings: Optional[SnapSettings] = None) -> None:
        self._cache = cache
        self._fetch = fetcher
        self.settings = settings or SnapSettings()
        self._lock = threading.Lock()

    def snap(self, segments: List[List[Location]], max_new_tiles: int) -> SnapResult:
        """Snap each segment, downloading at most `max_new_tiles` new map squares."""
        result = SnapResult(segments=[])
        snappable = [s for s in segments if self._slow_enough(s)]
        wanted: List[Tile] = sorted({t for s in snappable for t in tiles_for(s)})
        ways: List[Way] = []
        unavailable: Set[Tile] = set()
        with self._lock:
            for tile in wanted:
                cached = self._cache.get(tile)
                if cached is None:
                    if max_new_tiles <= 0:
                        unavailable.add(tile)
                        result.missing_tiles += 1
                        continue
                    max_new_tiles -= 1
                    try:
                        cached = self._fetch(tile)
                    except RoadDownloadError:
                        unavailable.add(tile)
                        result.failed_tiles += 1
                        continue
                    self._cache.put(tile, cached)
                ways.extend(cached)
        network = RoadNetwork(ways)

        for segment in segments:
            if (segment in snappable and len(network)
                    and not unavailable.intersection(tiles_for(segment))):
                pieces = self._match(segment, network)
            else:
                pieces = [(False, segment)]
            joined: List[Location] = []
            for snapped, piece in pieces:
                km = sum(distance_m(a, b) for a, b in zip(piece, piece[1:])) / 1000
                if snapped:
                    result.snapped_km += km
                else:
                    result.unsnapped_km += km
                if joined and piece and joined[-1].timestamp == piece[0].timestamp:
                    piece = piece[1:]
                joined.extend(piece)
            if len(joined) > 1:
                result.segments.append(joined)
        return result

    def _slow_enough(self, segment: List[Location]) -> bool:
        seconds = (segment[-1].timestamp - segment[0].timestamp) / 1000
        if seconds <= 0:
            return False
        km = sum(distance_m(a, b) for a, b in zip(segment, segment[1:])) / 1000
        return km / (seconds / 3600) <= self.settings.max_speed_kmh

    def _thin(self, segment: List[Location]) -> List[Location]:
        kept = [segment[0]]
        for loc in segment[1:-1]:
            if distance_m(kept[-1], loc) >= self.settings.match_spacing_m:
                kept.append(loc)
        if len(segment) > 1:
            kept.append(segment[-1])
        return kept

    def _match(self, segment: List[Location], network: RoadNetwork) -> List[Tuple[bool, List[Location]]]:
        """Split into runs that match the roads and runs that keep their line."""
        settings = self.settings
        points = self._thin(segment)
        pieces: List[Tuple[bool, List[Location]]] = []
        chain: List[Tuple[int, List[Candidate], List[float], List[int]]] = []

        def search_radius(loc: Location) -> float:
            accuracy = loc.accuracy_m if loc.accuracy_m is not None else settings.min_search_m
            return max(settings.min_search_m, min(settings.max_search_m, accuracy * 3))

        def finish_chain() -> None:
            if len(chain) >= 2:
                pieces.append((True, self._draw_chain(points, chain, network)))
            elif chain:
                pieces.append((False, [points[chain[0][0]]]))
            chain.clear()

        for i, loc in enumerate(points):
            candidates = network.candidates(loc.latitude, loc.longitude,
                                            search_radius(loc), settings.max_candidates)
            if not candidates:
                finish_chain()
                pieces.append((False, [loc]))
                continue
            sigma = max(8.0, min(50.0, loc.accuracy_m or 25.0))
            emission = [-0.5 * (c.distance_m / sigma) ** 2 for c in candidates]
            if not chain:
                chain.append((i, candidates, emission, [-1] * len(candidates)))
                continue

            prev_index, prev_candidates, prev_scores, _ = chain[-1]
            prev = points[prev_index]
            straight = distance_m(prev, loc)
            seconds = max((loc.timestamp - prev.timestamp) / 1000, 1.0)
            cutoff = min(max(straight * settings.route_factor, straight + settings.route_slack_m),
                         seconds * settings.max_speed_kmh / 3.6 + 200)
            beta = 10.0 + 0.1 * straight
            scores = [-inf] * len(candidates)
            back = [-1] * len(candidates)
            for j, (source, score) in enumerate(zip(prev_candidates, prev_scores)):
                if score == -inf:
                    continue
                routes, _ = network.routes_from(source, candidates, cutoff)
                for k, route in enumerate(routes):
                    if route == inf:
                        continue
                    total = score - abs(route - straight) / beta + emission[k]
                    if total > scores[k]:
                        scores[k], back[k] = total, j
            if max(scores) == -inf:
                finish_chain()
                chain.append((i, candidates, emission, [-1] * len(candidates)))
            else:
                chain.append((i, candidates, scores, back))
        finish_chain()

        # Join neighbouring pieces so the line stays continuous.
        merged: List[Tuple[bool, List[Location]]] = []
        for snapped, piece in pieces:
            if merged and merged[-1][0] == snapped and not snapped:
                merged[-1][1].extend(piece)
            else:
                merged.append((snapped, list(piece)))
        result: List[Tuple[bool, List[Location]]] = []
        for k, (snapped, piece) in enumerate(merged):
            if not snapped and k + 1 < len(merged):
                piece = piece + merged[k + 1][1][:1]       # bridge to the next snapped run
            if not snapped and k > 0:
                piece = result[-1][1][-1:] + piece
            result.append((snapped, piece))
        return [(s, p) for s, p in result if len(p) > 1 or s]

    def _draw_chain(self, points: List[Location], chain, network: RoadNetwork) -> List[Location]:
        # Follow the back-pointers from the best final candidate.
        best = max(range(len(chain[-1][2])), key=lambda k: chain[-1][2][k])
        picks: List[Tuple[int, Candidate]] = []
        for index, candidates, _, back in reversed(chain):
            picks.append((index, candidates[best]))
            best = back[best]
        picks.reverse()

        line: List[Location] = []
        for (i, start), (j, end) in zip(picks, picks[1:]):
            _, tree = network.routes_from(start, [end], inf)
            geometry = network.route_points(start, end, tree)
            t0, t1 = points[i].timestamp, points[j].timestamp
            lengths = [0.0]
            for (la, lo), (lb, lob) in zip(geometry, geometry[1:]):
                lengths.append(lengths[-1] + distance_m(Location(0, la, lo), Location(0, lb, lob)))
            total = lengths[-1] or 1.0
            for (lat, lng), along in zip(geometry, lengths):
                stamp = t0 + int((t1 - t0) * along / total)
                if line and (stamp < line[-1].timestamp or
                             (line[-1].latitude, line[-1].longitude) == (lat, lng)):
                    continue
                line.append(Location(stamp, lat, lng, None))
        if picks and (not line or line[-1].timestamp != points[picks[-1][0]].timestamp):
            last = picks[-1][1]
            line.append(Location(points[picks[-1][0]].timestamp, last.latitude, last.longitude, None))
        return _remove_spurs(line)


def _remove_spurs(line: List[Location]) -> List[Location]:
    """Drop out-and-back detours: A, B, A becomes A."""
    result: List[Location] = []
    for loc in line:
        result.append(loc)
        while len(result) >= 3 and distance_m(result[-3], result[-1]) < 1.0:
            del result[-2:]
    return result
