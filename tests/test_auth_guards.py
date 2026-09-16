"""
Auth guard behaviour — config.require_login / require_admin /
require_superadmin / require_qc_access.

These encode the access rules as they actually are today (verified against
config.py), so a future refactor that silently widens access fails here.
"""
import pytest
from fastapi import HTTPException


def _request_with(cookies: dict):
    """A minimal object with the only attribute the guards read: .cookies."""
    class _Req:
        def __init__(self, c):
            self.cookies = c
    return _Req(cookies)


# --------------------------------------------------------------------------
# require_login — user cookie only
# --------------------------------------------------------------------------

def test_require_login_rejects_anonymous(app_module):
    import config
    with pytest.raises(config._AuthRedirect) as e:
        config.require_login(_request_with({}))
    assert e.value.url == "/login"


def test_require_login_accepts_a_user(app_module, make_user, session_cookie):
    import config
    user = make_user(role="user")
    out = config.require_login(_request_with(session_cookie(user)))
    assert out["id"] == user["id"]


def test_require_login_rejects_an_admin_cookie(app_module, make_user, session_cookie):
    """An admin session must not satisfy a user-only route: require_login
    reads the session_user cookie only."""
    import config
    admin = make_user(role="admin")
    with pytest.raises(config._AuthRedirect):
        config.require_login(_request_with(session_cookie(admin)))


def test_require_login_rejects_a_deactivated_user(app_module, make_user, session_cookie):
    """is_active=0 must end the session immediately, not at expiry."""
    import config
    user = make_user(role="user")
    cookies = session_cookie(user)

    import db.user_repo as adb
    from db.connection import get_db
    with get_db() as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))

    with pytest.raises(config._AuthRedirect) as e:
        config.require_login(_request_with(cookies))
    assert "deactivated" in e.value.url


def test_require_login_rejects_a_tampered_cookie(app_module, make_user, session_cookie):
    """The session is signed; flipping bytes must invalidate it."""
    import config
    user = make_user(role="user")
    cookies = session_cookie(user)
    name = config.COOKIE
    cookies[name] = cookies[name][:-6] + "AAAAAA"
    with pytest.raises(config._AuthRedirect):
        config.require_login(_request_with(cookies))


def test_require_login_rejects_an_unsigned_cookie(app_module):
    import config
    with pytest.raises(config._AuthRedirect):
        config.require_login(_request_with({config.COOKIE: "not-a-signed-token"}))


# --------------------------------------------------------------------------
# require_admin / require_superadmin
# --------------------------------------------------------------------------

def test_require_admin_rejects_anonymous(app_module):
    import config
    with pytest.raises(config._AuthRedirect):
        config.require_admin(_request_with({}))


def test_require_admin_rejects_a_plain_user_cookie(app_module, make_user, session_cookie):
    """A user session must never satisfy an admin route."""
    import config
    user = make_user(role="user")
    with pytest.raises(config._AuthRedirect):
        config.require_admin(_request_with(session_cookie(user)))


def test_require_admin_accepts_an_admin(app_module, make_user, session_cookie):
    import config
    admin = make_user(role="admin")
    out = config.require_admin(_request_with(session_cookie(admin)))
    assert out["id"] == admin["id"]


def test_require_superadmin_rejects_a_normal_admin(app_module, make_user, session_cookie):
    """A regular admin may manage plain users but not act on other admins."""
    import config
    admin = make_user(role="admin", is_super_admin=0)
    with pytest.raises(HTTPException) as e:
        config.require_superadmin(_request_with(session_cookie(admin)))
    assert e.value.status_code == 403


def test_require_superadmin_accepts_a_super_admin(app_module, make_user, session_cookie):
    import config
    su = make_user(role="admin", is_super_admin=1)
    out = config.require_superadmin(_request_with(session_cookie(su)))
    assert out["id"] == su["id"]


# --------------------------------------------------------------------------
# require_qc_access — both roles allowed, admin cookie wins
# --------------------------------------------------------------------------

def test_require_qc_access_rejects_anonymous(app_module):
    import config
    with pytest.raises(config._AuthRedirect):
        config.require_qc_access(_request_with({}))


def test_require_qc_access_allows_a_user(app_module, make_user, session_cookie):
    import config
    user = make_user(role="user")
    assert config.require_qc_access(_request_with(session_cookie(user)))["id"] == user["id"]


def test_require_qc_access_allows_an_admin(app_module, make_user, session_cookie):
    import config
    admin = make_user(role="admin")
    assert config.require_qc_access(_request_with(session_cookie(admin)))["id"] == admin["id"]


def test_require_qc_access_prefers_the_admin_cookie(app_module, make_user, session_cookie):
    """Documented precedence: when BOTH cookies are present the admin session
    wins, because a stray user cookie previously outranked a real admin
    session and caused spurious "Access denied" on QC-version routes."""
    import config
    user = make_user(role="user")
    admin = make_user(role="admin")
    both = {**session_cookie(user), **session_cookie(admin)}
    assert config.require_qc_access(_request_with(both))["id"] == admin["id"]


def test_require_qc_access_rejects_a_deactivated_user(app_module, make_user, session_cookie):
    import config
    user = make_user(role="user")
    cookies = session_cookie(user)
    from db.connection import get_db
    with get_db() as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))
    with pytest.raises(config._AuthRedirect):
        config.require_qc_access(_request_with(cookies))


def test_require_qc_access_does_not_require_email_verification(app_module, make_user, session_cookie):
    """Deliberate behaviour per config.py: accounts are admin-created, not
    self-registered, and OTP delivery proved unreliable — so gating the QC
    flow on email_verified was a real lockout risk. Locked in so it is not
    "tightened" back by accident."""
    import config
    user = make_user(role="user", email_verified=0)
    assert config.require_qc_access(_request_with(session_cookie(user)))["id"] == user["id"]
