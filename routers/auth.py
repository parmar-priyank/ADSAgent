"""
routers/auth.py — login/logout routes for both users and admins.

Routes:
  GET  /login
  POST /login
  GET  /admin-dashboard
  POST /admin-dashboard
  GET  /logout
  POST /toggle-theme
"""
import re
import urllib.parse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db.user_repo as adb
import db.audit_repo as audit
from config import (
    _NO_CACHE,
    _POST_ONLY_PATHS,
    _get_session,
    _get_admin_session,
    _login_ctx,
    _resolve_theme,
    _set_session,
    _verify_recaptcha,
    limiter,
    require_login,
    require_qc_access,
    templates,
    COOKIE,
    COOKIE_ADMIN,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_user_page(request: Request, error: str = None):
    """Public user login page."""
    user = _get_session(request)
    if user and user.get("role") == "user":
        return RedirectResponse(url="/user_home", status_code=302)
    # Never auto-redirect to admin even if admin cookie exists
    response = templates.TemplateResponse(request, "login_user.html", _login_ctx(error))
    response.headers.update(_NO_CACHE)
    return response


@router.post("/login", response_class=HTMLResponse)
@limiter.limit("5/minute")
def login_user_post(
    request: Request,
    username: str = Form(..., max_length=150),
    password: str = Form(..., max_length=256),
    g_recaptcha_response: str = Form("", alias="g-recaptcha-response"),
):
    if not _verify_recaptcha(g_recaptcha_response):
        resp = templates.TemplateResponse(
            request, "login_user.html",
            _login_ctx("Please complete the CAPTCHA."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    user = adb.verify_user(username, password)
    if not user or user.get("role") == "admin":
        audit.log_event(request, "login_failed", username=username, detail="user panel")
        resp = templates.TemplateResponse(
            request, "login_user.html",
            _login_ctx("Invalid username or password."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    audit.log_event(request, "login_success", username=user["username"], user_id=user["id"], detail="user panel")
    response = RedirectResponse(url="/user_home", status_code=303)
    _set_session(response, user)
    return response


@router.get("/admin-dashboard", response_class=HTMLResponse)
def login_admin_page(request: Request, error: str = None):
    """Secret admin login — URL not publicly linked anywhere."""
    user = _get_admin_session(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    response = templates.TemplateResponse(request, "login_admin.html", _login_ctx(error))
    response.headers.update(_NO_CACHE)
    return response


@router.post("/admin-dashboard", response_class=HTMLResponse)
@limiter.limit("5/minute")
def login_admin_post(
    request: Request,
    username: str = Form(..., max_length=150),
    password: str = Form(..., max_length=256),
    g_recaptcha_response: str = Form("", alias="g-recaptcha-response"),
):
    if not _verify_recaptcha(g_recaptcha_response):
        resp = templates.TemplateResponse(
            request, "login_admin.html",
            _login_ctx("Please complete the CAPTCHA."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    user = adb.verify_user(username, password)
    if not user or user.get("role") != "admin":
        audit.log_event(request, "admin_login_failed", username=username, detail="admin panel")
        resp = templates.TemplateResponse(
            request, "login_admin.html",
            _login_ctx("Invalid credentials."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    audit.log_event(request, "admin_login_success", username=user["username"], user_id=user["id"], detail="admin panel")
    response = RedirectResponse(url="/admin", status_code=303)
    _set_session(response, user)
    return response


@router.get("/logout")
def logout(request: Request):
    # Determine which panel triggered logout based on which cookie is
    # present — check presence, not validity, so this still redirects
    # correctly even if the session had already gone idle/expired (e.g.
    # the 15-minute inactivity timeout) by the time Sign Out is clicked.
    dest = "/admin-dashboard" if request.cookies.get(COOKIE_ADMIN) else "/login"
    # Best-effort actor for the audit row — the session may already be expired.
    _who = _get_admin_session(request) or _get_session(request)
    audit.log_event(request, "logout",
                    username=(_who or {}).get("username", ""),
                    user_id=(_who or {}).get("id"))
    response = RedirectResponse(url=dest, status_code=303)
    response.delete_cookie(COOKIE)
    response.delete_cookie(COOKIE_ADMIN)
    return response


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


@router.post("/user/profile/update")
def user_update_profile(request: Request,
                        full_name: str = Form("", max_length=120),
                        email: str = Form("", max_length=254),
                        phone: str = Form("", max_length=30),
                        phone_country: str = Form("", max_length=10),
                        phone_number: str = Form("", max_length=20),
                        user=Depends(require_login)):
    clean_name  = full_name.strip()[:120]
    clean_email = email.strip()
    if clean_email and not _EMAIL_RE.match(clean_email):
        return RedirectResponse(url="/user_home?profile_error=Invalid+email+address.", status_code=303)
    combined_phone = phone.strip()
    if not combined_phone and phone_number.strip():
        digits = "".join(c for c in phone_number if c.isdigit())[:15]
        combined_phone = f"{phone_country.strip()} {digits}".strip() if phone_country.strip() else digits
    err = adb.update_profile(user["id"], clean_name, clean_email, combined_phone[:30])
    if err:
        return RedirectResponse(url=f"/user_home?profile_error={urllib.parse.quote(err)}", status_code=303)
    ref = request.headers.get("referer", "")
    # Compare only the path (not scheme/host) — request.base_url can resolve
    # with the wrong scheme behind this app's reverse-proxy setup (confirmed:
    # base_url reports http:// even though the site is https-only), which
    # would make a full-origin comparison against the browser's real https
    # referer always fail.
    ref_path = urllib.parse.urlparse(ref).path if ref else ""
    dest = ref if ref_path else "/user_home"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/user/change-password")
def user_change_password(request: Request,
                         old_password: str = Form(..., max_length=256),
                         new_password: str = Form(..., max_length=256),
                         user=Depends(require_login)):
    import bcrypt
    current_hash = adb.get_password_hash(user["id"])
    if not current_hash or not bcrypt.checkpw(old_password.encode(), current_hash.encode()):
        return RedirectResponse(url="/user_home?pwd_error=Current+password+is+incorrect.", status_code=303)
    ok = adb.change_password(user["id"], new_password)
    if ok:
        audit.log_event(request, "password_changed", username=user["username"], user_id=user["id"], detail="self-service")
        return RedirectResponse(url="/user_home?pwd_ok=Password+changed+successfully.", status_code=303)
    return RedirectResponse(
        url="/user_home?pwd_error=Password+must+be+8-256+characters+with+uppercase,+lowercase,+a+number,+and+a+symbol.",
        status_code=303,
    )


@router.post("/toggle-theme")
async def toggle_theme(request: Request, user=Depends(require_qc_access)):
    current = _resolve_theme(user)
    adb.set_user_theme(user["id"], "light" if current == "dark" else "dark")
    form = await request.form()
    next_url = form.get("next", "")

    # Compare only the URL PATH, not scheme/host — request.base_url can
    # resolve with the wrong scheme behind this app's reverse-proxy setup
    # (confirmed: base_url reports http:// even though the site is
    # https-only), which would make a full-origin comparison against the
    # browser's real https next_url/referer always fail.
    def _safe_path(url: str) -> str:
        if not url:
            return ""
        path = urllib.parse.urlparse(url).path
        if any(path.startswith(p) for p in _POST_ONLY_PATHS):
            return ""
        return path

    next_path = _safe_path(next_url)
    if next_path:
        dest = next_url
    else:
        referer = request.headers.get("referer", "")
        dest = referer if _safe_path(referer) else "/user_home"
    return RedirectResponse(url=dest, status_code=303)
