import sqlite3
from typing import List

from database.connection import DatabaseConnection
from models.location import Location


class LocationRepository:
    """Handles all persistence operations for Location records."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def create_table(self) -> None:
        with self._db.get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS timeline (
                    timestamp    TEXT PRIMARY KEY,
                    latitude     REAL NOT NULL,
                    longitude    REAL NOT NULL,
                    semantic_type TEXT NOT NULL DEFAULT ''
                )
            ''')

    def insert_many(self, locations: List[Location]) -> int:
        """Insert locations, skipping duplicates. Returns the count inserted."""
        inserted = 0
        with self._db.get_connection() as conn:
            for loc in locations:
                try:
                    conn.execute(
                        '''INSERT INTO timeline
                               (timestamp, latitude, longitude, semantic_type)
                           VALUES (?, ?, ?, ?)''',
                        (loc.timestamp, loc.latitude, loc.longitude, loc.semantic_type),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue
        return inserted

    def get_all_ordered(self) -> List[Location]:
        """Return all locations ordered chronologically."""
        with self._db.get_connection() as conn:
            rows = conn.execute(
                'SELECT timestamp, latitude, longitude, semantic_type '
                'FROM timeline ORDER BY timestamp'
            ).fetchall()
        return [Location(*row) for row in rows]
