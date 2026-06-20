"""
auth_db.py — user accounts and session helpers.

Table: users
  id          INTEGER PK
  username    TEXT UNIQUE NOT NULL
  password    TEXT NOT NULL  (bcrypt hash)
  role        TEXT DEFAULT 'user'  ('admin' or 'user')
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
"""
import sqlite3

import bcrypt

from database import get_db


def init_auth_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                role       TEXT DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Create a default admin account if no users exist yet.
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            _create_user(conn, "admin", "admin123", "admin")


def _create_user(conn, username: str, password: str, role: str = "user"):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, hashed, role),
    )


def create_user(username: str, password: str, role: str = "user") -> bool:
    """Returns False if username already exists or inputs are invalid."""
    username = username.strip()
    if not username or len(password) < 8:
        return False
    try:
        with get_db() as conn:
            _create_user(conn, username, password, role)
        return True
    except sqlite3.IntegrityError:
        return False


def verify_user(username: str, password: str):
    """Return user row dict if credentials are valid, else None."""
    username = username.strip()
    if not username or not password:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row and bcrypt.checkpw(password.encode(), row["password"].encode()):
        return dict(row)
    return None


def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_user(user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def get_user(user_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


init_auth_db()
