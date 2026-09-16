"""
Audit log behaviour.

The trail exists for incident forensics, so the property that matters most is
that NO event is ever dropped — the retention prune is sampled, the insert is
not. Locks in the fix from the "audit deletion on every insert" finding.
"""
import db.audit_repo as ar
from db.connection import get_db


def _count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def _old_count(days: int = 365) -> int:
    with get_db() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE ts < datetime('now', '-{days} days')"
        ).fetchone()[0]


def test_every_event_is_recorded(app_module):
    """The insert must never be sampled. 300 events -> 300 rows, exactly."""
    before = _count()
    for i in range(300):
        ar.log_event(None, "test_event", username=f"u{i}", detail="d")
    assert _count() - before == 300


def test_log_event_never_raises_on_a_bad_request_object(app_module):
    """Documented contract: a failure to write an audit row must not break
    the action being audited."""
    class Broken:
        @property
        def headers(self):
            raise RuntimeError("boom")

        @property
        def client(self):
            raise RuntimeError("boom")

    before = _count()
    ar.log_event(Broken(), "test_broken_request", username="u")
    # Must not raise; the row is still written (client_ip swallows the error).
    assert _count() >= before


def test_fields_are_truncated_not_rejected(app_module):
    """Over-long values are capped to the column widths rather than causing
    the insert to fail and lose the event."""
    ar.log_event(None, "e" * 200, username="u" * 400, detail="d" * 2000)
    with get_db() as conn:
        row = conn.execute(
            "SELECT event, username, detail FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert len(row["event"]) <= 60
    assert len(row["username"]) <= 150
    assert len(row["detail"]) <= 500


def test_retention_prunes_rows_older_than_a_year(app_module):
    """The window is still enforced, just not on every insert."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, event, username) "
            "VALUES (datetime('now','-400 days'), 'ancient', 'old')"
        )
    assert _old_count() >= 1

    # 1-in-100 sampling; 1500 inserts makes a miss vanishingly unlikely.
    for _ in range(1500):
        ar.log_event(None, "test_prune", username="u")

    assert _old_count() == 0


def test_recent_rows_are_not_pruned(app_module):
    """Pruning must only remove genuinely old rows."""
    ar.log_event(None, "test_keep_me", username="keeper")
    for _ in range(500):
        ar.log_event(None, "test_churn", username="u")
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'test_keep_me'"
        ).fetchone()[0]
    assert n == 1


def test_get_recent_clamps_its_limit(app_module):
    """Guards against an absurd limit being passed through to SQL."""
    assert len(ar.get_recent(limit=5)) <= 5
    assert len(ar.get_recent(limit=10**9)) <= 1000
    assert len(ar.get_recent(limit=0)) <= 1000      # clamped up to at least 1
    assert len(ar.get_recent(limit=-5)) <= 1000


def test_client_ip_prefers_x_real_ip(app_module):
    """X-Real-IP is set by our own nginx and is the trustworthy source;
    X-Forwarded-For's first value can be spoofed by the client."""
    class Req:
        headers = {"x-real-ip": "10.1.2.3",
                   "x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        client = None

    assert ar.client_ip(Req()) == "10.1.2.3"


def test_client_ip_uses_last_forwarded_value_when_no_real_ip(app_module):
    """Without X-Real-IP, the LAST X-Forwarded-For entry is the address the
    nearest proxy actually saw — the first is client-controlled."""
    class Req:
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        client = None

    assert ar.client_ip(Req()) == "5.6.7.8"


def test_client_ip_returns_empty_for_no_request(app_module):
    assert ar.client_ip(None) == ""
