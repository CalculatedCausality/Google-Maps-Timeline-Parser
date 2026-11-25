"""A map you open in your browser, drawn straight from the database.

Nothing is written to disk: each view asks this small local server for the
lines, visits, readings or heatmap of the dates you pick. The server only
listens on this computer (127.0.0.1), opens the database read-only, and
refuses requests addressed to any other host name, so a web page you visit
elsewhere cannot read your history through it.
"""
import json
import threading
from datetime import date, datetime, timedelta, timezone, tzinfo
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import log
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from repositories.timeline_repository import TimelineRepository

from .coverage_service import CoverageService
from .track_cleaner import CleanerSettings, TrackCleaner, segment_length_km

WEB_FOLDER = Path(__file__).resolve().parent.parent / 'web'
STATIC_FILES = {'/': 'index.html', '/index.html': 'index.html',
                '/app.js': 'app.js', '/app.css': 'app.css'}
CONTENT_TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}

MAX_TRACK_READINGS = 3_000_000
MAX_DOTS = 30_000
MAX_VISITS = 20_000
MAX_HEAT_CELLS = 150_000
CACHE_ENTRIES = 24

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com; "
    "img-src 'self' data: https://tile.openstreetmap.org; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class BadRequest(Exception):
    pass


def simplify_tolerance_m(days: int) -> float:
    """Longer periods are drawn with fewer points, as detail would not show anyway."""
    if days <= 2:
        return 5.0
    if days <= 14:
        return 10.0
    if days <= 62:
        return 25.0
    if days <= 400:
        return 60.0
    return 150.0


class MapData:
    """Answers the viewer's questions from the database, with a small cache."""

    def __init__(self, repository: TimelineRepository, zone: tzinfo, zone_name: str,
                 max_accuracy_m: Optional[float], max_gap_minutes: float) -> None:
        self._repo = repository
        self._zone = zone
        self._zone_name = zone_name
        self._max_accuracy_m = max_accuracy_m
        self._max_gap_minutes = max_gap_minutes
        self._cache: Dict[Any, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Dates
    # ------------------------------------------------------------------

    def _local_midnight_ms(self, day: date) -> int:
        moment = datetime(day.year, day.month, day.day, tzinfo=self._zone)
        return int(moment.timestamp() * 1000)

    def _local_text(self, t_ms: int) -> str:
        return datetime.fromtimestamp(t_ms / 1000, tz=self._zone).strftime('%Y-%m-%d %H:%M')

    def _local_day(self, t_ms: int) -> str:
        return datetime.fromtimestamp(t_ms / 1000, tz=self._zone).strftime('%Y-%m-%d')

    def date_range(self, query: Dict[str, List[str]]) -> Tuple[int, int, int]:
        """(start ms, end ms, number of days) for inclusive local dates `from` and `to`."""
        def read(name: str) -> date:
            values = query.get(name)
            if not values:
                raise BadRequest(f'"{name}" date is missing')
            try:
                return date.fromisoformat(values[0])
            except ValueError:
                raise BadRequest(f'"{name}" must be a date like 2019-03-31')
        first, last = read('from'), read('to')
        if last < first:
            raise BadRequest('"to" is before "from"')
        start = self._local_midnight_ms(first)
        end = self._local_midnight_ms(last + timedelta(days=1))
        return start, end, (last - first).days + 1

    def _cached(self, key: Tuple, build):
        key = (self._repo.data_version(),) + key
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = build()
        with self._lock:
            if len(self._cache) >= CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = value
        return value

    # ------------------------------------------------------------------
    # Answers
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        def build():
            report = CoverageService(self._repo, self._zone, self._zone_name).build()
            return {
                'timezone': self._zone_name,
                'days': [day.isoformat() for day in report.days],
            }
        return self._cached(('summary',), build)

    def track(self, query: Dict[str, List[str]]) -> dict:
        start, end, days = self.date_range(query)
        clean = query.get('clean', ['1'])[0] != '0'
        return self._cached(('track', start, end, clean),
                            lambda: self._build_track(start, end, days, clean))

    def _build_track(self, start: int, end: int, days: int, clean: bool) -> dict:
        cleaner = TrackCleaner(CleanerSettings(
            max_gap_minutes=self._max_gap_minutes,
            simplify_m=simplify_tolerance_m(days),
        ))
        readings = 0
        truncated = False

        def counted():
            nonlocal readings, truncated
            for loc in self._repo.iter_track(start, end, self._max_accuracy_m):
                readings += 1
                if readings > MAX_TRACK_READINGS:
                    truncated = True
                    return
                yield loc

        if clean:
            segments = cleaner.clean(counted())
        else:
            # Unchanged readings, only thinned out for long periods.
            segments = (cleaner.simplify(segment, 0 if days <= 2 else None)
                        for segment in cleaner.raw_segments(counted()))

        features = []
        points_out = 0
        total_km = 0.0
        for segment in segments:
            km = segment_length_km(segment)
            total_km += km
            points_out += len(segment)
            features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[round(loc.longitude, 6), round(loc.latitude, 6)]
                                    for loc in segment],
                },
                'properties': {
                    'day': self._local_day(segment[0].timestamp),
                    'start': self._local_text(segment[0].timestamp),
                    'end': self._local_text(segment[-1].timestamp),
                    'km': round(km, 2),
                },
            })
        return {
            'type': 'FeatureCollection',
            'features': features,
            'stats': {
                'readings': min(readings, MAX_TRACK_READINGS),
                'points': points_out,
                'segments': len(features),
                'km': round(total_km, 2),
                'truncated': truncated,
            },
        }

    def readings(self, query: Dict[str, List[str]]) -> dict:
        start, end, _ = self.date_range(query)
        rows = self._repo.readings_between(start, end, self._max_accuracy_m, MAX_DOTS + 1)
        return {
            'points': [[round(lat, 6), round(lng, 6), acc, self._local_text(t)]
                       for t, lat, lng, acc in rows[:MAX_DOTS]],
            'truncated': len(rows) > MAX_DOTS,
        }

    def visits(self, query: Dict[str, List[str]]) -> dict:
        start, end, _ = self.date_range(query)
        rows = self._repo.visits_between(start, end, MAX_VISITS + 1)
        return {
            'visits': [{
                'start': self._local_text(start_ms),
                'end': self._local_text(end_ms),
                'lat': round(lat, 6),
                'lng': round(lng, 6),
                'name': name,
                'address': address,
                'type': semantic_type,
                'probability': probability,
            } for start_ms, end_ms, lat, lng, name, address, semantic_type, probability
                in rows[:MAX_VISITS]],
            'truncated': len(rows) > MAX_VISITS,
        }

    def heat(self, query: Dict[str, List[str]]) -> dict:
        start, end, days = self.date_range(query)

        def build():
            decimals = 4 if days <= 31 else 3
            cells = self._repo.heat_cells(start, end, decimals, self._max_accuracy_m)
            if len(cells) > MAX_HEAT_CELLS and decimals > 2:
                cells = self._repo.heat_cells(start, end, decimals - 1, self._max_accuracy_m)
            # Damp the weight so home and work do not drown everything else out.
            return {'cells': [[lat, lng, round(1.0 + log(count), 3)]
                              for lat, lng, count in cells]}
        return self._cached(('heat', start, end), build)


class MapServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, data: MapData) -> None:
        super().__init__(('127.0.0.1', port), MapRequestHandler)
        self.data = data
        actual = self.server_address[1]
        self.allowed_hosts = {f'127.0.0.1:{actual}', f'localhost:{actual}'}

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server_address[1]}/'


class MapRequestHandler(BaseHTTPRequestHandler):
    server: MapServer
    server_version = 'TimelineMap'
    sys_version = ''

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep the terminal quiet; dates in requests are personal

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get('Host', '') not in self.server.allowed_hosts:
            self._send_json({'error': 'Unknown host'}, HTTPStatus.FORBIDDEN)
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        answers = {
            '/api/summary': lambda: self.server.data.summary(),
            '/api/track': lambda: self.server.data.track(query),
            '/api/readings': lambda: self.server.data.readings(query),
            '/api/visits': lambda: self.server.data.visits(query),
            '/api/heat': lambda: self.server.data.heat(query),
        }
        if url.path in answers:
            try:
                self._send_json(answers[url.path]())
            except BadRequest as error:
                self._send_json({'error': str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if url.path in STATIC_FILES:
            self._send_file(WEB_FOLDER / STATIC_FILES[url.path])
            return
        self._send_json({'error': 'Not found'}, HTTPStatus.NOT_FOUND)

    def _security_headers(self) -> None:
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Cache-Control', 'no-store')

    def _send_json(self, body: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(body, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path: Path) -> None:
        payload = path.read_bytes()
        content_type = CONTENT_TYPES[path.suffix]
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', f'{content_type}; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Content-Security-Policy', CONTENT_SECURITY_POLICY)
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)


def build_server(repository: TimelineRepository, port: int, zone: tzinfo = timezone.utc,
                 zone_name: str = 'UTC', max_accuracy_m: Optional[float] = 200.0,
                 max_gap_minutes: float = 30.0) -> MapServer:
    data = MapData(repository, zone, zone_name, max_accuracy_m, max_gap_minutes)
    return MapServer(port, data)
