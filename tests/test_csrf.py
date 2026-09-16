"""
CSRF protection (double-submit cookie pattern).

Two things must both hold: a forged request is rejected, AND every legitimate
form and fetch call still works. The second is the one that breaks users, so
it gets the most coverage here.

These tests force CSRF_ENFORCE on regardless of the .env default, so they
test the strict path that production will eventually run.
"""
import re

import pytest


@pytest.fixture
def strict_client(app_module, monkeypatch):
    """A client with CSRF enforcement ON, whatever the env says."""
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", True)
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def logged_in_admin(strict_client, make_user, session_cookie):
    """An admin session, established WITHOUT going through POST /login.

    POST /login only authenticates when _verify_recaptcha() passes, and that
    auto-passes ONLY when RECAPTCHA_SECRET_KEY is unset. The production server
    has a real key, so a test login posts an empty CAPTCHA token, Google
    answers success=false, and no session is created — tests that relied on
    logging in failed there while passing locally.

    Setting the signed cookie directly is equivalent for these tests: they are
    about CSRF, not authentication, and the login flow itself is covered
    separately in test_routes.py.
    """
    user = make_user(role="admin")
    strict_client.cookies.update(session_cookie(user))
    strict_client.get("/admin")   # pick up a CSRF cookie
    return strict_client, user


# --------------------------------------------------------------------------
# Cookie issuing
# --------------------------------------------------------------------------

def test_a_get_issues_a_csrf_cookie(strict_client):
    strict_client.get("/login")
    assert strict_client.cookies.get("csrf_token")


def test_the_token_is_long_enough_to_be_unguessable(strict_client):
    strict_client.get("/login")
    assert len(strict_client.cookies.get("csrf_token")) >= 32


def test_two_visitors_get_different_tokens(app_module, monkeypatch):
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", True)
    a = TestClient(app_module.app, raise_server_exceptions=False)
    b = TestClient(app_module.app, raise_server_exceptions=False)
    a.get("/login")
    b.get("/login")
    assert a.cookies.get("csrf_token") != b.cookies.get("csrf_token")


def test_the_cookie_is_readable_by_javascript(strict_client):
    """Must NOT be HttpOnly — page JS has to read it to set the header."""
    r = strict_client.get("/login")
    setcookie = " ".join(r.headers.get_list("set-cookie"))
    csrf_bit = [p for p in setcookie.split(",") if "csrf_token" in p]
    assert csrf_bit, "no csrf_token Set-Cookie header"
    assert "httponly" not in csrf_bit[0].lower()


def test_the_session_cookie_is_still_httponly(app_module, make_user):
    """The CSRF cookie being readable must not have loosened the session
    cookie, which must stay HttpOnly.

    Calls _set_session() directly rather than going through POST /login.
    POST /login only authenticates if _verify_recaptcha() passes, and that
    returns True automatically ONLY when RECAPTCHA_SECRET_KEY is unset. The
    production server has a real key configured, so a test login posts an
    empty CAPTCHA token, Google answers success=false, the route re-renders
    the login form, and no session cookie is ever set — this test failed on
    the server while passing locally purely because of that .env difference.

    Asserting on _set_session() tests the actual cookie attributes, which is
    what this test is about, with no dependency on the login flow or on
    environment config. The login flow itself is covered in test_routes.py.
    """
    import config
    from fastapi.responses import RedirectResponse

    user = make_user(role="user")
    response = RedirectResponse(url="/user_home", status_code=303)
    config._set_session(response, user)

    headers = [v.decode() for k, v in response.raw_headers
               if k.lower() == b"set-cookie"]
    session_bits = [h for h in headers if h.startswith(f"{config.COOKIE}=")]
    assert session_bits, f"no {config.COOKIE} cookie was set"

    cookie = session_bits[0].lower()
    assert "httponly" in cookie, "the session cookie must stay HttpOnly"
    assert "samesite" in cookie, "the session cookie must keep SameSite"
    assert "max-age" in cookie, "the session cookie must keep its lifetime"


def test_the_csrf_and_session_cookies_have_opposite_httponly(app_module, make_user):
    """The two cookies must differ on exactly this point: the CSRF cookie is
    readable so page JS can echo it in a header, while the session cookie
    must never be readable by script."""
    import config
    from fastapi.responses import RedirectResponse

    user = make_user(role="user")
    response = RedirectResponse(url="/", status_code=303)
    config._set_session(response, user)
    config._set_csrf_cookie(response, "some-token-value")

    headers = [v.decode() for k, v in response.raw_headers
               if k.lower() == b"set-cookie"]
    session = next(h for h in headers if h.startswith(f"{config.COOKIE}="))
    csrf = next(h for h in headers if h.startswith(f"{config.CSRF_COOKIE}="))

    assert "httponly" in session.lower()
    assert "httponly" not in csrf.lower()


# --------------------------------------------------------------------------
# Rejection of forged requests
# --------------------------------------------------------------------------

def test_post_without_any_token_is_blocked(logged_in_admin):
    client, _ = logged_in_admin
    r = client.post("/admin/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_with_a_wrong_header_token_is_blocked(logged_in_admin):
    client, _ = logged_in_admin
    r = client.post("/admin/settings/theme",
                    data={"theme": "dark"},
                    headers={"X-CSRF-Token": "not-the-right-token"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_post_with_a_wrong_form_token_is_blocked(logged_in_admin):
    client, _ = logged_in_admin
    r = client.post("/admin/settings/theme",
                    data={"theme": "dark", "csrf_token": "wrong"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_post_with_a_wrong_query_token_is_blocked(logged_in_admin):
    client, _ = logged_in_admin
    r = client.post("/admin/settings/theme?csrf=wrong",
                    data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code == 403


def test_a_token_from_another_visitor_is_rejected(app_module, monkeypatch, make_user,
                                                  session_cookie):
    """The heart of the double-submit defence: a valid-looking token that does
    not match THIS request's cookie must fail.

    Session set directly rather than via POST /login — see the note on the
    logged_in_admin fixture about reCAPTCHA blocking test logins on the server.
    """
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", True)

    victim = make_user(role="admin")
    a = TestClient(app_module.app, raise_server_exceptions=False)
    a.cookies.update(session_cookie(victim))
    a.get("/admin")

    attacker = TestClient(app_module.app, raise_server_exceptions=False)
    attacker.get("/login")
    stolen = attacker.cookies.get("csrf_token")

    r = a.post("/admin/settings/theme",
               data={"theme": "dark"},
               headers={"X-CSRF-Token": stolen},
               follow_redirects=False)
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Legitimate requests must still work — the regression that matters
# --------------------------------------------------------------------------

def test_post_with_the_correct_header_is_accepted(logged_in_admin):
    client, _ = logged_in_admin
    tok = client.cookies.get("csrf_token")
    r = client.post("/admin/settings/theme",
                    data={"theme": "dark"},
                    headers={"X-CSRF-Token": tok},
                    follow_redirects=False)
    assert r.status_code != 403


def test_post_with_the_correct_form_field_is_accepted(logged_in_admin):
    client, _ = logged_in_admin
    tok = client.cookies.get("csrf_token")
    r = client.post("/admin/settings/theme",
                    data={"theme": "dark", "csrf_token": tok},
                    follow_redirects=False)
    assert r.status_code != 403


def test_post_with_the_correct_query_token_is_accepted(logged_in_admin):
    """How the large-upload forms (/run-checklist, /db/restore) pass the
    token, since they cannot set a header and must not be buffered."""
    client, _ = logged_in_admin
    tok = client.cookies.get("csrf_token")
    r = client.post(f"/admin/settings/theme?csrf={tok}",
                    data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code != 403


def test_the_form_body_still_reaches_the_route_handler(logged_in_admin):
    """THE critical regression test. Reading the body in middleware to find
    the token consumes it; without re-injection the handler receives an empty
    form and FastAPI returns 422 "Field required". Verified that this is
    exactly what happens without the fix, so a 422 here means the
    re-injection broke."""
    client, _ = logged_in_admin
    tok = client.cookies.get("csrf_token")
    r = client.post("/admin/settings/theme",
                    data={"theme": "light", "csrf_token": tok},
                    follow_redirects=False)
    assert r.status_code != 422, "the route handler did not receive the form body"
    assert r.status_code in (200, 302, 303)


def test_a_multipart_upload_still_reaches_the_handler(logged_in_admin):
    """Same re-injection concern, for multipart/form-data with a file part."""
    import io
    client, _ = logged_in_admin
    tok = client.cookies.get("csrf_token")
    r = client.post("/templates/upload",
                    data={"name": "CSRFTest", "csrf_token": tok},
                    files={"file": ("t.xlsx", io.BytesIO(b"not-a-real-xlsx"),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    follow_redirects=False)
    # Whatever the app decides about the bogus xlsx, it must have PARSED the
    # request rather than failing validation for a missing field.
    assert r.status_code != 403, "CSRF rejected a correctly-tokened upload"
    assert r.status_code != 422, "the handler did not receive the multipart body"


def test_login_stays_exempt(strict_client, make_user):
    """Login happens before any CSRF cookie exists, so it must be exempt or
    nobody could ever log in."""
    user = make_user(role="user", password="UserPass123!x")
    r = strict_client.post("/login",
                           data={"username": user["username"], "password": "UserPass123!x"},
                           follow_redirects=False)
    assert r.status_code != 403


def test_get_requests_are_never_blocked(strict_client):
    """Only state-changing methods are checked."""
    for path in ["/login", "/admin-dashboard", "/static/home.css"]:
        assert strict_client.get(path).status_code != 403


# --------------------------------------------------------------------------
# Rendered pages must carry a usable token
# --------------------------------------------------------------------------

def test_rendered_forms_contain_a_token_field(logged_in_admin):
    client, _ = logged_in_admin
    r = client.get("/admin/users")
    assert 'name="csrf_token"' in r.text


def test_every_post_form_in_the_source_carries_a_token():
    """Static check over the template source, because a rendered-page check
    only proves SOME form has a token — it passes even if one specific form
    lost its field. Verified: removing a single csrf_input() slipped past the
    rendered-page assertions, so this test exists to catch exactly that.

    A form is protected either by a hidden csrf_input() field, or (for the
    large-upload forms) by a csrf= token in its action URL.
    """
    import glob
    import os

    unprotected = []
    total = 0
    for path in sorted(glob.glob("templates/*.html")):
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r'<form[^>]*method="post"[^>]*>', src, re.I):
            total += 1
            tag = m.group(0)
            action_m = re.search(r'action="([^"]*)"', tag)
            action = action_m.group(1) if action_m else ""
            # The token field is inserted immediately after the opening tag.
            following = src[m.end():m.end() + 250]
            if "csrf=" in action or "csrf_input(request)" in following:
                continue
            unprotected.append(f"{os.path.basename(path)} -> {action or '(no action)'}")

    assert total > 0, "found no POST forms at all — has the template layout changed?"
    assert not unprotected, (
        f"{len(unprotected)} of {total} POST forms have no CSRF token:\n  "
        + "\n  ".join(unprotected)
    )


def test_every_js_post_sends_the_csrf_header():
    """Companion static check for fetch()/XHR calls, which cannot use a
    hidden field. Catches a new POST added later without a token."""
    import glob
    import os

    unprotected = []
    total = 0
    # Matches an explicit POST literal, and the one call that passes the
    # method in a variable (the template editor's form interceptor).
    pattern = re.compile(r"method:\s*(?:['\"]POST['\"]|method\b)")
    for path in sorted(glob.glob("templates/*.html")) + sorted(glob.glob("static/*.js")):
        src = open(path, encoding="utf-8").read()
        for m in pattern.finditer(src):
            total += 1
            # Search forward from the method to the end of that fetch options
            # object. Looking backwards too can pick up a NEIGHBOURING call's
            # header and mask a genuinely missing one.
            window = src[m.start(): m.start() + 450]
            if "X-CSRF-Token" not in window:
                line = src[:m.start()].count("\n") + 1
                unprotected.append(f"{os.path.basename(path)}:{line}")

    assert total > 0, "found no JS POST calls — has the JS layout changed?"
    assert not unprotected, (
        f"{len(unprotected)} of {total} JS POST calls send no CSRF header:\n  "
        + "\n  ".join(unprotected)
    )


def test_the_rendered_token_matches_the_cookie(logged_in_admin):
    """If these ever diverge, every form on the page breaks."""
    client, _ = logged_in_admin
    r = client.get("/admin/users")
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, "no csrf_token value rendered"
    assert m.group(1) == client.cookies.get("csrf_token")


def test_no_unresolved_template_tags_leak_into_pages(logged_in_admin):
    """A typo in the Jinja call would render the literal tag text and every
    token on the page would be wrong."""
    client, _ = logged_in_admin
    for path in ["/admin", "/admin/users", "/admin/records", "/Excel"]:
        body = client.get(path).text
        assert "{{ csrf" not in body, f"unresolved csrf tag in {path}"
        assert "csrf_token(request)" not in body, f"unrendered csrf call in {path}"


def test_log_only_mode_does_not_block(app_module, monkeypatch, make_user, session_cookie):
    """The deploy-safe default: a missing token is logged, not rejected.

    Session set directly rather than via POST /login — see the note on the
    logged_in_admin fixture about reCAPTCHA blocking test logins on the server.
    """
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", False)

    user = make_user(role="admin")
    c = TestClient(app_module.app, raise_server_exceptions=False)
    c.cookies.update(session_cookie(user))
    c.get("/admin")

    r = c.post("/admin/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code != 403, "log-only mode must not block"


# --------------------------------------------------------------------------
# Diagnostic logging
# --------------------------------------------------------------------------

def _capture_csrf_logs():
    """Attach a capturing handler to the csrf logger; returns (buffer, detach)."""
    import io
    import logging
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("adsagent.csrf")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)

    def detach():
        logger.removeHandler(handler)
        logger.setLevel(previous)

    return buf, detach


def test_a_stale_page_is_distinguishable_in_the_logs(app_module, monkeypatch,
                                                     make_user, session_cookie):
    """A page left open across the deploy posts without a token field, but its
    browser DOES still send the CSRF cookie. The log must record had_cookie
    so this harmless case can be told apart from a genuine failure without
    re-investigating every time — which is exactly what happened with the
    first /admin/settings/theme entries in production.
    """
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", False)

    user = make_user(role="admin")
    client = TestClient(app_module.app, raise_server_exceptions=False)
    client.cookies.update(session_cookie(user))
    client.get("/admin")            # browser now holds a CSRF cookie

    buf, detach = _capture_csrf_logs()
    try:
        client.post("/admin/settings/theme", data={"theme": "dark"},
                    headers={"referer": "https://example.test/admin/users"},
                    follow_redirects=False)
        out = buf.getvalue()
    finally:
        detach()

    assert "had_cookie=True" in out, out
    assert "no csrf_token field in form" in out, out
    assert "/admin/users" in out, "the referer must name the offending page"
    assert user["username"] in out, "the log must name the user"


def test_a_request_with_no_csrf_cookie_is_logged_differently(app_module, monkeypatch,
                                                             make_user, session_cookie):
    """The case that DOES warrant investigation: no CSRF cookie at all."""
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", False)

    user = make_user(role="admin")
    client = TestClient(app_module.app, raise_server_exceptions=False)
    client.cookies.update(session_cookie(user))   # session, but no CSRF cookie

    buf, detach = _capture_csrf_logs()
    try:
        client.post("/admin/settings/theme", data={"theme": "dark"},
                    follow_redirects=False)
        out = buf.getvalue()
    finally:
        detach()

    assert "had_cookie=False" in out, out


def test_the_who_helper_never_raises_on_a_bad_session(app_module):
    """Logging must never break the request it is describing."""
    import config

    class Req:
        cookies = {config.COOKIE: "not-a-valid-token"}

    assert config.CSRFMiddleware._who(Req()) == ""

    class Broken:
        @property
        def cookies(self):
            raise RuntimeError("boom")

    assert config.CSRFMiddleware._who(Broken()) == ""
