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
from groq import Groq
from groq import AuthenticationError, GroqError

import records as db      # quote/extraction storage
import checklists as tdb  # checklist template storage
import excel as cx         # parse / build checklist Excel files
import users as adb        # user accounts

# ---------------------------------------------------------------------------
# Config â€" loaded from .env
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY          = os.environ.get("GROQ_API_KEY")
GROQ_MODEL            = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
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

# Groq client initialised once at module load -- no mutable global, no race condition.
if not GROQ_API_KEY:
    _groq_client = None
else:
    _groq_client = Groq(api_key=GROQ_API_KEY)

def _get_groq():
    if _groq_client is None:
        raise HTTPException(500, "GROQ_API_KEY not set. Add it to your .env file.")
    return _groq_client

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB hard cap for all uploads

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

# A random ID generated fresh every time the server starts.
# Embedding it in every session token means all existing sessions are
# automatically invalidated on restart â€" users must log in again.
_SERVER_INSTANCE_ID = secrets.token_hex(8)

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
        "sid": _SERVER_INSTANCE_ID,
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
    # Reject tokens issued before this server boot â€" forces re-login on restart.
    if data.get("sid") != _SERVER_INSTANCE_ID:
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
        token = request.cookies.get(COOKIE)
        try:
            data = _signer.loads(token, max_age=SESSION_MAX_AGE)
            if data.get("sid") != _SERVER_INSTANCE_ID:
                r.delete_cookie(COOKIE)
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
# Groq extraction
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
    "roof_type": "string",
    "line_items": (
        "array of objects covering EVERY row of the System/pricing table in order, "
        "INCLUDING the equipment rows (Panels, Inverter, Inverter phase, Racking, "
        "Optimisers, Exp ctrl device, Battery, Stories, Extended Warranty, Options, "
        "Roof Type) AND the summary/financial rows (System Price, Less STC incentive, "
        "VIC Interest free Loan, VIC Rebate, ACT Govt Next Gen Rebate, Battery Rebate, "
        "Total Price). Each object: {\"item\": string (the row label, e.g. 'Panels', "
        "'System Price', 'Less STC incentive', 'VIC Rebate', 'Total Price'), "
        "\"quantity\": string (the number before the 'X' at the start of the specification, "
        "e.g. '26' for '26.0 X JKM510N...', '1' for '1.0 X FOXESS...'; drop the trailing "
        "'.0'; empty for summary/financial rows or rows with no quantity like 'Roof Type'), "
        "\"specification\": string (the description/spec text WITHOUT the leading "
        "'<number> X ' quantity prefix, or empty for summary rows that have none), "
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


def extract_with_groq(text: str) -> dict:
    client = _get_groq()

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

    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    return json.loads(resp.choices[0].message.content)


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
    if next_url and next_url.startswith(origin):
        dest = next_url
    else:
        referer = request.headers.get("referer", "")
        dest = referer if referer.startswith(origin) else "/user_home"
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
def admin_dashboard(request: Request, user=Depends(require_admin)):
    records = db.get_recent()
    ctx = _admin_ctx(user,
        users_count=len(adb.list_users()),
        records_count=len(records),
        templates_count=len(tdb.list_templates()),
        install_map_json=_build_install_map(records),
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
def admin_user_detail(user_id: int, request: Request, user=Depends(require_admin)):
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    response = templates.TemplateResponse(request, "admin_user.html", {
        "current_user": user,
        "target": target,
        "theme": _resolve_theme(user),
        "success": None,
        "error": None,
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response



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
        return _qc_error("ZIP file is too large. Maximum allowed size is 30 MB.")

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

    client = _get_groq()
    QC_SYSTEM = (
        "You are a QC document checker. "
        "Determine if the requirement is met based on the provided content. "
        'Reply with JSON only: {"status": "Yes" or "No", "remark": "one sentence explanation"}'
    )

    def _groq_check(model: str, messages: list) -> dict:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        r = json.loads(resp.choices[0].message.content)
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
        if missing:
            return {"status": "N/A", "remark": f"File not found in ZIP: {', '.join(missing)}"}

        context = f"Checklist item: {item['text']}\nRequirement: {prompt_text}"

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

            # Extract text from PDFs; scanned PDFs (no text) are rendered as images instead
            combined_pdf_text = ""
            scanned_pdf_images = []  # (fname, page_image_bytes) for text-less PDFs
            for fname, fdata, _ in pdf_parts:
                try:
                    with pdfplumber.open(io.BytesIO(fdata)) as pdf:
                        pages_text = [p.extract_text() or "" for p in pdf.pages]
                    text = "\n".join(pages_text)
                except Exception as e:
                    return {"status": "N/A", "remark": f"Could not open PDF '{fname}': {e}"}

                if text.strip():
                    combined_pdf_text += f"\n\n--- {fname} ---\n{text}"
                else:
                    # Scanned/image-only PDF — render each page as a PNG for the vision model
                    try:
                        import fitz  # PyMuPDF
                        doc = fitz.open(stream=fdata, filetype="pdf")
                        for page in doc:
                            pix = page.get_pixmap(dpi=150)
                            scanned_pdf_images.append((fname, pix.tobytes("png")))
                        doc.close()
                    except Exception:
                        # PyMuPDF not available — report as unreadable
                        return {"status": "N/A", "remark": f"PDF '{fname}' appears to be scanned (no text layer) and could not be rendered. Install PyMuPDF (pip install pymupdf) for scanned PDF support."}

            needs_vision = bool(image_parts or scanned_pdf_images)

            # If only text-based PDFs and no images, use the text model
            if pdf_parts and not needs_vision:
                if not combined_pdf_text.strip():
                    return {"status": "N/A", "remark": "Could not extract text from PDF(s)."}
                messages = [
                    {"role": "system", "content": QC_SYSTEM},
                    {"role": "user", "content": f"{context}\n\nDocument content:{combined_pdf_text[:8000]}"},
                ]
                return _groq_check(GROQ_MODEL, messages)

            # Vision path — combine text context + real images + scanned PDF pages
            user_content = [{"type": "text", "text": context}]
            if combined_pdf_text.strip():
                user_content.append({"type": "text", "text": f"Document content:{combined_pdf_text[:4000]}"})
            for fname, fdata, mime in image_parts:
                b64 = base64.standard_b64encode(fdata).decode()
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            for fname, page_png in scanned_pdf_images:
                b64 = base64.standard_b64encode(page_png).decode()
                user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

            messages = [
                {"role": "system", "content": QC_SYSTEM},
                {"role": "user", "content": user_content},
            ]
            return _groq_check("meta-llama/llama-4-scout-17b-16e-instruct", messages)

        except Exception as e:
            return {"status": "N/A", "remark": f"Error analysing file: {e}"}

    # Run analysis concurrently — each Groq call is a blocking HTTP request so
    # we offload to threads to avoid freezing the event loop.
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
    result_token = _signer.dumps({
        "tpl": tpl_safe,
        "rows": checklist_rows,
        "quote_id": quote_id,
        "dl_token": dl_token,
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
        "theme": _resolve_theme(user),
    })
    resp.headers.update(_NO_CACHE)
    return resp


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
    response = templates.TemplateResponse(
        request, "user_pdf.html",
        {"record": record, "display_num": display_num, "current_user": user, "theme": _resolve_theme(user), "user_panel_theme": adb.get_setting("user_panel_theme", "dark")},
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


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
async def upload(request: Request, file: UploadFile = File(...), user=Depends(require_login)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    file_bytes = await file.read()

    # H1 â€" file size cap
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="PDF file is too large. Maximum allowed size is 30 MB."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    # Duplicate detection: extract the quote number from the PDF first (cheap text
    # scan), then look it up by quote_number — not filename, which is not unique
    # across different customers who both happen to save their file as "quote.pdf".
    import re as _re
    _qn_text = extract_pdf_text(file_bytes)
    _qn_match = _re.search(r'(?i)quote\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z0-9\-]{4,20})', _qn_text)
    _detected_qn = _qn_match.group(1).strip() if _qn_match else None
    existing = db.find_by_quote_number(_detected_qn) if _detected_qn else None
    if existing:
        pdf_b64 = base64.b64encode(file_bytes).decode()
        pdf_token = _signer.dumps({"pdf": pdf_b64, "filename": file.filename})
        existing_data = existing.get("data") or existing
        existing_data["id"] = existing["id"]
        resp = templates.TemplateResponse(request, "user_confirm.html", {
            "current_user": user,
            "theme": _resolve_theme(user),
            "existing": existing_data,
            "pdf_token": pdf_token,
            "filename": file.filename,
        })
        resp.headers.update(_NO_CACHE)
        return resp

    text = extract_pdf_text(file_bytes)

    try:
        data = extract_with_groq(text)
    except AuthenticationError:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Invalid Groq API key. Check the GROQ_API_KEY value in your .env file "
                                 "(it should start with 'gsk_') and restart the server."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    except GroqError:
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
    pdf_token: str = Form(...),
    existing_id: int = Form(...),
    user=Depends(require_login),
):
    if action == "keep":
        return RedirectResponse(url=f"/user_upload?id={existing_id}", status_code=303)

    # action == "reextract" â€" decode the stored PDF and run Groq
    try:
        payload = _signer.loads(pdf_token, max_age=3600)
    except BadSignature:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Session expired. Please upload the PDF again."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    file_bytes = base64.b64decode(payload["pdf"])
    filename = payload["filename"]
    text = extract_pdf_text(file_bytes)

    try:
        data = extract_with_groq(text)
    except AuthenticationError:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Invalid Groq API key."),
        )
        resp.headers.update(_NO_CACHE)
        return resp
    except GroqError:
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

