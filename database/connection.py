import sqlite3
from contextlib import contextmanager
from pathlib import Path


class DatabaseConnection:
    """Manages SQLite connections. Each context-managed block gets a
    dedicated connection that is committed on success and rolled back on error.

    With `read_only=True` the database cannot be changed through it, which is
    how the map viewer opens it.
    """

    def __init__(self, db_path: str, read_only: bool = False) -> None:
        self._db_path = db_path
        self._read_only = read_only

    @contextmanager
    def get_connection(self):
        if self._read_only:
            uri = Path(self._db_path).resolve().as_uri() + '?mode=ro'
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
