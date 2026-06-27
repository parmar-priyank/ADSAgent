import os
import sqlite3
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.environ.get("DB_PATH", "extractions.db")


def _configure(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")   # 30 s — gives other writers time to finish
    conn.execute("PRAGMA synchronous = NORMAL")   # safe with WAL; faster than FULL
    conn.execute("PRAGMA cache_size = -8000")     # 8 MB per connection cache
    conn.execute("PRAGMA wal_autocheckpoint = 100")  # checkpoint every 100 pages


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    _configure(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
