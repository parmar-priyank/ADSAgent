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
def logged_in_admin(strict_client, make_user):
    user = make_user(role="admin", password="AdminPass123!x")
    strict_client.get("/login")
    strict_client.post("/login",
                       data={"username": user["username"], "password": "AdminPass123!x"},
                       follow_redirects=False)
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


def test_the_session_cookie_is_still_httponly(strict_client, make_user):
    """The CSRF cookie being readable must not have loosened the session
    cookie, which must stay HttpOnly."""
    user = make_user(role="user", password="UserPass123!x")
    r = strict_client.post("/login",
                           data={"username": user["username"], "password": "UserPass123!x"},
                           follow_redirects=False)
    session_bits = [h for h in r.headers.get_list("set-cookie") if "session_user" in h]
    assert session_bits
    assert "httponly" in session_bits[0].lower()


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


def test_a_token_from_another_visitor_is_rejected(app_module, monkeypatch, make_user):
    """The heart of the double-submit defence: a valid-looking token that does
    not match THIS request's cookie must fail."""
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", True)

    victim = make_user(role="admin", password="AdminPass123!x")
    a = TestClient(app_module.app, raise_server_exceptions=False)
    a.get("/login")
    a.post("/login", data={"username": victim["username"], "password": "AdminPass123!x"},
           follow_redirects=False)

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


def test_log_only_mode_does_not_block(app_module, monkeypatch, make_user):
    """The deploy-safe default: a missing token is logged, not rejected."""
    import config
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CSRF_ENFORCE", False)

    user = make_user(role="admin", password="AdminPass123!x")
    c = TestClient(app_module.app, raise_server_exceptions=False)
    c.get("/login")
    c.post("/login", data={"username": user["username"], "password": "AdminPass123!x"},
           follow_redirects=False)

    r = c.post("/admin/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code != 403, "log-only mode must not block"
