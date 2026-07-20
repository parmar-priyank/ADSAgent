"""
db/user_repo.py — user accounts, authentication, and app-wide settings.
"""
import re
import secrets
import sqlite3

import bcrypt

from db.connection import get_db

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-@]{1,64}$")


def _is_strong_password(password: str) -> bool:
    """Mirrors the client-side rules already shown in the Add User /
    Reset Password forms (length, upper, lower, digit, symbol) — enforced
    here too since the JS check alone can be bypassed by posting directly
    to the create/change-password routes."""
    if not (8 <= len(password) <= 256):
        return False
    return (
        re.search(r"[A-Z]", password) is not None
        and re.search(r"[a-z]", password) is not None
        and re.search(r"[0-9]", password) is not None
        and re.search(r"[^A-Za-z0-9]", password) is not None
    )


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
                    "phone TEXT DEFAULT NULL", "email_verified INTEGER DEFAULT 0",
                    "two_factor_enabled INTEGER DEFAULT 0",
                    "is_super_admin INTEGER DEFAULT 0",
                    "is_active INTEGER DEFAULT 1",
                    "can_pre_qc INTEGER DEFAULT 1",
                    "can_post_qc INTEGER DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
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
    if not _is_strong_password(password):
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
    if not row or not bcrypt.checkpw(password.encode(), row["password"].encode()):
        return None
    if not row["is_active"]:
        return None
    return _safe_user(row)


def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at, is_active, can_pre_qc, can_post_qc "
            "FROM users ORDER BY id ASC"
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
    if not _is_strong_password(new_password):
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
        if new_role != "admin":
            # A plain "user" can't be a super admin — clear the flag so it
            # doesn't silently reappear if this account is promoted again later.
            conn.execute("UPDATE users SET is_super_admin = 0 WHERE id = ?", (user_id,))
    return True


def set_super_admin(user_id: int, is_super: bool) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row["role"] != "admin":
            return False
        conn.execute("UPDATE users SET is_super_admin = ? WHERE id = ?", (1 if is_super else 0, user_id))
    return True


def set_active(user_id: int, active: bool):
    with get_db() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))


def set_qc_access(user_id: int, can_pre_qc: bool, can_post_qc: bool):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET can_pre_qc = ?, can_post_qc = ? WHERE id = ?",
            (1 if can_pre_qc else 0, 1 if can_post_qc else 0, user_id),
        )


def get_user(user_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _safe_user(row) if row else None


def get_user_by_email(email: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return _safe_user(row) if row else None


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
