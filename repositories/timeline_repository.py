import heapq
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from database.connection import DatabaseConnection
from models.location import Location
from models.records import Activity, FrequentPlace, PathPoint, Position, Visit
from services.parsing_utils import iso_utc

SCHEMA = '''
CREATE TABLE IF NOT EXISTS source_files (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    format      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    records     INTEGER NOT NULL DEFAULT 0,
    new_records INTEGER NOT NULL DEFAULT 0,
    skipped     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES source_files(id),
    record_type TEXT NOT NULL,
    t_ms        INTEGER NOT NULL,
    time_utc    TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    accuracy_m  REAL,
    source      TEXT,
    device_tag  TEXT,
    altitude_m  REAL,
    speed_mps   REAL,
    UNIQUE (t_ms, latitude, longitude, record_type)
);
CREATE TABLE IF NOT EXISTS path_points (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES source_files(id),
    t_ms        INTEGER NOT NULL,
    time_utc    TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    UNIQUE (t_ms, latitude, longitude)
);
CREATE TABLE IF NOT EXISTS visits (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES source_files(id),
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    start_utc      TEXT NOT NULL,
    end_utc        TEXT NOT NULL,
    utc_offset_min INTEGER,
    latitude       REAL,
    longitude      REAL,
    place_id       TEXT,
    semantic_type  TEXT,
    name           TEXT,
    address        TEXT,
    probability    REAL,
    confidence     TEXT,
    UNIQUE (start_ms, end_ms, latitude, longitude)
);
CREATE TABLE IF NOT EXISTS activities (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES source_files(id),
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    start_utc       TEXT NOT NULL,
    end_utc         TEXT NOT NULL,
    utc_offset_min  INTEGER,
    activity_type   TEXT NOT NULL,
    distance_m      REAL,
    probability     REAL,
    confidence      TEXT,
    start_latitude  REAL,
    start_longitude REAL,
    end_latitude    REAL,
    end_longitude   REAL,
    UNIQUE (start_ms, end_ms, activity_type)
);
CREATE TABLE IF NOT EXISTS frequent_places (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_files(id),
    place_id  TEXT,
    label     TEXT,
    latitude  REAL,
    longitude REAL,
    UNIQUE (place_id, label, latitude, longitude)
);
'''

BATCH_SIZE = 20_000
DAY_MS = 86_400_000
RECORD_TABLES = ('positions', 'path_points', 'visits', 'activities', 'frequent_places')


def _position_row(source_id: int, r: Position) -> tuple:
    return (source_id, r.record_type, r.t_ms, iso_utc(r.t_ms), r.latitude, r.longitude,
            r.accuracy_m, r.source, r.device_tag, r.altitude_m, r.speed_mps)


def _path_row(source_id: int, r: PathPoint) -> tuple:
    return (source_id, r.t_ms, iso_utc(r.t_ms), r.latitude, r.longitude)


def _visit_row(source_id: int, r: Visit) -> tuple:
    return (source_id, r.start_ms, r.end_ms, iso_utc(r.start_ms), iso_utc(r.end_ms),
            r.utc_offset_min, r.latitude, r.longitude, r.place_id, r.semantic_type,
            r.name, r.address, r.probability, r.confidence)


def _activity_row(source_id: int, r: Activity) -> tuple:
    return (source_id, r.start_ms, r.end_ms, iso_utc(r.start_ms), iso_utc(r.end_ms),
            r.utc_offset_min, r.activity_type, r.distance_m, r.probability, r.confidence,
            r.start_latitude, r.start_longitude, r.end_latitude, r.end_longitude)


def _place_row(source_id: int, r: FrequentPlace) -> tuple:
    return (source_id, r.place_id, r.label, r.latitude, r.longitude)


# record type -> (table, columns, row builder)
_WRITERS = {
    Position: ('positions',
               'source_id, record_type, t_ms, time_utc, latitude, longitude, '
               'accuracy_m, source, device_tag, altitude_m, speed_mps', _position_row),
    PathPoint: ('path_points', 'source_id, t_ms, time_utc, latitude, longitude', _path_row),
    Visit: ('visits',
            'source_id, start_ms, end_ms, start_utc, end_utc, utc_offset_min, latitude, '
            'longitude, place_id, semantic_type, name, address, probability, confidence',
            _visit_row),
    Activity: ('activities',
               'source_id, start_ms, end_ms, start_utc, end_utc, utc_offset_min, '
               'activity_type, distance_m, probability, confidence, start_latitude, '
               'start_longitude, end_latitude, end_longitude', _activity_row),
    FrequentPlace: ('frequent_places', 'source_id, place_id, label, latitude, longitude',
                    _place_row),
}


class TimelineRepository:
    """Handles all persistence for Timeline records.

    Every row points back to the file it came from. Importing the same record
    twice (the same file again, or a newer export that repeats older history)
    stores it once.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def create_tables(self) -> None:
        with self._db.get_connection() as conn:
            conn.executescript(SCHEMA)

    def has_old_table(self) -> bool:
        """True when the database holds the single table earlier versions wrote."""
        with self._db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'timeline'"
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Importing
    # ------------------------------------------------------------------

    def has_source(self, sha256: str) -> bool:
        with self._db.get_connection() as conn:
            row = conn.execute(
                'SELECT 1 FROM source_files WHERE sha256 = ?', (sha256,)
            ).fetchone()
        return row is not None

    def import_source(
        self,
        name: str,
        sha256: str,
        format_name: str,
        size_bytes: int,
        records: Iterable[Any],
        skipped: Dict[str, int],
    ) -> Dict[str, Tuple[int, int]]:
        """Store one file's records in a single transaction.

        `skipped` is read after the records are consumed, so the parser can
        keep filling it in. Returns {table: (records read, records new)}.
        """
        totals: Dict[str, List[int]] = {}
        with self._db.get_connection() as conn:
            source_id = conn.execute(
                'INSERT INTO source_files (name, sha256, format, size_bytes, imported_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (name, sha256, format_name, size_bytes,
                 datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')),
            ).lastrowid

            pending: Dict[type, List[tuple]] = {kind: [] for kind in _WRITERS}

            def flush(kind: type) -> None:
                rows = pending[kind]
                if not rows:
                    return
                table, columns, _ = _WRITERS[kind]
                marks = ', '.join('?' * len(rows[0]))
                before = conn.total_changes
                conn.executemany(
                    f'INSERT OR IGNORE INTO {table} ({columns}) VALUES ({marks})', rows
                )
                total = totals.setdefault(table, [0, 0])
                total[0] += len(rows)
                total[1] += conn.total_changes - before
                rows.clear()

            for record in records:
                kind = type(record)
                pending[kind].append(_WRITERS[kind][2](source_id, record))
                if len(pending[kind]) >= BATCH_SIZE:
                    flush(kind)
            for kind in _WRITERS:
                flush(kind)

            conn.execute(
                'UPDATE source_files SET records = ?, new_records = ?, skipped = ? '
                'WHERE id = ?',
                (sum(t[0] for t in totals.values()), sum(t[1] for t in totals.values()),
                 json.dumps(skipped, sort_keys=True), source_id),
            )
        return {table: (t[0], t[1]) for table, t in totals.items()}

    # ------------------------------------------------------------------
    # Reading for the coverage report
    # ------------------------------------------------------------------

    def sources(self) -> List[dict]:
        with self._db.get_connection() as conn:
            rows = conn.execute(
                'SELECT id, name, format, size_bytes, imported_at, records, new_records, '
                'skipped FROM source_files ORDER BY id'
            ).fetchall()
        keys = ('id', 'name', 'format', 'size_bytes', 'imported_at', 'records',
                'new_records', 'skipped')
        result = [dict(zip(keys, row)) for row in rows]
        for item in result:
            item['skipped'] = json.loads(item['skipped'])
        return result

    def table_summary(self) -> Dict[str, Tuple[int, Optional[int], Optional[int]]]:
        """{table: (row count, first time, last time)} for the four dated tables."""
        queries = {
            'positions': 'SELECT COUNT(*), MIN(t_ms), MAX(t_ms) FROM positions',
            'path_points': 'SELECT COUNT(*), MIN(t_ms), MAX(t_ms) FROM path_points',
            'visits': 'SELECT COUNT(*), MIN(start_ms), MAX(end_ms) FROM visits',
            'activities': 'SELECT COUNT(*), MIN(start_ms), MAX(end_ms) FROM activities',
        }
        with self._db.get_connection() as conn:
            return {table: tuple(conn.execute(sql).fetchone())
                    for table, sql in queries.items()}

    def position_types(self) -> List[Tuple[str, int, int]]:
        """(record type, readings, readings with no accuracy figure)."""
        with self._db.get_connection() as conn:
            return conn.execute(
                'SELECT record_type, COUNT(*), SUM(accuracy_m IS NULL) '
                'FROM positions GROUP BY record_type ORDER BY record_type'
            ).fetchall()

    def point_buckets(self, table: str, bucket_ms: int) -> List[Tuple[int, int]]:
        """(bucket number, rows) where a bucket is `bucket_ms` of UTC time."""
        if table not in ('positions', 'path_points'):
            raise ValueError(table)
        with self._db.get_connection() as conn:
            return conn.execute(
                f'SELECT t_ms / ? AS bucket, COUNT(*) FROM {table} GROUP BY bucket',
                (bucket_ms,),
            ).fetchall()

    def visit_intervals(self) -> List[Tuple[int, int]]:
        with self._db.get_connection() as conn:
            return conn.execute('SELECT start_ms, end_ms FROM visits').fetchall()

    def activity_intervals(self) -> List[Tuple[int, int, str, Optional[float]]]:
        with self._db.get_connection() as conn:
            return conn.execute(
                'SELECT start_ms, end_ms, activity_type, distance_m FROM activities'
            ).fetchall()

    # ------------------------------------------------------------------
    # Reading for maps, GPX and distance
    # ------------------------------------------------------------------

    def iter_track(
        self,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
        max_accuracy_m: Optional[float] = None,
    ) -> Iterator[Location]:
        """One time-ordered track that never mixes two sources in one day.

        Location readings and route points can both cover the same trip.
        Joining them would zig-zag between the two and inflate distance, so
        for each UTC day only the denser of the two is used. Readings less
        accurate than `max_accuracy_m` are left out.
        """
        clauses, params = [], []
        if from_ms is not None:
            clauses.append('t_ms >= ?')
            params.append(from_ms)
        if to_ms is not None:
            clauses.append('t_ms < ?')
            params.append(to_ms)
        position_clauses, position_params = list(clauses), list(params)
        if max_accuracy_m is not None:
            position_clauses.append('(accuracy_m IS NULL OR accuracy_m <= ?)')
            position_params.append(max_accuracy_m)

        def where(parts: List[str]) -> str:
            return f" WHERE {' AND '.join(parts)}" if parts else ''

        conn_manager = self._db.get_connection()
        with conn_manager as conn:
            position_days = dict(conn.execute(
                f'SELECT t_ms / {DAY_MS}, COUNT(*) FROM positions'
                f'{where(position_clauses)} GROUP BY 1', position_params))
            path_days = dict(conn.execute(
                f'SELECT t_ms / {DAY_MS}, COUNT(*) FROM path_points'
                f'{where(clauses)} GROUP BY 1', params))

            positions = (
                row for row in conn.execute(
                    f'SELECT t_ms, latitude, longitude, accuracy_m FROM positions'
                    f'{where(position_clauses)} ORDER BY t_ms', position_params)
                if position_days.get(row[0] // DAY_MS, 0) >= path_days.get(row[0] // DAY_MS, 0)
            )
            paths = (
                row for row in conn.execute(
                    f'SELECT t_ms, latitude, longitude, NULL FROM path_points'
                    f'{where(clauses)} ORDER BY t_ms', params)
                if path_days.get(row[0] // DAY_MS, 0) > position_days.get(row[0] // DAY_MS, 0)
            )
            for row in heapq.merge(positions, paths, key=lambda row: row[0]):
                yield Location(*row)

    def data_version(self) -> Tuple[int, int]:
        """Changes whenever a file is imported, so cached results can be refreshed."""
        with self._db.get_connection() as conn:
            return tuple(conn.execute(
                'SELECT COUNT(*), COALESCE(MAX(id), 0) FROM source_files').fetchone())

    def readings_between(
        self, from_ms: int, to_ms: int, max_accuracy_m: Optional[float], limit: int
    ) -> List[Tuple[int, float, float, Optional[float]]]:
        """Individual readings (not route points) for drawing as dots."""
        sql = ('SELECT t_ms, latitude, longitude, accuracy_m FROM positions '
               'WHERE t_ms >= ? AND t_ms < ?')
        params: List[Any] = [from_ms, to_ms]
        if max_accuracy_m is not None:
            sql += ' AND (accuracy_m IS NULL OR accuracy_m <= ?)'
            params.append(max_accuracy_m)
        sql += ' ORDER BY t_ms LIMIT ?'
        params.append(limit)
        with self._db.get_connection() as conn:
            return conn.execute(sql, params).fetchall()

    def visits_between(self, from_ms: int, to_ms: int, limit: int) -> List[tuple]:
        with self._db.get_connection() as conn:
            return conn.execute(
                'SELECT start_ms, end_ms, latitude, longitude, name, address, semantic_type, '
                'probability FROM visits WHERE latitude IS NOT NULL '
                'AND end_ms >= ? AND start_ms < ? ORDER BY start_ms LIMIT ?',
                (from_ms, to_ms, limit),
            ).fetchall()

    def activities_between(self, from_ms: int, to_ms: int) -> List[tuple]:
        with self._db.get_connection() as conn:
            return conn.execute(
                'SELECT start_ms, end_ms, activity_type, distance_m, start_latitude, '
                'start_longitude, end_latitude, end_longitude FROM activities '
                'WHERE end_ms > ? AND start_ms < ? ORDER BY start_ms',
                (from_ms, to_ms),
            ).fetchall()

    def frequent_places(self) -> List[Tuple[Optional[str], float, float]]:
        with self._db.get_connection() as conn:
            return conn.execute(
                'SELECT label, latitude, longitude FROM frequent_places '
                'WHERE latitude IS NOT NULL AND label IS NOT NULL').fetchall()

    def heat_cells(
        self, from_ms: int, to_ms: int, decimals: int, max_accuracy_m: Optional[float]
    ) -> List[Tuple[float, float, int]]:
        """Readings and route points counted in small squares, done inside SQLite."""
        accuracy = ''
        params: List[Any] = [decimals, decimals, from_ms, to_ms]
        if max_accuracy_m is not None:
            accuracy = ' AND (accuracy_m IS NULL OR accuracy_m <= ?)'
            params.append(max_accuracy_m)
        params += [decimals, decimals, from_ms, to_ms]
        with self._db.get_connection() as conn:
            return conn.execute(
                'SELECT lat, lng, SUM(n) FROM ('
                '  SELECT ROUND(latitude, ?) AS lat, ROUND(longitude, ?) AS lng, COUNT(*) AS n'
                f'  FROM positions WHERE t_ms >= ? AND t_ms < ?{accuracy} GROUP BY 1, 2'
                '  UNION ALL'
                '  SELECT ROUND(latitude, ?), ROUND(longitude, ?), COUNT(*)'
                '  FROM path_points WHERE t_ms >= ? AND t_ms < ? GROUP BY 1, 2'
                ') GROUP BY lat, lng',
                params,
            ).fetchall()

    def visit_points(
        self, from_ms: Optional[int] = None, to_ms: Optional[int] = None
    ) -> List[Tuple[float, float]]:
        clauses, params = ['latitude IS NOT NULL'], []
        if from_ms is not None:
            clauses.append('end_ms >= ?')
            params.append(from_ms)
        if to_ms is not None:
            clauses.append('start_ms < ?')
            params.append(to_ms)
        with self._db.get_connection() as conn:
            return conn.execute(
                f"SELECT latitude, longitude FROM visits WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
