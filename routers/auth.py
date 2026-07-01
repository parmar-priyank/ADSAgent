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
from utils.mailer import send_otp_email, SMTP_CONFIGURED
from fastapi.responses import JSONResponse

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
    # If a user (non-admin) session is active, show admin login anyway —
    # don't redirect them away, they may want to log in as admin separately.
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


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, error: str = None, success: str = None,
                         step: str = "request", via: str = "admin", username: str = ""):
    return templates.TemplateResponse(request, "forgot_password.html", {
        "error": error, "success": success, "step": step,
        "smtp_configured": bool(SMTP_CONFIGURED),
        "via": via,
        "saved_username": username,
    })


@router.post("/forgot-password/request")
@limiter.limit("5/minute")
def forgot_password_request(request: Request,
                            username: str = Form("", max_length=150),
                            via: str = Form("admin")):
    """Look up user, send OTP to verified email."""
    via = via if via in ("admin", "user") else "admin"
    uname = username.strip()
    if not uname:
        return RedirectResponse(url=f"/forgot-password?via={via}&error=Please+enter+your+username.", status_code=303)
    import urllib.parse
    uname_enc = urllib.parse.quote(uname)
    user = adb.get_user_by_username(uname)
    if not user:
        # Don't reveal whether username exists
        return RedirectResponse(
            url=f"/forgot-password?step=otp&via={via}&username={uname_enc}&success=If+that+username+exists%2C+an+OTP+was+sent.",
            status_code=303,
        )
    email = user.get("email")
    if not email:
        return RedirectResponse(
            url=f"/forgot-password?via={via}&username={uname_enc}&error=No+email+address+linked+to+this+account.+Contact+your+administrator.",
            status_code=303,
        )
    if not user.get("email_verified"):
        return RedirectResponse(
            url=f"/forgot-password?via={via}&username={uname_enc}&error=Email+not+verified.+Log+in+and+verify+your+email+first+via+Profile+Settings.",
            status_code=303,
        )
    if not bool(SMTP_CONFIGURED):
        return RedirectResponse(
            url=f"/forgot-password?via={via}&error=Email+service+not+configured+on+this+server.",
            status_code=303,
        )
    otp = adb.create_otp(user["id"], "reset")
    send_otp_email(email, otp, "reset", user["username"])
    return RedirectResponse(
        url=f"/forgot-password?step=otp&via={via}&username={uname_enc}&success=OTP+sent+to+your+registered+email.",
        status_code=303,
    )


@router.post("/forgot-password/verify")
@limiter.limit("10/minute")
def forgot_password_verify(request: Request,
                           username: str = Form("", max_length=150),
                           otp: str = Form("", max_length=6),
                           new_password: str = Form("", max_length=256),
                           via: str = Form("admin")):
    via = via if via in ("admin", "user") else "admin"
    import urllib.parse
    uname = username.strip()
    uname_enc = urllib.parse.quote(uname)
    user = adb.get_user_by_username(uname)
    if not user:
        return RedirectResponse(url=f"/forgot-password?step=otp&via={via}&username={uname_enc}&error=Invalid+OTP.", status_code=303)
    if not adb.verify_otp(user["id"], "reset", otp.strip()):
        return RedirectResponse(url=f"/forgot-password?step=otp&via={via}&username={uname_enc}&error=Invalid+or+expired+OTP.", status_code=303)
    strong = (len(new_password) >= 8 and
              any(c.isupper() for c in new_password) and
              any(c.islower() for c in new_password) and
              any(c.isdigit() for c in new_password) and
              any(not c.isalnum() for c in new_password))
    if not strong:
        return RedirectResponse(
            url=f"/forgot-password?step=otp&via={via}&error=Password+must+be+8%2B+chars+with+upper%2C+lower%2C+number+and+symbol.",
            status_code=303,
        )
    adb.change_password(user["id"], new_password)
    dest = "/admin-dashboard" if via == "admin" else "/login"
    return RedirectResponse(url=f"{dest}?success=Password+reset+successfully.", status_code=303)


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
    import urllib.parse
    err = adb.update_profile(user["id"], clean_name, clean_email, combined_phone[:30])
    if err:
        return RedirectResponse(url=f"/user_home?profile_error={urllib.parse.quote(err)}", status_code=303)
    ref = request.headers.get("referer", "/user_home")
    origin = str(request.base_url).rstrip("/")
    dest = ref if ref.startswith(origin) else "/user_home"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/user/email/send-otp")
def user_send_email_otp(request: Request, user=Depends(require_login)):
    email = user.get("email")
    if not email:
        return JSONResponse({"ok": False, "error": "No email address saved. Save your email first."})
    if not SMTP_CONFIGURED:
        return JSONResponse({"ok": False, "error": "SMTP is not configured on this server."})
    otp = adb.create_otp(user["id"], "verify")
    ok = send_otp_email(email, otp, "verify", user["username"])
    if not ok:
        return JSONResponse({"ok": False, "error": "Failed to send email. Check SMTP settings."})
    return JSONResponse({"ok": True})


@router.post("/user/email/verify-otp")
@limiter.limit("10/minute")
def user_verify_email_otp(request: Request,
                           otp: str = Form(..., max_length=6),
                           user=Depends(require_login)):
    if not otp.strip().isdigit() or len(otp.strip()) != 6:
        return RedirectResponse(url="/user_home?profile_error=Invalid+OTP+format.", status_code=303)
    if not adb.verify_otp(user["id"], "verify", otp.strip()):
        return RedirectResponse(url="/user_home?profile_error=Invalid+or+expired+OTP.", status_code=303)
    adb.set_email_verified(user["id"])
    return RedirectResponse(url="/user_home?profile_ok=Email+verified+successfully.", status_code=303)


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
        return RedirectResponse(url="/user_home?pwd_ok=Password+changed+successfully.", status_code=303)
    return RedirectResponse(url="/user_home?pwd_error=Password+must+be+at+least+8+characters.", status_code=303)


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
