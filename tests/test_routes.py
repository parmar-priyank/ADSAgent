"""
Route-level smoke and access-control tests, exercised over HTTP through
TestClient so they cover the real dependency wiring, not just the guard
functions in isolation.
"""
import pytest


# --------------------------------------------------------------------------
# Public routes must serve
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/login", "/admin-dashboard"])
def test_public_pages_render(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200, r.text[:300]


def test_static_files_are_served(client):
    assert client.get("/static/home.css").status_code == 200


def test_unknown_path_is_a_404_not_a_crash(client):
    assert client.get("/no-such-page-exists").status_code == 404


# --------------------------------------------------------------------------
# Protected routes must not serve content to anonymous visitors
# --------------------------------------------------------------------------

_PROTECTED_GET = [
    "/user_home",
    "/user/history",
    "/admin",
    "/admin/users",
    "/admin/records",
    "/admin/logs",
    "/Excel",
    "/post-qc",
]


@pytest.mark.parametrize("path", _PROTECTED_GET)
def test_protected_pages_deny_anonymous(client, path):
    """Must redirect to a login page (303/302/307) or refuse (401/403/404) —
    never return a rendered 200 page to someone with no session."""
    r = client.get(path, follow_redirects=False)
    assert r.status_code != 200, f"{path} served content to an anonymous visitor"
    assert r.status_code in (302, 303, 307, 308, 401, 403, 404), (
        f"{path} returned an unexpected {r.status_code}"
    )


@pytest.mark.parametrize("path", _PROTECTED_GET)
def test_protected_pages_do_not_500_for_anonymous(client, path):
    """A missing session is a normal condition, not an error."""
    r = client.get(path, follow_redirects=False)
    assert r.status_code < 500, f"{path} crashed for an anonymous visitor"


def test_a_plain_user_cannot_reach_the_admin_panel(client, make_user, session_cookie):
    user = make_user(role="user")
    client.cookies.update(session_cookie(user))
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code != 200


def test_an_admin_can_reach_the_admin_panel(client, make_user, session_cookie):
    admin = make_user(role="admin")
    client.cookies.update(session_cookie(admin))
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 200, r.text[:300]


def test_a_user_can_reach_their_own_history(client, make_user, session_cookie):
    user = make_user(role="user")
    client.cookies.update(session_cookie(user))
    r = client.get("/user/history", follow_redirects=False)
    assert r.status_code == 200, r.text[:300]


# --------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------

def test_security_headers_are_present(client):
    """SecurityHeadersMiddleware must apply to normal responses."""
    h = client.get("/login").headers
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert h.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in h
    assert "Referrer-Policy" in h


def test_csp_restricts_object_and_frame_ancestors(client):
    csp = client.get("/login").headers["Content-Security-Policy"]
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


# --------------------------------------------------------------------------
# Login form behaviour
# --------------------------------------------------------------------------

def test_login_rejects_a_wrong_password(client, make_user):
    user = make_user(role="user")
    r = client.post("/login",
                    data={"username": user["username"], "password": "definitely-wrong"},
                    follow_redirects=False)
    # Must not hand out a session cookie.
    assert "session_user" not in r.cookies
    assert r.status_code < 500


def test_login_rejects_an_unknown_username(client):
    r = client.post("/login",
                    data={"username": "no_such_user_here", "password": "whatever"},
                    follow_redirects=False)
    assert "session_user" not in r.cookies
    assert r.status_code < 500


def test_login_succeeds_with_correct_credentials(client, make_user, monkeypatch):
    """The positive case: a correct password issues a session cookie.

    reCAPTCHA is stubbed to pass, because _verify_recaptcha() only auto-passes
    when RECAPTCHA_SECRET_KEY is unset. On a machine with a real key
    configured (as production has), an un-stubbed test login posts an empty
    CAPTCHA token, Google answers success=false, and the route re-renders the
    form with no session — so without this stub the test would assert the
    wrong thing depending on which .env it ran against.
    """
    import config
    monkeypatch.setattr(config, "_verify_recaptcha", lambda token: True)
    import routers.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_verify_recaptcha", lambda token: True)

    user = make_user(role="user", password="CorrectHorse1!x")
    r = client.post("/login",
                    data={"username": user["username"], "password": "CorrectHorse1!x"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    session_cookies = [h for h in r.headers.get_list("set-cookie")
                       if h.startswith(f"{config.COOKIE}=")]
    assert session_cookies, "a correct login issued no session cookie"
    assert "httponly" in session_cookies[0].lower()


def test_verify_user_rejects_a_deactivated_account(app_module, make_user):
    """Checked at the repo level: a deactivated account must not authenticate
    even with the correct password."""
    import db.user_repo as adb
    from db.connection import get_db

    user = make_user(role="user")
    assert adb.verify_user(user["username"], user["_plain_password"]) is not None

    with get_db() as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))

    assert adb.verify_user(user["username"], user["_plain_password"]) is None


def test_verify_user_rejects_blank_credentials(app_module):
    import db.user_repo as adb
    assert adb.verify_user("", "") is None
    assert adb.verify_user("   ", "x") is None


def test_password_is_stored_hashed_not_plaintext(app_module, make_user):
    """bcrypt hash, never the plaintext."""
    from db.connection import get_db
    user = make_user(role="user", )
    with get_db() as conn:
        stored = conn.execute(
            "SELECT password FROM users WHERE id = ?", (user["id"],)
        ).fetchone()["password"]
    assert stored != user["_plain_password"]
    assert stored.startswith("$2")          # bcrypt prefix
    assert user["_plain_password"] not in stored
