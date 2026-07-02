"""
db/user_repo.py — user accounts, authentication, and app-wide settings.
"""
import re
import secrets
import sqlite3
import time

import bcrypt

from db.connection import get_db

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-@]{1,64}$")


def init_auth_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                role       TEXT DEFAULT 'user',
                theme      TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            conn.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        for col in ("full_name TEXT DEFAULT NULL", "email TEXT DEFAULT NULL",
                    "phone TEXT DEFAULT NULL", "email_verified INTEGER DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS otp_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                purpose    TEXT NOT NULL,
                otp        TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                used       INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            initial_password = secrets.token_urlsafe(16)
            _create_user(conn, "admin", initial_password, "admin")
            print(
                f"\n{'='*60}\n"
                f"  First-run admin account created.\n"
                f"  Username : admin\n"
                f"  Password : {initial_password}\n"
                f"  Change this password immediately after first login.\n"
                f"{'='*60}\n"
            )


def _create_user(conn, username: str, password: str, role: str = "user"):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, hashed, role),
    )


def create_user(username: str, password: str, role: str = "user") -> bool:
    username = username.strip()
    if not username or not _USERNAME_RE.match(username):
        return False
    if len(password) < 8 or len(password) > 256:
        return False
    try:
        with get_db() as conn:
            _create_user(conn, username, password, role)
        return True
    except sqlite3.IntegrityError:
        return False


def _safe_user(row) -> dict:
    """Return user dict with the password hash removed."""
    d = dict(row)
    d.pop("password", None)
    return d


def verify_user(username: str, password: str):
    username = username.strip()
    if not username or not password:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row and bcrypt.checkpw(password.encode(), row["password"].encode()):
        return _safe_user(row)
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


def get_password_hash(user_id: int) -> str | None:
    """Return raw bcrypt hash — only used internally to verify current password."""
    with get_db() as conn:
        row = conn.execute("SELECT password FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["password"] if row else None


def change_password(user_id: int, new_password: str) -> bool:
    if len(new_password) < 8:
        return False
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with get_db() as conn:
        conn.execute("UPDATE users SET password = ? WHERE id = ?", (hashed, user_id))
    return True


def change_role(user_id: int, new_role: str) -> bool:
    if new_role not in {"user", "admin"}:
        return False
    with get_db() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    return True


def get_user(user_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _safe_user(row) if row else None


def get_user_by_email(email: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return _safe_user(row) if row else None


def set_email_verified(user_id: int, verified: bool = True):
    with get_db() as conn:
        conn.execute("UPDATE users SET email_verified = ? WHERE id = ?", (1 if verified else 0, user_id))


def create_otp(user_id: int, purpose: str, ttl_seconds: int = 300) -> str:
    """Generate a 6-digit OTP, store it, and return the code."""
    otp = f"{secrets.randbelow(1000000):06d}"
    expires_at = int(time.time()) + ttl_seconds
    with get_db() as conn:
        # Invalidate any previous unused OTPs for same user+purpose
        conn.execute(
            "UPDATE otp_tokens SET used=1 WHERE user_id=? AND purpose=? AND used=0",
            (user_id, purpose),
        )
        conn.execute(
            "INSERT INTO otp_tokens (user_id, purpose, otp, expires_at) VALUES (?,?,?,?)",
            (user_id, purpose, otp, expires_at),
        )
    return otp


def verify_otp(user_id: int, purpose: str, otp: str) -> bool:
    """Check OTP validity and mark it used. Returns True if valid."""
    now = int(time.time())
    with get_db() as conn:
        row = conn.execute(
            """SELECT id FROM otp_tokens
               WHERE user_id=? AND purpose=? AND otp=? AND used=0 AND expires_at>?
               ORDER BY id DESC LIMIT 1""",
            (user_id, purpose, otp, now),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE otp_tokens SET used=1 WHERE id=?", (row["id"],))
    return True


def update_profile(user_id: int, full_name: str, email: str, phone: str) -> str | None:
    """Update profile. Saves whichever of email/phone are not taken by another
    account, and returns a combined error string naming only the field(s) that
    collided — a bad phone must not block saving a valid email, or vice versa.
    """
    current = get_user(user_id)
    clean_email = email or None
    clean_phone = phone or None
    errors = []
    with get_db() as conn:
        if clean_email:
            row = conn.execute(
                "SELECT id FROM users WHERE email=? AND id!=?", (clean_email, user_id)
            ).fetchone()
            if row:
                errors.append("This email address is already used by another account.")
                clean_email = current.get("email") if current else None
        if clean_phone:
            row = conn.execute(
                "SELECT id FROM users WHERE phone=? AND id!=?", (clean_phone, user_id)
            ).fetchone()
            if row:
                errors.append("This phone number is already used by another account.")
                clean_phone = current.get("phone") if current else None
        email_changed = clean_email != (current.get("email") if current else None)
        conn.execute(
            "UPDATE users SET full_name=?, email=?, phone=?, email_verified=? WHERE id=?",
            (full_name or None, clean_email, clean_phone,
             0 if email_changed else (current.get("email_verified", 0) if current else 0),
             user_id),
        )
    return " ".join(errors) if errors else None


def save_email_only(user_id: int, email: str) -> str | None:
    """Save just the email address, leaving name/phone untouched. Returns an
    error string if the email is already used by another account, else None.
    """
    clean_email = email.strip()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email=? AND id!=?", (clean_email, user_id)
        ).fetchone()
        if row:
            return "This email address is already used by another account."
        conn.execute(
            "UPDATE users SET email=?, email_verified=0 WHERE id=?",
            (clean_email, user_id),
        )
    return None


def get_user_theme(user_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT theme FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["theme"] if row else None


def set_user_theme(user_id: int, theme: str):
    with get_db() as conn:
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))


def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


init_auth_db()
