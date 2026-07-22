"""
db/audit_repo.py — security audit trail for incident forensics.

Every security-relevant event (logins, failed logins, account changes,
record deletions, database access) is recorded with the actor, source IP,
and device (user agent), so that if a cyber incident is ever suspected
there is an evidence trail of who did what, from where, and when.

Append-only by design: the app exposes no route that edits or deletes
audit rows. Rows older than 365 days are pruned automatically on insert.
"""
import logging

from db.connection import get_db

_log = logging.getLogger("adsagent.audit")


def init_audit_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                event      TEXT NOT NULL,
                username   TEXT DEFAULT '',
                user_id    INTEGER,
                ip         TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                detail     TEXT DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event)")


def client_ip(request) -> str:
    """Best-effort real client IP.

    In production the app sits behind nginx on a unix socket, and nginx sets
    X-Real-IP from $remote_addr — that header can only come from our own
    nginx, so it's the trustworthy one. X-Forwarded-For's FIRST value can be
    spoofed by the client (nginx appends to whatever was sent), so it's used
    with that caveat only when X-Real-IP is absent (e.g. local dev), where we
    instead fall back to the socket peer address.
    """
    try:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            # Last entry is the address the nearest proxy actually saw.
            return xff.split(",")[-1].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def log_event(request, event: str, username: str = "", user_id=None, detail: str = ""):
    """Record one audit event. Never raises — a failure to write the audit
    row must not break the action being audited (it's logged instead)."""
    try:
        ip = client_ip(request) if request is not None else ""
        ua = ""
        if request is not None:
            ua = (request.headers.get("user-agent", "") or "")[:300]
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (event, username, user_id, ip, user_agent, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event[:60], (username or "")[:150], user_id, ip[:64], ua, (detail or "")[:500]),
            )
            # Retention: keep one year of evidence, prune older rows.
            conn.execute("DELETE FROM audit_log WHERE ts < datetime('now', '-365 days')")
    except Exception:
        _log.exception("Failed to write audit event %r", event)


def get_recent(limit: int = 200) -> list:
    limit = max(1, min(int(limit), 1000))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, ts, event, username, user_id, ip, user_agent, detail "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


init_audit_db()
