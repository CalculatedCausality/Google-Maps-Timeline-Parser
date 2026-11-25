"""Your corrections to the timeline, kept in their own SQLite file.

The imported history is never changed. Each correction is stored here and
applied on top of it whenever a day is shown or exported, so a day can
always be reset to what Google recorded.
"""
import json
import sqlite3
import time
from math import cos, radians
from typing import Dict, List, Optional, Tuple

SCHEMA = '''
CREATE TABLE IF NOT EXISTS named_places (
    id         INTEGER PRIMARY KEY,
    latitude   REAL NOT NULL,
    longitude  REAL NOT NULL,
    radius_m   REAL NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_edits (
    day        TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    name       TEXT,
    mode       TEXT,
    hidden     INTEGER NOT NULL DEFAULT 0,
    geometry   TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, item_id)
);
'''

METRES_PER_DEG = 111_195.0


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class ItemEdit:
    def __init__(self, name: Optional[str], mode: Optional[str], hidden: bool,
                 geometry: Optional[List[Tuple[int, float, float]]]) -> None:
        self.name = name
        self.mode = mode
        self.hidden = hidden
        self.geometry = geometry


class EditsRepository:
    def __init__(self, path: str) -> None:
        self._path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    # ------------------------------------------------------------------
    # Per-item corrections
    # ------------------------------------------------------------------

    def for_day(self, day: str) -> Dict[str, ItemEdit]:
        with self._connect() as conn:
            rows = conn.execute('SELECT item_id, name, mode, hidden, geometry FROM item_edits '
                                'WHERE day = ?', (day,)).fetchall()
        return {
            item_id: ItemEdit(name, mode, bool(hidden),
                              [tuple(p) for p in json.loads(geometry)] if geometry else None)
            for item_id, name, mode, hidden, geometry in rows
        }

    def days_with_edits(self) -> List[str]:
        with self._connect() as conn:
            return [row[0] for row in conn.execute(
                'SELECT DISTINCT day FROM item_edits ORDER BY day')]

    def _update(self, day: str, item_id: str, column: str, value) -> None:
        with self._connect() as conn:
            conn.execute('INSERT OR IGNORE INTO item_edits (day, item_id, updated_at) '
                         'VALUES (?, ?, ?)', (day, item_id, _now()))
            conn.execute(f'UPDATE item_edits SET {column} = ?, updated_at = ? '
                         'WHERE day = ? AND item_id = ?', (value, _now(), day, item_id))
            # A row with nothing left in it is removed.
            conn.execute('DELETE FROM item_edits WHERE day = ? AND item_id = ? AND name IS NULL '
                         'AND mode IS NULL AND hidden = 0 AND geometry IS NULL', (day, item_id))

    def set_name(self, day: str, item_id: str, name: Optional[str]) -> None:
        self._update(day, item_id, 'name', name)

    def set_mode(self, day: str, item_id: str, mode: Optional[str]) -> None:
        self._update(day, item_id, 'mode', mode)

    def set_hidden(self, day: str, item_id: str, hidden: bool) -> None:
        self._update(day, item_id, 'hidden', 1 if hidden else 0)

    def set_geometry(self, day: str, item_id: str,
                     geometry: Optional[List[Tuple[int, float, float]]]) -> None:
        value = None if geometry is None else json.dumps(
            [[t, round(lat, 7), round(lng, 7)] for t, lat, lng in geometry],
            separators=(',', ':'))
        self._update(day, item_id, 'geometry', value)

    def reset_day(self, day: str) -> int:
        with self._connect() as conn:
            return conn.execute('DELETE FROM item_edits WHERE day = ?', (day,)).rowcount

    # ------------------------------------------------------------------
    # Named places: a name you give a spot, used on every visit there
    # ------------------------------------------------------------------

    def add_named_place(self, latitude: float, longitude: float, name: str,
                        radius_m: float = 75.0) -> None:
        with self._connect() as conn:
            conn.execute('INSERT INTO named_places (latitude, longitude, radius_m, name, '
                         'created_at) VALUES (?, ?, ?, ?, ?)',
                         (latitude, longitude, radius_m, name, _now()))

    def named_places(self) -> List[Tuple[float, float, float, str]]:
        with self._connect() as conn:
            return conn.execute('SELECT latitude, longitude, radius_m, name FROM named_places '
                                'ORDER BY id').fetchall()


def nearest_named(places: List[Tuple[float, float, float, str]],
                  latitude: float, longitude: float) -> Optional[str]:
    """The name of the closest named place whose circle contains this point.

    Later names win over earlier ones at the same distance.
    """
    best: Optional[Tuple[float, str]] = None
    metres_per_lng = METRES_PER_DEG * cos(radians(latitude))
    for lat, lng, radius, name in places:
        d = (((lat - latitude) * METRES_PER_DEG) ** 2
             + ((lng - longitude) * metres_per_lng) ** 2) ** 0.5
        if d <= radius and (best is None or d <= best[0]):
            best = (d, name)
    return best[1] if best else None
