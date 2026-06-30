"""
config.py — shared application state.

All routers import from here so the FastAPI app, Jinja2 templates, limiter,
signer, Anthropic client, constants, and auth helpers are initialised exactly
once at process start.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
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


_signer = URLSafeTimedSerializer(SECRET_KEY)

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
COOKIE = "session"
SESSION_MAX_AGE = 86400 * 7  # 7 days


def _set_session(response, user: dict):
    token = _signer.dumps({
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
    })
    response.set_cookie(
        COOKIE, token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=SESSION_MAX_AGE,
    )


def _get_session(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        data = _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data


class _AuthRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


async def _auth_redirect_handler(request: Request, exc: _AuthRedirect):
    r = RedirectResponse(url=exc.url, status_code=303)
    if request.cookies.get(COOKIE):
        try:
            _signer.loads(request.cookies.get(COOKIE), max_age=SESSION_MAX_AGE)
        except BadSignature:
            r.delete_cookie(COOKIE)
    return r


def require_login(request: Request):
    user = _get_session(request)
    if not user:
        raise _AuthRedirect("/login")
    if user.get("role") == "admin":
        raise _AuthRedirect("/admin")
    # Fetch fresh row so profile fields (full_name, email, phone) are current
    fresh = adb.get_user(user["id"])
    return fresh if fresh else user


def require_admin(request: Request):
    user = _get_session(request)
    if not user or user.get("role") != "admin":
        raise _AuthRedirect("/admin-dashboard")
    # Always fetch fresh user row so profile fields (full_name, email, phone) are current
    fresh = adb.get_user(user["id"])
    return fresh if fresh else user


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
    base = {
        "records": db_quotes.get_recent(),
        "result": None,
        "error": None,
        "latest_template": saved[0] if saved else None,
        "saved_templates": saved,
        "current_user": user,
        "theme": _resolve_theme(user) if user else "dark",
    }
    base.update(extra)
    return base


def _login_ctx(error=None) -> dict:
    return {"error": error, "recaptcha_site_key": RECAPTCHA_SITE_KEY}


def _build_install_map(records: list) -> str:
    install_map: dict = {}
    for r in records:
        raw = (r.get("install_date") or "").strip()
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
            "id":       r.get("id") or "",
            "customer": r.get("customer_name") or "",
            "quote":    r.get("quote_number")  or "",
            "address":  r.get("delivery_address") or "",
        })
    return json.dumps(install_map)


def _admin_ctx(user: dict, **extra) -> dict:
    base = {
        "current_user": user,
        "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
        "theme": _resolve_theme(user),
    }
    base.update(extra)
    return base
