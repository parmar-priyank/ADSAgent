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
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db.user_repo as adb
from config import (
    RECAPTCHA_SITE_KEY,
    _NO_CACHE,
    _POST_ONLY_PATHS,
    _get_session,
    _login_ctx,
    _resolve_theme,
    _set_session,
    _verify_recaptcha,
    limiter,
    require_login,
    templates,
    COOKIE,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_user_page(request: Request):
    """Public user login page."""
    user = _get_session(request)
    if user and user.get("role") == "user":
        return RedirectResponse(url="/user_home", status_code=302)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    response = templates.TemplateResponse(request, "login_user.html", _login_ctx())
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
        resp = templates.TemplateResponse(
            request, "login_user.html",
            _login_ctx("Invalid username or password."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    response = RedirectResponse(url="/user_home", status_code=303)
    _set_session(response, user)
    return response


@router.get("/admin-dashboard", response_class=HTMLResponse)
def login_admin_page(request: Request):
    """Secret admin login — URL not publicly linked anywhere."""
    user = _get_session(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    response = templates.TemplateResponse(request, "login_admin.html", _login_ctx())
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
        resp = templates.TemplateResponse(
            request, "login_admin.html",
            _login_ctx("Invalid credentials."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    response = RedirectResponse(url="/admin", status_code=303)
    _set_session(response, user)
    return response


@router.get("/logout")
def logout(request: Request):
    user = _get_session(request)
    dest = "/admin-dashboard" if (user and user.get("role") == "admin") else "/login"
    response = RedirectResponse(url=dest, status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.post("/toggle-theme")
async def toggle_theme(request: Request, user=Depends(require_login)):
    current = _resolve_theme(user)
    adb.set_user_theme(user["id"], "light" if current == "dark" else "dark")
    form = await request.form()
    next_url = form.get("next", "")
    origin = str(request.base_url).rstrip("/")
    if next_url and next_url.startswith(origin) and not any(next_url.startswith(origin + p) for p in _POST_ONLY_PATHS):
        dest = next_url
    else:
        referer = request.headers.get("referer", "")
        dest = referer if (referer.startswith(origin) and not any(referer.startswith(origin + p) for p in _POST_ONLY_PATHS)) else "/user_home"
    return RedirectResponse(url=dest, status_code=303)
