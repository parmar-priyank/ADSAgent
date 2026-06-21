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
    conn.execute("PRAGMA busy_timeout = 5000")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    _configure(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
