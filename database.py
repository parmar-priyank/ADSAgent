import os
import sqlite3
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.environ.get("DB_PATH", "extractions.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
