import os
import io
import json
import base64
import mimetypes
import secrets
import sqlite3
import asyncio
import urllib.request
import urllib.parse
import zipfile as zipmod

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Form, Depends
from fastapi.responses import (
    HTMLResponse, Response, StreamingResponse, RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
import pdfplumber
import anthropic

import records as db      # quote/extraction storage
import checklists as tdb  # checklist template storage
import excel as cx         # parse / build checklist Excel files
import users as adb        # user accounts

# ---------------------------------------------------------------------------
# Config â€" loaded from .env
# ---------------------------------------------------------------------------
load_dotenv()

ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL          = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
SECRET_KEY            = os.environ.get("SECRET_KEY")
RECAPTCHA_SITE_KEY    = os.environ.get("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET_KEY  = os.environ.get("RECAPTCHA_SECRET_KEY", "")

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<random-string> to your .env file and restart."
    )


def _verify_recaptcha(token: str) -> bool:
    """Return True if the reCAPTCHA v2 token passes Google's verification."""
    if not RECAPTCHA_SECRET_KEY or RECAPTCHA_SECRET_KEY == "YOUR_RECAPTCHA_SECRET_KEY_HERE":
        return True  # skip check when keys are not configured yet
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

# Anthropic client initialised once at module load.
_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

def _get_claude():
    if _anthropic_client is None:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set. Add it to your .env file.")
    return _anthropic_client

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB hard cap for all uploads

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


# Rate limiter (keyed by client IP)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Solar Agreement Extractor",
    docs_url=None,    # disable /docs  (H5)
    redoc_url=None,   # disable /redoc (H5)
)
app.state.limiter = limiter

async def _on_rate_limit_exceeded(request: Request, exc: RateLimitExceeded):  # noqa: ARG001
    referer = request.headers.get("referer", "")
    origin = str(request.base_url).rstrip("/")
    dest = referer if referer.startswith(origin) else "/qc-check"
    return RedirectResponse(url=dest, status_code=303)

app.add_exception_handler(RateLimitExceeded, _on_rate_limit_exceeded)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")



# ---------------------------------------------------------------------------
# Security headers middleware  (L1)
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

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = _CSP
        return response

app.add_middleware(SecurityHeadersMiddleware)

_signer = URLSafeTimedSerializer(SECRET_KEY)

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
COOKIE = "session"

SESSION_MAX_AGE = 86400 * 7  # 7 days â€" must match _signer max_age in _get_session

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
        secure=False,       # set to True when serving over HTTPS in production
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
    """Raised by auth dependencies to trigger a redirect that also clears the stale cookie."""
    def __init__(self, url: str):
        self.url = url

async def _auth_redirect_handler(request: Request, exc: _AuthRedirect):
    r = RedirectResponse(url=exc.url, status_code=303)
    # Only clear the cookie if one exists but is invalid (stale server instance,
    # bad signature). If there is no cookie at all, deleting it is a no-op but
    # avoids accidentally logging out a valid session on an unrelated error.
    if request.cookies.get(COOKIE):
        try:
            _signer.loads(request.cookies.get(COOKIE), max_age=SESSION_MAX_AGE)
        except BadSignature:
            r.delete_cookie(COOKIE)
    return r

app.add_exception_handler(_AuthRedirect, _auth_redirect_handler)

def require_login(request: Request):
    user = _get_session(request)
    if not user:
        raise _AuthRedirect("/login")
    # Admins are allowed to use user pages (e.g. for testing).
    return user

def require_admin(request: Request):
    user = _get_session(request)
    if not user or user.get("role") != "admin":
        raise _AuthRedirect("/admin-dashboard")
    return user


# Serve a small inline SVG favicon so the browser stops requesting /favicon.ico
# (which would otherwise 404 on every page load).
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="#e8590c" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
    'M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/></svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=_FAVICON, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------
def extract_pdf_text(file_bytes: bytes) -> str:
    """
    Extract text from the PDF (skipping page 1, the marketing flyer).

    The agreement lays customer details and retailer details in two
    side-by-side columns. A plain extract_text() reads each row left-to-right
    and glues the two columns together (e.g. the customer's billing address
    ends up merged with the retailer's postal address). To avoid that, each
    page is split down the middle and the left (customer) and right (retailer)
    halves are extracted separately and clearly labelled.
    """
    left_parts, right_parts, full_parts = [], [], []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i == 0:
                continue  # ignore page one per requirement
            mid = page.width / 2
            left = page.crop((0, 0, mid, page.height)).extract_text() or ""
            right = page.crop((mid, 0, page.width, page.height)).extract_text() or ""
            left_parts.append(left)
            right_parts.append(right)
            full_parts.append(page.extract_text() or "")

    return (
        "===== LEFT COLUMN - CUSTOMER SIDE (use for the customer_* fields) =====\n"
        + "\n".join(left_parts)
        + "\n\n===== RIGHT COLUMN - RETAILER SIDE (use for the retailer_* fields; "
          "do NOT use for customer fields) =====\n"
        + "\n".join(right_parts)
        + "\n\n===== FULL TEXT (use ONLY for pricing/dates) =====\n"
        + "\n".join(full_parts)
    )


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------
EXTRACTION_SCHEMA = {
    "quote_number": "string",
    "quote_valid_until": "string (date the quote is valid until)",
    "customer_name": "string (the CUSTOMER, not the retailer)",
    "contact_person": "string (the customer's contact person only, never 'Nik' or the retailer's)",
    "billing_address": "string (single line, comma separated; the customer's billing address only)",
    "delivery_address": "string (single line, comma separated; the customer's delivery address only)",
    "email": "string (the customer's email, e.g. a personal address -- never sales@adssolar.com.au)",
    "phone": "string (the customer's phone/mobile -- never the retailer's 1300 number)",
    "retailer_name": "string (the retailer, e.g. 'ADS Pty Ltd t/as ADS Solar')",
    "retailer_contact_person": "string (the retailer's contact person, e.g. 'Nik')",
    "retailer_postal_address": "string (single line; the retailer's postal address, e.g. 'PO Box 6208, Norwest NSW 2153')",
    "retailer_street_address": "string (single line; the retailer's street address, e.g. '104, 29-31 Solent Circuit, Baulkham Hills NSW 2153')",
    "retailer_phone": "string (the retailer's phone, e.g. the 1300 number)",
    "retailer_email": "string (the retailer's email, e.g. 'sales@adssolar.com.au')",
    "roof_type": "string (the roof type, e.g. 'Tiled Roof', 'Colorbond', 'Metal' — look for the 'Roof Type' row in the system/pricing table and read its specification text)",
    "line_items": (
        "array of objects covering EVERY row of the System/pricing table in order, "
        "INCLUDING the equipment rows (Panels, Inverter, Inverter phase, Racking, "
        "Optimisers, Exp ctrl device, Battery, Stories, Extended Warranty, Options, "
        "Roof Type) AND the summary/financial rows (System Price, Less STC incentive, "
        "VIC Interest free Loan, VIC Rebate, ACT Govt Next Gen Rebate, Battery Rebate, "
        "Total Price). Each object: {\"item\": string (the row label, e.g. 'Panels', "
        "'System Price', 'Less STC incentive', 'VIC Rebate', 'Total Price'), "
        "\"quantity\": string (if the specification text starts with a pattern like "
        "'1.0 X', '26.0 X', etc., extract that number dropping the trailing '.0' — "
        "e.g. '26' for '26.0 X JKM510N...', '1' for '1.0 X [Solar Vic Rebate]...', "
        "'1' for '1.0 X [STC Battery Rebate]...'. This applies to ALL rows including "
        "rebate and financial rows — if the spec starts with '<number> X', capture it. "
        "Leave empty ONLY if the row has no specification text at all, e.g. "
        "'System Price', 'Total Price'), "
        "\"specification\": string (the full description/spec text WITHOUT the leading "
        "'<number> X ' quantity prefix; empty if the row has no spec text at all), "
        "\"price\": string (the dollar amount shown on that row, e.g. "
        "'$ 31,742.62', '$ 3,142.62', '$ -1,400.00', '$ 21,100.00'; empty if the row has "
        "no price)}. IMPORTANT: read the prices from the 'FULL TEXT' section, where each "
        "amount appears at the END of its row -- do not leave a price blank if the FULL "
        "TEXT shows a dollar amount for that row."
    ),
    "system_price": "string",
    "stc_incentive": "string (the 'Less STC incentive' amount)",
    "vic_rebate": "string",
    "battery_rebate": "string",
    "total_price": "string",
    "deposit": "string",
    "balance": "string",
    "payment_terms": "string",
    "install_date": "string (proposed install date)",
    "balance_due_date": "string",
    "notes": "string (any notes, e.g. gift voucher)",
}


def extract_with_claude(text: str) -> dict:
    client = _get_claude()

    system_prompt = (
        "You are a precise data extraction engine for solar agreements. "
        "The document has a CUSTOMER side and a RETAILER side. "
        "Extract ONLY the CUSTOMER's details for the customer fields. "
        "NEVER mix in retailer details: the retailer is 'ADS Pty Ltd t/as ADS Solar', "
        "its contact person is 'Nik', its postal address contains 'PO Box 6208 / Norwest', "
        "its street address is 'Solent Circuit, Baulkham Hills', its email is "
        "'sales@adssolar.com.au', and its phone is a 1300 number -- none of these belong "
        "in customer fields. The CUSTOMER section is under the 'LEFT COLUMN' heading "
        "and is the source for the customer_* fields. The RETAILER section is under the "
        "'RIGHT COLUMN' heading and IS the source for the retailer_* fields: fill "
        "retailer_name, retailer_contact_person, retailer_postal_address, "
        "retailer_street_address, retailer_phone, and retailer_email from that RIGHT "
        "section (e.g. retailer_contact_person='Nik', retailer_email='sales@adssolar.com.au'). "
        "Do NOT use the RIGHT section for any customer field, and do NOT use the LEFT "
        "section for any retailer field. For the line_items pricing table, take the PRICE for "
        "each row from the 'FULL TEXT' section (the column-split text clips the price "
        "column, so prices may be missing there) -- every row that shows a dollar amount "
        "in the FULL TEXT must carry that amount in its 'price'. "
        "Return ONLY a valid JSON object matching the schema, "
        "with no markdown, no code fences, and no commentary. If a field is missing, "
        "use an empty string."
    )

    user_prompt = (
        f"Schema (keys and expected meaning):\n{json.dumps(EXTRACTION_SCHEMA, indent=2)}\n\n"
        f'Agreement text:\n"""\n{text}\n"""'
    )

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = resp.content[0].text.strip()
    # Strip markdown code fences if Claude wrapped the JSON
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _resolve_theme(user: dict) -> str:
    """User's personal DB preference wins; falls back to admin-set global default."""
    personal = adb.get_user_theme(user["id"])
    if personal in ("dark", "light"):
        return personal
    return adb.get_setting("user_panel_theme", "dark")


def _index_context(user=None, **extra):
    """Common context for any render of user_home.html."""
    saved = tdb.list_templates()
    base = {
        "records": db.get_recent(),
        "result": None,
        "error": None,
        "latest_template": saved[0] if saved else None,
        "saved_templates": saved,
        "current_user": user,
        "theme": _resolve_theme(user) if user else "dark",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

def _login_ctx(error=None) -> dict:
    return {"error": error, "recaptcha_site_key": RECAPTCHA_SITE_KEY}


@app.get("/login", response_class=HTMLResponse)
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


@app.post("/login", response_class=HTMLResponse)
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


@app.get("/admin-dashboard", response_class=HTMLResponse)
def login_admin_page(request: Request):
    """Secret admin login â€" URL not publicly linked anywhere."""
    user = _get_session(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    response = templates.TemplateResponse(request, "login_admin.html", _login_ctx())
    response.headers.update(_NO_CACHE)
    return response


@app.post("/admin-dashboard", response_class=HTMLResponse)
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


@app.get("/logout")
def logout(request: Request):
    user = _get_session(request)
    dest = "/admin-dashboard" if (user and user.get("role") == "admin") else "/login"
    response = RedirectResponse(url=dest, status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.post("/toggle-theme")
async def toggle_theme(request: Request, user=Depends(require_login)):
    current = _resolve_theme(user)
    adb.set_user_theme(user["id"], "light" if current == "dark" else "dark")
    form = await request.form()
    next_url = form.get("next", "")
    origin = str(request.base_url).rstrip("/")
    _post_only = ("/run-checklist", "/user_upload", "/checklist-confirm")
    if next_url and next_url.startswith(origin) and not any(next_url.startswith(origin + p) for p in _post_only):
        dest = next_url
    else:
        referer = request.headers.get("referer", "")
        dest = referer if (referer.startswith(origin) and not any(referer.startswith(origin + p) for p in _post_only)) else "/user_home"
    return RedirectResponse(url=dest, status_code=303)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
def _build_install_map(records: list) -> str:
    from datetime import datetime
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
            "customer": r.get("customer_name") or "",
            "quote":    r.get("quote_number")  or "",
            "address":  r.get("delivery_address") or "",
        })
    return json.dumps(install_map)


def _admin_ctx(user: dict, **extra) -> dict:
    """Common context shared by all admin pages."""
    base = {
        "current_user": user,
        "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
        "theme": _resolve_theme(user),
    }
    base.update(extra)
    return base


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user=Depends(require_admin), cal: str = ""):
    records = db.get_recent()
    ctx = _admin_ctx(user,
        users_count=len(adb.list_users()),
        records_count=len(records),
        templates_count=len(tdb.list_templates()),
        install_map_json=_build_install_map(records),
        cal_jump=cal,  # e.g. "2026-08" — tells the calendar JS which month to open
    )
    response = templates.TemplateResponse(request, "admin_dashboard.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, user=Depends(require_admin), success: str = None):
    ctx = _admin_ctx(user,
        users=adb.list_users(),
        success="User created successfully." if success else None,
        error=None,
    )
    response = templates.TemplateResponse(request, "admin_users.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/admin/records", response_class=HTMLResponse)
def admin_records_page(request: Request, user=Depends(require_admin)):
    ctx = _admin_ctx(user, records=db.get_recent())
    response = templates.TemplateResponse(request, "admin_records.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/admin/templates", response_class=HTMLResponse)
def admin_templates_page(request: Request, user=Depends(require_admin)):
    ctx = _admin_ctx(user, templates=tdb.list_templates())
    response = templates.TemplateResponse(request, "admin_templates.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/admin/api-status")
def admin_api_status(user=Depends(require_admin)):
    """Ping Claude and return latency + status for the dashboard meter."""
    import time

    def _ping(client, model: str) -> dict:
        t0 = time.monotonic()
        try:
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "Hi"}],
            )
            ms = round((time.monotonic() - t0) * 1000)
            return {"status": "ok", "ms": ms}
        except Exception as e:
            ms = round((time.monotonic() - t0) * 1000)
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower():
                return {"status": "rate_limited", "ms": ms, "detail": "Rate limit hit"}
            if "401" in msg or "invalid_api_key" in msg.lower():
                return {"status": "auth_error", "ms": ms, "detail": "Invalid API key"}
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                return {"status": "timeout", "ms": ms, "detail": "Request timed out"}
            return {"status": "error", "ms": ms, "detail": msg[:120]}

    result = _ping(_anthropic_client, CLAUDE_MODEL) if _anthropic_client else {"status": "not_configured", "ms": 0}

    return {
        "text":   {"model": CLAUDE_MODEL, **result},
        "vision": {"model": CLAUDE_MODEL, **result},
    }


@app.get("/admin/database", response_class=HTMLResponse)
def admin_database_page(request: Request, user=Depends(require_admin)):
    ctx = _admin_ctx(user)
    response = templates.TemplateResponse(request, "admin_database.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/admin/users/create", response_class=HTMLResponse)
def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    user=Depends(require_admin),
):
    # H2 â€" allowlist role to prevent arbitrary role injection via crafted POST
    if role not in {"user", "admin"}:
        role = "user"
    ok = adb.create_user(username, password, role)
    if ok:
        return RedirectResponse(url="/admin/users?success=1", status_code=303)
    ctx = _admin_ctx(user,
        users=adb.list_users(),
        error=f"Username '{username}' already exists, is invalid, or password is too short (min. 8 characters).",
        success=None,
    )
    response = templates.TemplateResponse(request, "admin_users.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(user_id: int, request: Request, user=Depends(require_admin),
                      success: str = None, error: str = None):
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    qc_history = db.get_qc_history_by_user(user_id)
    ctx = _admin_ctx(user,
        target=target,
        success=success,
        error=error,
        qc_history=qc_history,
    )
    response = templates.TemplateResponse(request, "admin_user.html", ctx)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/admin/users/{user_id}/change-password")
def admin_change_password(user_id: int, request: Request,
                          new_password: str = Form(...),
                          user=Depends(require_admin)):
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    ok = adb.change_password(user_id, new_password)
    if ok:
        return RedirectResponse(url=f"/admin/users/{user_id}?success=Password+changed+successfully.", status_code=303)
    return RedirectResponse(url=f"/admin/users/{user_id}?error=Password+must+be+at+least+8+characters.", status_code=303)


@app.post("/admin/users/{user_id}/change-role")
def admin_change_role(user_id: int, request: Request,
                      new_role: str = Form(...),
                      user=Depends(require_admin)):
    if user_id == user["id"]:
        return RedirectResponse(url=f"/admin/users/{user_id}?error=Cannot+change+your+own+role.", status_code=303)
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    ok = adb.change_role(user_id, new_role)
    if ok:
        label = "Admin" if new_role == "admin" else "User"
        return RedirectResponse(url=f"/admin/users/{user_id}?success=Role+changed+to+{label}.", status_code=303)
    return RedirectResponse(url=f"/admin/users/{user_id}?error=Invalid+role.", status_code=303)


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request, user=Depends(require_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account.")
    adb.delete_user(user_id)
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/records/{record_id}/delete")
def admin_delete_record(record_id: int, request: Request, user=Depends(require_admin)):
    db.delete_quote(record_id)
    return RedirectResponse(url="/admin/records", status_code=303)


@app.post("/admin/settings/theme")
def admin_set_theme(request: Request, theme: str = Form(...), user=Depends(require_admin),
                    referer: str = None):
    if theme in ("dark", "light"):
        adb.set_setting("user_panel_theme", theme)
    ref = request.headers.get("referer", "")
    origin = str(request.base_url).rstrip("/")
    dest = ref if (ref.startswith(origin + "/admin")) else "/admin"
    return RedirectResponse(url=dest, status_code=303)


# ---------------------------------------------------------------------------
# Database backup / restore (admin only)
# ---------------------------------------------------------------------------
@app.get("/db/download")
def db_download(user=Depends(require_admin)):
    from database import DB_PATH
    import shutil, tempfile
    # Use SQLite backup API for a consistent snapshot (safe even while live)
    src = sqlite3.connect(DB_PATH)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dst = sqlite3.connect(tmp.name)
    src.backup(dst)
    dst.close()
    src.close()
    filename = os.path.basename(DB_PATH).replace(".db", "") + "_backup.db"
    return Response(
        content=open(tmp.name, "rb").read(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/db/restore")
async def db_restore(request: Request, file: UploadFile = File(...), user=Depends(require_admin)):
    from database import DB_PATH
    import shutil, tempfile
    data = await file.read()
    if not data.startswith(b"SQLite format 3"):
        raise HTTPException(400, "Invalid file — must be a SQLite database.")
    # Snapshot the live DB before overwriting so the admin can recover if the
    # uploaded file turns out to be corrupt despite passing the header check.
    backup_path = DB_PATH + ".pre_restore_backup"
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(data)
    tmp.close()
    shutil.move(tmp.name, DB_PATH)
    return RedirectResponse(url="/Excel?restored=1", status_code=303)


# ---------------------------------------------------------------------------
# App routes (require login)
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
@app.get("/home", response_class=HTMLResponse)
def root_redirect(request: Request):
    user = _get_session(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    return RedirectResponse(url="/user_home", status_code=302)


@app.get("/user_home", response_class=HTMLResponse)
def home(request: Request, user=Depends(require_login)):
    response = templates.TemplateResponse(request, "user_home.html", _index_context(user=user))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/qc-check", response_class=HTMLResponse)
def qc_check_page(request: Request, quote_id: str = "", user=Depends(require_login)):
    saved = tdb.list_templates()
    resp = templates.TemplateResponse(request, "user_qc.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "saved_templates": saved,
        "error": None,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@app.post("/run-checklist", response_class=HTMLResponse)
async def upload_zip(
    request: Request,
    zip_file: UploadFile = File(...),
    quote_id: str = Form(""),
    template_id: int = Form(...),
    user=Depends(require_login),
):
    # Load checklist items for the chosen template
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)

    # Extract ZIP into a flat dict: lowercase filename â†' bytes
    data = await zip_file.read()

    def _qc_error(error: str):
        saved = tdb.list_templates()
        resp = templates.TemplateResponse(request, "user_qc.html", {
            "current_user": user,
            "theme": _resolve_theme(user),
            "quote_id": quote_id,
            "saved_templates": saved,
            "error": error,
        })
        resp.headers.update(_NO_CACHE)
        return resp

    if len(data) > MAX_UPLOAD_BYTES:
        return _qc_error("ZIP file is too large. Maximum allowed size is 200 MB.")

    zip_files: dict[str, bytes] = {}
    try:
        with zipmod.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                # H3 â€" use os.path.basename to handle both / and \ separators safely
                basename = os.path.basename(name).strip()
                if basename:
                    zip_files[basename.lower()] = zf.read(name)
    except zipmod.BadZipFile:
        return _qc_error("Uploaded file is not a valid ZIP archive.")

    # Load the main reference PDF text from the uploaded agreement (Step 1).
    # This is injected into every checklist prompt so Claude can compare
    # values in ZIP documents against the original signed agreement.
    reference_pdf_text = ""
    if quote_id:
        try:
            ref_record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            ref_record = db.find_by_quote_number(quote_id)
        if ref_record:
            # Build structured summary from all extracted quote fields
            fields = [
                ("Quote Number",    ref_record.get("quote_number", "")),
                ("Customer Name",   ref_record.get("customer_name", "")),
                ("Install Date",    ref_record.get("install_date", "")),
                ("System Price",    ref_record.get("system_price", "")),
                ("STC Incentive",   ref_record.get("stc_incentive", "")),
                ("VIC Rebate",      ref_record.get("vic_rebate", "")),
                ("Battery Rebate",  ref_record.get("battery_rebate", "")),
                ("Total Price",     ref_record.get("total_price", "")),
                ("Deposit",         ref_record.get("deposit", "")),
                ("Balance",         ref_record.get("balance", "")),
                ("Payment Terms",   ref_record.get("payment_terms", "")),
                ("Billing Address", ref_record.get("billing_address", "")),
                ("Delivery Address",ref_record.get("delivery_address", "")),
                ("Roof Type",       ref_record.get("roof_type", "")),
                ("Email",           ref_record.get("email", "")),
                ("Phone",           ref_record.get("phone", "")),
                ("Notes",           ref_record.get("notes", "")),
            ]
            reference_pdf_text = "\n".join(
                f"{k}: {v}" for k, v in fields if v
            )
            # Also include line items (panels, inverter, battery, roof type etc.)
            line_items = ref_record.get("line_items") or []
            if line_items:
                reference_pdf_text += "\n\nSystem Components:\n" + "\n".join(
                    f"- {li.get('item','')}: {li.get('specification','')}"
                    for li in line_items if li.get("item")
                )

    _claude_client = _get_claude()
    QC_SYSTEM = (
        "You are a QC document checker. "
        "Determine if the requirement is met based on the provided content. "
        'Reply with JSON only: {"status": "Yes" or "No", "remark": "one sentence explanation"}'
    )

    def _claude_check(user_content) -> dict:
        resp = _claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=QC_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        try:
            r = json.loads(raw)
        except Exception:
            return {"status": "N/A", "remark": "AI returned an unreadable response."}
        return {"status": r.get("status", "N/A"), "remark": r.get("remark", "")}

    def _resolve_file(name: str):
        """Return (actual_zip_filename, bytes) for a reference name, or (name, None) if not found.

        Matching order:
        1. Exact name (case-insensitive)
        2. Exact name ignoring spaces
        3. Same stem, any extension (e.g. template says Deposit.jpg, ZIP has Deposit.png)
        4. Same stem ignoring spaces, any extension
        """
        key = name.lower()
        # 1. Exact match
        data = zip_files.get(key)
        if data is not None:
            return key, data

        key_nospace = key.replace(" ", "")
        stem = os.path.splitext(key)[0]
        stem_nospace = stem.replace(" ", "")

        best_name, best_data = None, None
        for zname, zbytes in zip_files.items():
            # 2. Exact match ignoring spaces
            if zname.replace(" ", "") == key_nospace:
                return zname, zbytes
            # 3. Same stem, any extension
            zstem = os.path.splitext(zname)[0]
            if zstem == stem:
                best_name, best_data = zname, zbytes
            # 4. Same stem ignoring spaces, any extension
            elif zstem.replace(" ", "") == stem_nospace and best_data is None:
                best_name, best_data = zname, zbytes

        if best_data is not None:
            return best_name, best_data
        return name, None

    def _analyse_item(item: dict) -> dict:
        ref = (item.get("reference") or "").strip()
        prompt_text = (item.get("prompt") or "").strip()

        if not ref:
            return {"status": "N/A", "remark": "No reference file specified."}
        if not prompt_text:
            return {"status": "N/A", "remark": "No prompt defined for this item."}

        # Support multiple files separated by "+" e.g. "Rates.pdf + DL.pdf"
        ref_names = [r.strip() for r in ref.split("+") if r.strip()]
        resolved = [_resolve_file(r) for r in ref_names]

        missing = [name for name, data in resolved if data is None]
        found = [(name, data) for name, data in resolved if data is not None]
        if not found:
            return {"status": "N/A", "remark": f"File not found in ZIP: {', '.join(missing)}"}
        # Work with whatever files were found; note any missing ones in context
        resolved = found
        missing_note = f" (Note: {', '.join(missing)} not found in ZIP, working with available files only.)" if missing else ""

        ref_section = (
            f"\n\n--- MAIN REFERENCE PDF (Signed Agreement) ---\n{reference_pdf_text}"
            if reference_pdf_text else ""
        )
        context = f"Checklist item: {item['text']}\nRequirement: {prompt_text}{ref_section}{missing_note}"

        try:
            # Detect MIME from actual ZIP filename; fall back to magic-byte sniffing
            def _mime_of(fname: str, fbytes: bytes) -> str:
                mime, _ = mimetypes.guess_type(fname)
                if mime:
                    return mime
                # sniff by magic bytes
                if fbytes[:4] == b"%PDF":
                    return "application/pdf"
                if fbytes[:8] in (b"\x89PNG\r\n\x1a\n",):
                    return "image/png"
                if fbytes[:3] in (b"\xff\xd8\xff",):
                    return "image/jpeg"
                if fbytes[:6] in (b"GIF87a", b"GIF89a"):
                    return "image/gif"
                if fbytes[:4] == b"RIFF" and fbytes[8:12] == b"WEBP":
                    return "image/webp"
                if fbytes[:2] == b"BM":
                    return "image/bmp"
                return "application/octet-stream"

            # Separate files by type using actual filename + magic bytes
            pdf_parts, image_parts, unsupported = [], [], []
            for fname, fdata in resolved:
                mime = _mime_of(fname, fdata)
                if mime == "application/pdf":
                    pdf_parts.append((fname, fdata, mime))
                elif mime.startswith("image/"):
                    image_parts.append((fname, fdata, mime))
                else:
                    unsupported.append((fname, fdata, mime))

            if unsupported:
                return {"status": "N/A", "remark": f"Unsupported file type: {', '.join(n for n, _, _ in unsupported)}"}

            # Always render PDFs as images — text extraction alone misses handwritten
            # signatures, stamps, checkbox ticks, and other visual elements.
            combined_pdf_text = ""
            pdf_page_images = []  # (fname, page_png_bytes)
            for fname, fdata, _ in pdf_parts:
                # Extract text as supplementary context (helps with typed content)
                try:
                    with pdfplumber.open(io.BytesIO(fdata)) as pdf:
                        pages_text = [p.extract_text() or "" for p in pdf.pages]
                    text = "\n".join(pages_text)
                    if text.strip():
                        combined_pdf_text += f"\n\n--- {fname} ---\n{text}"
                except Exception:
                    pass  # text extraction optional; vision path handles it

                # Render every page as PNG so Claude can see visual elements
                try:
                    import fitz  # PyMuPDF
                    doc = fitz.open(stream=fdata, filetype="pdf")
                    for page in doc:
                        pix = page.get_pixmap(dpi=150)
                        pdf_page_images.append((fname, pix.tobytes("png")))
                    doc.close()
                except Exception:
                    # PyMuPDF unavailable — fall back to text-only for this PDF
                    if not combined_pdf_text.strip():
                        return {"status": "N/A", "remark": f"Could not render '{fname}' as image. Install PyMuPDF (pip install pymupdf)."}

            # Build vision message: text context first, then all images
            user_content = [{"type": "text", "text": context}]
            if combined_pdf_text.strip():
                user_content.append({"type": "text", "text": f"Document text (for reference):{combined_pdf_text[:4000]}"})
            for fname, page_png in pdf_page_images:
                b64 = base64.standard_b64encode(page_png).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
            for fname, fdata, mime in image_parts:
                b64 = base64.standard_b64encode(fdata).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})

            # If nothing to send (no PDFs, no images), fall back to text-only
            if len(user_content) == 1:
                if combined_pdf_text.strip():
                    return _claude_check(f"{context}\n\nDocument content:{combined_pdf_text[:8000]}")
                return {"status": "N/A", "remark": "No readable content found in reference file(s)."}

            return _claude_check(user_content)

        except Exception as e:
            return {"status": "N/A", "remark": f"Error analysing file: {e}"}

    # Run all checklist items concurrently — no rate limit on the local LLM.
    non_section = [(i, item) for i, item in enumerate(items) if not item.get("is_section")]
    section_rows = {i: item for i, item in enumerate(items) if item.get("is_section")}

    results = await asyncio.gather(
        *[asyncio.to_thread(_analyse_item, item) for _, item in non_section]
    )

    checklist_rows = []
    filled: dict[int, dict] = {}
    ns_iter = iter(zip(non_section, results))
    next_ns = next(ns_iter, None)
    for i, item in enumerate(items):
        if item.get("is_section"):
            checklist_rows.append({**item, "status": "", "remark": ""})
        else:
            (_, _item), result = next_ns
            filled[item["position"]] = result
            checklist_rows.append({**item, "status": result["status"], "remark": result["remark"]})
            next_ns = next(ns_iter, None)

    # Build a downloadable Excel blob with the results filled in
    headers = {
        "customer_label": tpl.get("customer_label"),
        "address_label": tpl.get("address_label"),
        "job_label": tpl.get("job_label"),
    }
    xlsx_blob = cx.build_xlsx(items, headers, tpl.get("note_text", ""), filled=filled)

    # Two separate signed tokens so neither URL param grows too large.
    # result_token carries tpl + rows + quote_id (no binary blob).
    # dl_token carries only the xlsx bytes and is reused by the download endpoint.
    xlsx_b64 = base64.b64encode(xlsx_blob).decode()
    dl_token = _signer.dumps({"xlsx": xlsx_b64, "name": tpl["name"]})
    # Strip the raw Excel blob from tpl — it is bytes and not JSON-serialisable.
    tpl_safe = {k: v for k, v in tpl.items() if not isinstance(v, (bytes, bytearray))}
    yes_count = sum(1 for r in checklist_rows if r.get("status") == "Yes")
    no_count  = sum(1 for r in checklist_rows if r.get("status") == "No")
    na_count  = sum(1 for r in checklist_rows if r.get("status") == "N/A")
    result_token = _signer.dumps({
        "tpl": tpl_safe,
        "rows": checklist_rows,
        "quote_id": quote_id,
        "dl_token": dl_token,
        "zip_filename": zip_file.filename or "",
        "yes_count": yes_count,
        "no_count": no_count,
        "na_count": na_count,
    })
    rt_enc = urllib.parse.quote(result_token, safe="")
    return RedirectResponse(url=f"/checklist-result?token={rt_enc}", status_code=303)


@app.get("/checklist-result", response_class=HTMLResponse)
def checklist_result(request: Request, token: str, user=Depends(require_login)):
    try:
        payload = _signer.loads(token, max_age=7200)
    except BadSignature:
        raise HTTPException(400, "Result link has expired. Please re-run the checklist.")
    resp = templates.TemplateResponse(request, "user_result.html", {
        "current_user": user,
        "tpl": payload["tpl"],
        "checklist_rows": payload["rows"],
        "dl_token": payload["dl_token"],
        "quote_id": payload["quote_id"],
        "zip_filename": payload.get("zip_filename", ""),
        "yes_count": payload.get("yes_count", 0),
        "no_count": payload.get("no_count", 0),
        "na_count": payload.get("na_count", 0),
        "theme": _resolve_theme(user),
    })
    resp.headers.update(_NO_CACHE)
    return resp


@app.post("/checklist-save-edits")
async def checklist_save_edits(request: Request, _auth=Depends(require_login)):
    """
    Accepts modified checklist rows as JSON, rebuilds the Excel, and returns
    a new signed dl_token so the confirm flow can save the corrected version.
    """
    body = await request.json()
    rows        = body.get("rows", [])       # [{sno, text, status, remark, position, is_section, ...}]
    tpl         = body.get("tpl", {})
    orig_token  = body.get("dl_token", "")

    # Decode original token just to get template name (no need to validate Excel)
    tpl_name = tpl.get("name", "checklist")
    try:
        orig_payload = _signer.loads(orig_token, max_age=7200)
        tpl_name = orig_payload.get("name", tpl_name)
    except Exception:
        pass

    headers = {
        "customer_label": tpl.get("customer_label"),
        "address_label":  tpl.get("address_label"),
        "job_label":      tpl.get("job_label"),
    }
    note_text = tpl.get("note_text", "")

    # Build filled dict from edited rows
    filled = {}
    for row in rows:
        if not row.get("is_section") and row.get("position") is not None:
            filled[row["position"]] = {
                "status": row.get("status", "N/A"),
                "remark": row.get("remark", ""),
            }

    xlsx_blob = cx.build_xlsx(rows, headers, note_text, filled=filled)
    xlsx_b64  = base64.b64encode(xlsx_blob).decode()
    new_dl_token = _signer.dumps({"xlsx": xlsx_b64, "name": tpl_name})
    return {"dl_token": new_dl_token}


@app.get("/checklist-download", response_class=Response)
def checklist_download(token: str, _auth=Depends(require_login)):
    try:
        payload = _signer.loads(token, max_age=7200)
    except BadSignature:
        raise HTTPException(400, "Invalid or expired download token.")
    xlsx_blob = base64.b64decode(payload["xlsx"])
    safe_name = (payload.get("name") or "checklist").replace(" ", "_")
    return Response(
        content=xlsx_blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_QC.xlsx"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.post("/checklist-confirm")
def checklist_confirm(
    request: Request,
    quote_id: str = Form(""),
    dl_token: str = Form(""),
    zip_filename: str = Form(""),
    tpl_name: str = Form(""),
    yes_count: int = Form(0),
    no_count: int = Form(0),
    na_count: int = Form(0),
    rows_json: str = Form(""),
    user=Depends(require_login),
):
    """
    Called when the user clicks 'Confirm & Add to Calendar' on the result page.
    Saves the QC Excel as a new version and updates the latest snapshot on the quote.
    """
    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)

    # Persist the QC Excel as a confirmed version and update the latest snapshot.
    if dl_token and record:
        try:
            payload = _signer.loads(dl_token, max_age=7200)
            xlsx_bytes = base64.b64decode(payload["xlsx"])
            db.save_qc_excel(record["id"], xlsx_bytes)
            db.add_qc_version(
                quote_id=record["id"],
                xlsx_bytes=xlsx_bytes,
                template_name=tpl_name,
                zip_filename=zip_filename,
                yes_count=yes_count,
                no_count=no_count,
                na_count=na_count,
                rows_json=rows_json,
                confirmed_by_user_id=user["id"],
                saved_by_user_id=user["id"],
                status="confirmed",
            )
        except Exception:
            pass

    # Build redirect target: admin calendar, jumping to the install month if known.
    install_date = (record or {}).get("install_date", "").strip() if record else ""
    cal_param = ""
    if install_date:
        from datetime import datetime
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                dt = datetime.strptime(install_date, fmt)
                cal_param = f"?cal={dt.year}-{dt.month:02d}"
                break
            except ValueError:
                continue

    resp = templates.TemplateResponse(request, "user_thankyou.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "cal_param": cal_param,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@app.post("/checklist-save-draft")
def checklist_save_draft(
    request: Request,
    quote_id: str = Form(""),
    dl_token: str = Form(""),
    zip_filename: str = Form(""),
    tpl_name: str = Form(""),
    yes_count: int = Form(0),
    no_count: int = Form(0),
    na_count: int = Form(0),
    rows_json: str = Form(""),
    user=Depends(require_login),
):
    """Save QC results as a draft (no calendar entry). User can revisit later."""
    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)

    version_id = None
    if dl_token and record:
        try:
            payload = _signer.loads(dl_token, max_age=7200)
            xlsx_bytes = base64.b64decode(payload["xlsx"])
            version_id, _ = db.add_qc_version(
                quote_id=record["id"],
                xlsx_bytes=xlsx_bytes,
                template_name=tpl_name,
                zip_filename=zip_filename,
                yes_count=yes_count,
                no_count=no_count,
                na_count=na_count,
                rows_json=rows_json,
                saved_by_user_id=user["id"],
                status="draft",
            )
        except Exception:
            pass

    return RedirectResponse(url="/user/history", status_code=303)


@app.get("/user/history", response_class=HTMLResponse)
def user_history(request: Request, user=Depends(require_login)):
    history = db.get_qc_history_for_user(user["id"])
    resp = templates.TemplateResponse(request, "user_history.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "history": history,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@app.get("/user/qc-version/{version_id}", response_class=HTMLResponse)
def user_qc_version_revisit(request: Request, version_id: int, user=Depends(require_login)):
    """Let a user revisit a saved/confirmed QC version."""
    with __import__("database").get_db() as conn:
        row = conn.execute(
            """SELECT qv.*, q.customer_name, q.quote_number, q.install_date
               FROM qc_versions qv JOIN quotes q ON q.id = qv.quote_id
               WHERE qv.id = ?""",
            (version_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
    # Only the user who saved/confirmed it can revisit it (admins can too)
    if (user.get("role") != "admin"
            and v.get("saved_by_user_id") != user["id"]
            and v.get("confirmed_by_user_id") != user["id"]):
        raise HTTPException(403, "Access denied.")
    try:
        rows = json.loads(v["rows_json"]) if v.get("rows_json") else []
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    tpl = {"id": 0, "name": v.get("template_name", ""), "customer_label": "", "address_label": "", "job_label": "", "note_text": ""}

    # Re-sign a dl_token so download still works
    xlsx_b64 = base64.b64encode(bytes(v["excel_blob"])).decode() if v.get("excel_blob") else ""
    dl_token = _signer.dumps({"xlsx": xlsx_b64, "name": tpl["name"]}) if xlsx_b64 else ""

    yes_count = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "Yes")
    no_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "No")
    na_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "N/A")

    resp = templates.TemplateResponse(request, "user_result.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "tpl": tpl,
        "checklist_rows": rows,
        "dl_token": dl_token,
        "quote_id": v["quote_id"],
        "zip_filename": v.get("zip_filename", ""),
        "yes_count": yes_count,
        "no_count": no_count,
        "na_count": na_count,
        "revisit_version": v,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@app.get("/pdf", response_class=HTMLResponse)
def quote_detail(request: Request, id: int = None, user=Depends(require_login)):
    all_records = db.get_recent()
    if id is None:
        if not all_records:
            raise HTTPException(404, "No records found.")
        record = all_records[0]
    else:
        record = db.get_quote(id)
        if not record:
            raise HTTPException(404, "Quote not found.")
    display_num = next(
        (r["display_num"] for r in all_records if r["id"] == record["id"]),
        record["id"],
    )
    qc_versions = db.get_qc_versions(record["id"]) if record else []
    response = templates.TemplateResponse(
        request, "user_pdf.html",
        {
            "record": record,
            "display_num": display_num,
            "current_user": user,
            "theme": _resolve_theme(user),
            "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
            "has_qc_excel": bool(record.get("qc_excel")),
            "qc_versions": qc_versions,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/admin/qc-download/{quote_id}", response_class=Response)
def admin_qc_download(quote_id: int, user=Depends(require_admin)):
    """Download the latest QC Excel for a quote (admin only)."""
    record = db.get_quote(quote_id)
    if not record or not record.get("qc_excel"):
        raise HTTPException(404, "No QC report found for this record.")
    safe_name = (record.get("customer_name") or f"quote_{quote_id}").replace(" ", "_")
    return Response(
        content=bytes(record["qc_excel"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_QC.xlsx"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@app.get("/admin/qc-version/{version_id}/download", response_class=Response)
def admin_qc_version_download(version_id: int, user=Depends(require_admin)):
    """Download a specific QC version Excel (admin only)."""
    xlsx = db.get_qc_version_excel(version_id)
    if not xlsx:
        raise HTTPException(404, "QC version not found.")
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="QC_v{version_id}.xlsx"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@app.get("/admin/qc-version/{version_id}", response_class=HTMLResponse)
def admin_qc_version_view(request: Request, version_id: int, user=Depends(require_admin)):
    """Admin view of a specific QC version with inline edit capability."""
    with __import__("database").get_db() as conn:
        row = conn.execute(
            """SELECT qv.*, q.customer_name, q.quote_number, q.install_date,
                      q.email, q.phone, q.total_price, q.system_price,
                      q.deposit, q.balance, q.payment_terms, q.billing_address
               FROM qc_versions qv JOIN quotes q ON q.id = qv.quote_id
               WHERE qv.id = ?""",
            (version_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
    rows = json.loads(v["rows_json"]) if v.get("rows_json") else []
    resp = templates.TemplateResponse(request, "admin_qc_version.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
        "v": v,
        "rows": rows,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@app.post("/admin/qc-version/{version_id}/save")
async def admin_qc_version_save(request: Request, version_id: int, user=Depends(require_admin)):
    """Save admin edits to a QC version — rebuilds Excel and updates DB."""
    body = await request.json()
    rows = body.get("rows", [])

    # Rebuild Excel from edited rows
    filled = {}
    for row in rows:
        if not row.get("is_section") and row.get("position") is not None:
            filled[row["position"]] = {"status": row.get("status", "N/A"), "remark": row.get("remark", "")}

    xlsx_blob = cx.build_xlsx(rows, filled=filled)
    yes_count = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "Yes")
    no_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "No")
    na_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "N/A")

    db.update_qc_version(
        version_id=version_id,
        xlsx_bytes=xlsx_blob,
        rows_json=json.dumps(rows),
        yes_count=yes_count,
        no_count=no_count,
        na_count=na_count,
    )
    return {"ok": True, "yes_count": yes_count, "no_count": no_count, "na_count": na_count}


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail_legacy(request: Request, quote_id: int, user=Depends(require_login)):
    return RedirectResponse(url=f"/pdf?id={quote_id}", status_code=302)


@app.get("/user_upload", response_class=HTMLResponse)
def upload_get(request: Request, id: int = None, user=Depends(require_login)):
    result = None
    if id is not None:
        record = db.get_quote(id)
        if record:
            result = record.get("data") or record
            result["id"] = id
    response = templates.TemplateResponse(request, "user_home.html", _index_context(user=user, result=result))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/user_upload", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def upload(request: Request, file: UploadFile = File(...), user=Depends(require_login)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    file_bytes = await file.read()

    # H1 — file size cap
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="PDF file is too large. Maximum allowed size is 200 MB."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    # Extract text once — reused for both duplicate detection and local LLM extraction.
    import re as _re
    text = extract_pdf_text(file_bytes)
    print(f"[DEBUG] PDF text extracted, length={len(text)}")
    _qn_match = _re.search(r'(?i)quote\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z0-9\-]{4,20})', text)
    _detected_qn = _qn_match.group(1).strip() if _qn_match else None
    print(f"[DEBUG] Detected quote number: {_detected_qn!r}")
    existing = db.find_by_quote_number(_detected_qn) if _detected_qn else None
    print(f"[DEBUG] Existing record: {existing is not None}")
    if existing:
        pending_token = db.store_pending_pdf(file.filename, file_bytes, text)
        existing_data = existing.get("data") or existing
        existing_data["id"] = existing["id"]
        resp = templates.TemplateResponse(request, "user_confirm.html", {
            "current_user": user,
            "theme": _resolve_theme(user),
            "existing": existing_data,
            "pending_token": pending_token,
            "filename": file.filename,
        })
        resp.headers.update(_NO_CACHE)
        return resp

    try:
        data = extract_with_claude(text)
    except anthropic.AuthenticationError:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Invalid Anthropic API key. Check ANTHROPIC_API_KEY in your .env file and restart."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    except anthropic.APIError:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="AI service error. Please try again in a moment."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    quote_id = db.save_extraction(file.filename, data)
    return RedirectResponse(url=f"/user_upload?id={quote_id}", status_code=303)


@app.post("/user_upload/confirm", response_class=HTMLResponse)
async def upload_confirm(
    request: Request,
    action: str = Form(...),
    pending_token: str = Form(...),
    existing_id: int = Form(...),
    user=Depends(require_login),
):
    if action == "keep":
        # Consume the pending row so it doesn't linger; ignore if already gone.
        db.pop_pending_pdf(pending_token)
        return RedirectResponse(url=f"/user_upload?id={existing_id}", status_code=303)

    # action == "reextract" — retrieve PDF + pre-extracted text from DB.
    pending = db.pop_pending_pdf(pending_token)
    if not pending:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Session expired. Please upload the PDF again."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    # Reuse the text extracted during the initial upload — no second PDF parse.
    filename = pending["filename"]
    text = pending["pdf_text"]

    try:
        data = extract_with_claude(text)
    except anthropic.AuthenticationError:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Invalid Anthropic API key. Check ANTHROPIC_API_KEY in your .env file and restart."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    except anthropic.APIError:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="AI service error. Please try again in a moment."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    quote_id = db.save_extraction(filename, data)
    return RedirectResponse(url=f"/user_upload?id={quote_id}", status_code=303)


# ===========================================================================
# Checklist template management + editor
# ===========================================================================
@app.get("/Excel", response_class=HTMLResponse)
def template_editor(request: Request, user=Depends(require_login)):
    all_templates = tdb.list_templates()
    if not all_templates:
        raise HTTPException(404, "No templates found. Upload a checklist first.")
    tpl = tdb.get_template(all_templates[0]["id"])
    items = tdb.get_items(tpl["id"])
    response = templates.TemplateResponse(
        request, "user_excel.html",
        {"templates": all_templates, "selected": tpl, "items": items,
         "current_user": user, "theme": _resolve_theme(user)},
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/Excel/{template_id}", response_class=HTMLResponse)
def template_editor_by_id(request: Request, template_id: int, user=Depends(require_login)):
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)
    all_templates = tdb.list_templates()
    response = templates.TemplateResponse(
        request, "user_excel.html",
        {"templates": all_templates, "selected": tpl, "items": items,
         "current_user": user, "theme": _resolve_theme(user)},
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/templates/upload")
async def templates_upload(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    user=Depends(require_admin),  # M4 â€" admin only
):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx checklist template.")
    blob = await file.read()
    items, headers, note = cx.parse_xlsx(blob)
    tdb.create_template(name, blob, items, note)
    return RedirectResponse(url="/admin/templates", status_code=303)


@app.post("/templates/{template_id}/items/add")
def item_add(template_id: int, text: str = Form(...), sno: str = Form(""),
             is_section: str = Form(""), reference: str = Form(""),
             user=Depends(require_admin)):  # M4 â€" admin only
    tdb.add_item(template_id, text=text, sno=sno,
                 is_section=bool(is_section), reference=reference)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/templates/{template_id}/items/{item_id}/update")
def item_update(template_id: int, item_id: int,
                text: str = Form(...), sno: str = Form(""),
                reference: str = Form(""), prompt: str = Form(""),
                user=Depends(require_admin)):  # M4 â€" admin only
    tdb.update_item(item_id, text=text, sno=sno, reference=reference, prompt=prompt)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/templates/{template_id}/save-all")
async def items_save_all(template_id: int, request: Request, user=Depends(require_admin)):  # M4
    form = await request.form()
    item_ids = form.getlist("item_id")
    snos = form.getlist("sno")
    texts = form.getlist("text")
    refs = form.getlist("reference")
    prompts = form.getlist("prompt")
    for item_id, sno, text, ref, prompt in zip(item_ids, snos, texts, refs, prompts):
        tdb.update_item(int(item_id), text=text, sno=sno, reference=ref, prompt=prompt)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/templates/{template_id}/items/{item_id}/delete")
def item_delete(template_id: int, item_id: int, user=Depends(require_admin)):  # M4
    tdb.delete_item(item_id)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/templates/{template_id}/items/{item_id}/move")
def item_move(template_id: int, item_id: int, direction: str = Form(...),
              user=Depends(require_admin)):  # M4
    items = tdb.get_items(template_id)
    ids = [it["id"] for it in items]
    if item_id in ids:
        i = ids.index(item_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            tdb.reorder_items(template_id, ids)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/templates/{template_id}/reorder")
async def item_reorder(template_id: int, request: Request, user=Depends(require_admin)):
    form = await request.form()
    item_ids = form.getlist("item_id")
    if item_ids:
        tdb.reorder_items(template_id, [int(i) for i in item_ids])
    return Response(status_code=204)


@app.get("/admin/templates/{template_id}/fragment", response_class=HTMLResponse)
def admin_template_fragment(template_id: int, request: Request, user=Depends(require_admin)):
    """Return a bare HTML fragment for the inline template editor inside the admin dashboard."""
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)
    response = templates.TemplateResponse(
        request, "user_fragment.html",
        {"tpl": tpl, "items": items, "current_user": user, "theme": _resolve_theme(user)},
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/templates/{template_id}/delete")
def template_delete(template_id: int, user=Depends(require_admin)):  # M4
    tdb.delete_template(template_id)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/templates/{template_id}/download")
def template_download(template_id: int, user=Depends(require_login)):
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)
    headers = {
        "customer_label": tpl.get("customer_label"),
        "address_label": tpl.get("address_label"),
        "job_label": tpl.get("job_label"),
    }
    blob = cx.build_xlsx(items, headers, tpl.get("note_text", ""))
    safe = (tpl["name"] or "checklist").replace(" ", "_")
    return StreamingResponse(
        io.BytesIO(blob),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

