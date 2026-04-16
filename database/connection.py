import sqlite3
from contextlib import contextmanager


class DatabaseConnection:
    """Manages SQLite connections. Each context-managed block gets a
    dedicated connection that is committed on success and rolled back on error."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
