"""
config.py — shared application state.

All routers import from here so the FastAPI app, Jinja2 templates, limiter,
signer, Anthropic client, constants, and auth helpers are initialised exactly
once at process start.
"""
import logging
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

import db.checklist_repo as tdb
import db.quote_repo as db_quotes
import db.user_repo as adb

# ---------------------------------------------------------------------------
# Config — loaded from .env
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# File logging — captures app errors/warnings and unhandled exceptions
# (crashes, DB errors, etc.) to a plain file for tailing/grepping, in
# addition to whatever gunicorn/systemd already send to journald.
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_log_file_handler = RotatingFileHandler(
    os.path.join(_LOG_DIR, "app.log"),
    maxBytes=10 * 1024 * 1024,  # 10 MB per file
    backupCount=5,              # keep app.log.1 .. app.log.5 (50 MB total)
)
_log_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"
))
_log_file_handler.setLevel(logging.WARNING)
logging.getLogger().addHandler(_log_file_handler)
logging.getLogger().setLevel(logging.WARNING)

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL         = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
SECRET_KEY           = os.environ.get("SECRET_KEY")
RECAPTCHA_SITE_KEY   = os.environ.get("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET_KEY = os.environ.get("RECAPTCHA_SECRET_KEY", "")
COOKIE_SECURE        = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<random-string> to your .env file and restart."
    )


def _verify_recaptcha(token: str) -> bool:
    if not RECAPTCHA_SECRET_KEY or RECAPTCHA_SECRET_KEY == "YOUR_RECAPTCHA_SECRET_KEY_HERE":
        return True
    try:
        payload = urllib.parse.urlencode({
            "secret": RECAPTCHA_SECRET_KEY,
            "response": token,
        }).encode()
        req = urllib.request.Request(
            "https://www.google.com/recaptcha/api/siteverify",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        return bool(result.get("success"))
    except Exception:
        return False


_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def _get_claude():
    if _anthropic_client is None:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set. Add it to your .env file.")
    return _anthropic_client


MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB hard cap for all uploads

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
_POST_ONLY_PATHS = ("/run-checklist", "/user_upload", "/checklist-confirm")

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")

app = FastAPI(
    title="Solar Agreement Extractor",
    docs_url=None,
    redoc_url=None,
)
app.state.limiter = limiter


async def _on_rate_limit_exceeded(request: Request, exc: RateLimitExceeded):  # noqa: ARG001
    referer = request.headers.get("referer", "")
    origin = str(request.base_url).rstrip("/")
    dest = referer if referer.startswith(origin) else "/qc-check"
    return RedirectResponse(url=dest, status_code=303)


app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/; "
    "frame-src https://www.google.com/recaptcha/ https://recaptcha.google.com/recaptcha/; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)


_HSTS_ENABLED = os.environ.get("HSTS_ENABLED", "false").lower() == "true"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = _CSP
        if _HSTS_ENABLED:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


# ---------------------------------------------------------------------------
# User-side login IP allowlist — runtime on/off toggle (admin-controlled)
# ---------------------------------------------------------------------------
LOGIN_IP_ALLOWLIST_SETTING = "user_login_ip_restricted"
LOGIN_ALLOWED_IPS = {"103.233.116.210", "139.5.251.232"}


def is_login_ip_restricted() -> bool:
    return adb.get_setting(LOGIN_IP_ALLOWLIST_SETTING, "0") == "1"


def set_login_ip_restricted(enabled: bool):
    adb.set_setting(LOGIN_IP_ALLOWLIST_SETTING, "1" if enabled else "0")


class UserLoginIPRestrictionMiddleware(BaseHTTPMiddleware):
    """Blocks POST/GET /login from outside LOGIN_ALLOWED_IPS, but only when
    the admin has turned the restriction on (is_login_ip_restricted()).
    Off by default — matches the fact this used to be an nginx-only
    restriction the admin had to ask a developer to toggle; now it's a
    plain DB flag they can flip themselves from the admin panel.

    Only /login is restricted — /admin-dashboard is never touched here,
    same scope as the nginx rule this replaces.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/login" and is_login_ip_restricted():
            client_ip = get_remote_address(request)
            if client_ip not in LOGIN_ALLOWED_IPS:
                return PlainTextResponse("403 Forbidden", status_code=403)
        return await call_next(request)


_signer = URLSafeTimedSerializer(SECRET_KEY)

# ---------------------------------------------------------------------------
# Session helpers — two separate cookies so admin and user can coexist
# ---------------------------------------------------------------------------
COOKIE       = "session_user"   # used by user routes only
COOKIE_ADMIN = "session_admin"  # used by admin routes only
SESSION_MAX_AGE = 86400 * 7     # 7 days — absolute cap on a session's lifetime
INACTIVITY_TIMEOUT = 60 * 60 * 4  # 4 hours — session ends early if idle this long


def _make_token(user: dict) -> str:
    now = int(datetime.utcnow().timestamp())
    return _signer.dumps({
        "id":        user["id"],
        "username":  user["username"],
        "role":      user["role"],
        "issued_at": now,   # fixed at login — drives the 7-day absolute cap
        "activity":  now,   # refreshed on each request — drives the idle timeout
    })


def _decode_token(token: str):
    """Decode a session token, enforcing both the sliding inactivity timeout
    and the fixed absolute session lifetime. Returns None if either check
    fails or the signature itself is invalid/tampered.

    Note: max_age is intentionally omitted from _signer.loads() — itsdangerous
    checks max_age against the token's signing time, which would reset every
    time the middleware re-signs the payload to refresh "activity", silently
    extending the absolute cap forever. The 7-day cap is instead enforced
    manually below, against "issued_at", which is never refreshed.
    """
    try:
        payload = _signer.loads(token)
    except BadSignature:
        return None

    now = int(datetime.utcnow().timestamp())
    issued_at = payload.get("issued_at")
    if issued_at is not None and now - issued_at > SESSION_MAX_AGE:
        return None

    activity = payload.get("activity")
    if activity is not None and now - activity > INACTIVITY_TIMEOUT:
        return None

    return payload


def _set_session(response, user: dict):
    """Write the correct cookie based on role."""
    token  = _make_token(user)
    cookie = COOKIE_ADMIN if user["role"] == "admin" else COOKIE
    response.set_cookie(
        cookie, token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=SESSION_MAX_AGE,
    )


def _get_session(request: Request):
    """Read the user cookie (non-admin)."""
    token = request.cookies.get(COOKIE)
    return _decode_token(token) if token else None


def _get_admin_session(request: Request):
    """Read the admin cookie."""
    token = request.cookies.get(COOKIE_ADMIN)
    return _decode_token(token) if token else None


class InactivityTimeoutMiddleware(BaseHTTPMiddleware):
    """Slides the inactivity window forward on every request that carries a
    still-valid session cookie, by reissuing it with a fresh "activity"
    timestamp. The actual timeout/expiry check (both the 15-minute idle
    limit and the 7-day absolute cap) lives in _decode_token(), which every
    auth guard already calls — if a cookie has gone idle too long or is past
    its absolute lifetime, _decode_token() returns None on the very next
    request and the existing require_login/require_admin guards redirect to
    login as usual. This middleware only needs to refresh cookies that are
    still valid right now."""

    async def dispatch(self, request: Request, call_next):
        now = int(datetime.utcnow().timestamp())
        refresh_cookies = {}

        for cookie_name in (COOKIE, COOKIE_ADMIN):
            token = request.cookies.get(cookie_name)
            if not token:
                continue
            payload = _decode_token(token)
            if not payload:
                continue
            payload["activity"] = now
            refresh_cookies[cookie_name] = _signer.dumps(payload)

        response = await call_next(request)

        # If the route handler itself already set a Set-Cookie header for
        # one of these cookies (e.g. logout's delete_cookie(), or
        # _set_session() issuing a fresh token on login), don't overwrite
        # it with our stale pre-request refresh — otherwise a just-deleted
        # cookie gets silently re-added by this middleware, so logout
        # never actually ends the session.
        already_set = {
            existing.split("=", 1)[0]
            for existing in response.headers.getlist("set-cookie")
        }
        for cookie_name, new_token in refresh_cookies.items():
            if cookie_name in already_set:
                continue
            response.set_cookie(
                cookie_name, new_token,
                httponly=True,
                samesite="lax",
                secure=COOKIE_SECURE,
                max_age=SESSION_MAX_AGE,
            )

        return response


class _AuthRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


async def _auth_redirect_handler(request: Request, exc: _AuthRedirect):
    return RedirectResponse(url=exc.url, status_code=303)


_crash_logger = logging.getLogger("adsagent.crash")


async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler — logs the full traceback for any exception that
    escapes a route (a real "app crash": unexpected code path, DB error,
    etc.) to logs/app.log, then returns a plain 500 same as FastAPI's
    default would, so behavior for the client is unchanged."""
    _crash_logger.error(
        "Unhandled exception on %s %s", request.method, request.url.path,
        exc_info=exc,
    )
    return PlainTextResponse("Internal Server Error", status_code=500)


def require_login(request: Request):
    """Guard for user-only routes — reads session_user cookie only."""
    user = _get_session(request)
    if not user or user.get("role") != "user":
        raise _AuthRedirect("/login")
    fresh = adb.get_user(user["id"])
    return fresh if fresh else user


def require_admin(request: Request):
    """Guard for admin-only routes — reads session_admin cookie only."""
    user = _get_admin_session(request)
    if not user or user.get("role") != "admin":
        raise _AuthRedirect("/admin-dashboard")
    fresh = adb.get_user(user["id"])
    return fresh if fresh else user


def require_superadmin(request: Request):
    """Guard for the handful of admin-on-admin actions (changing another
    admin's password or role) that only a super admin may perform — a
    regular admin can still manage plain users freely, this only narrows
    who can act on OTHER admin accounts."""
    user = require_admin(request)
    if not user.get("is_super_admin"):
        raise HTTPException(403, "Only a super admin can do this.")
    return user


def require_qc_access(request: Request):
    """Guard for the QC flow (upload/verify-email/checklist/confirm routes),
    letting both a logged-in user AND a logged-in admin through — same
    checks, same route bodies, same save/history behavior either way.

    Does NOT require email_verified for either role. Regular user accounts
    are created by an admin (not self-registered), so there is no signup
    step to confirm — and email-OTP delivery to some domains has proven
    unreliable, so gating the QC flow on it was a real lockout risk.
    """
    user = _get_session(request)
    if user and user.get("role") == "user":
        fresh = adb.get_user(user["id"])
        return fresh if fresh else user

    admin_user = _get_admin_session(request)
    if admin_user and admin_user.get("role") == "admin":
        fresh = adb.get_user(admin_user["id"])
        return fresh if fresh else admin_user

    raise _AuthRedirect("/login")


_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="#e8590c" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
    'M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/></svg>'
)

# ---------------------------------------------------------------------------
# Context helpers used by multiple routers
# ---------------------------------------------------------------------------

def _resolve_theme(user: dict) -> str:
    personal = adb.get_user_theme(user["id"])
    if personal in ("dark", "light"):
        return personal
    return adb.get_setting("user_panel_theme", "dark")


def _index_context(user=None, **extra):
    saved = tdb.list_templates()
    # If this user has an already-computed checklist result sitting unclaimed
    # (e.g. a session timeout bounced them off the results page before they
    # saw it), surface it instead of letting them re-upload and re-pay for
    # AI analysis that already ran successfully.
    resume_token = tdb.get_latest_pending_result_token(user["id"]) if user else None
    base = {
        "records": db_quotes.get_recent(),
        "result": None,
        "error": None,
        "latest_template": saved[0] if saved else None,
        "saved_templates": saved,
        "current_user": user,
        "theme": _resolve_theme(user) if user else "dark",
        "today": datetime.now().strftime("%Y-%m-%d"),
        "resume_token": resume_token,
    }
    base.update(extra)
    return base


def _login_ctx(error=None) -> dict:
    return {"error": error, "recaptcha_site_key": RECAPTCHA_SITE_KEY}


def _build_install_map(records: list) -> dict:
    # Only show quotes that have at least one confirmed QC version — a raw
    # PDF upload alone (no confirmed checklist yet) shouldn't clutter the
    # calendar or create duplicate-looking entries for the same customer.
    confirmed_ids = db_quotes.get_confirmed_quote_ids()
    install_map: dict = {}
    for r in records:
        if r.get("id") not in confirmed_ids:
            continue
        preferred = (r.get("preferred_install_date") or "").strip()
        raw = preferred or (r.get("install_date") or "").strip()
        if not raw:
            continue
        dt = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            continue
        key = dt.strftime("%Y-%m-%d")
        install_map.setdefault(key, []).append({
            "id":         r.get("id") or "",
            "customer":   r.get("customer_name") or "",
            "quote":      r.get("quote_number")  or "",
            "address":    r.get("delivery_address") or "",
            "preferred":  bool(preferred),
        })
    # Return the dict itself — the template encodes it with |tojson, which
    # (unlike json.dumps + |safe) escapes </script>, <, >, & so a malicious
    # customer/address value from an uploaded PDF can't break out of the
    # inline <script> and inject markup (stored-XSS hardening).
    return install_map


def _admin_ctx(user: dict, **extra) -> dict:
    base = {
        "current_user": user,
        "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
        "theme": _resolve_theme(user),
    }
    base.update(extra)
    return base
