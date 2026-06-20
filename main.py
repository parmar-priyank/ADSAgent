import os
import io
import json
import base64
import mimetypes
import secrets
import zipfile as zipmod

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Form, Depends
from fastapi.responses import (
    HTMLResponse, Response, StreamingResponse, RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from slowapi import Limiter, _rate_limit_exceeded_handler
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
# Config — loaded from .env
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
SECRET_KEY   = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<random-string> to your .env file and restart."
    )

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB hard cap for all uploads

# A random ID generated fresh every time the server starts.
# Embedding it in every session token means all existing sessions are
# automatically invalidated on restart — users must log in again.
_SERVER_INSTANCE_ID = secrets.token_hex(8)

# Rate limiter (keyed by client IP)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Solar Agreement Extractor",
    docs_url=None,    # disable /docs  (H5)
    redoc_url=None,   # disable /redoc (H5)
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")



# ---------------------------------------------------------------------------
# Security headers middleware  (L1)
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

app.add_middleware(SecurityHeadersMiddleware)

_signer = URLSafeTimedSerializer(SECRET_KEY)

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
COOKIE = "session"

SESSION_MAX_AGE = 86400 * 7  # 7 days — must match _signer max_age in _get_session

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
    # Reject tokens issued before this server boot — forces re-login on restart.
    if data.get("sid") != _SERVER_INSTANCE_ID:
        return None
    return data

class _AuthRedirect(Exception):
    """Raised by auth dependencies to trigger a redirect that also clears the stale cookie."""
    def __init__(self, url: str):
        self.url = url

async def _auth_redirect_handler(_request: Request, exc: _AuthRedirect):
    r = RedirectResponse(url=exc.url, status_code=303)
    r.delete_cookie(COOKIE)
    return r

app.add_exception_handler(_AuthRedirect, _auth_redirect_handler)

def require_login(request: Request):
    user = _get_session(request)
    if not user:
        raise _AuthRedirect("/login")
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
        "===== LEFT COLUMN — CUSTOMER SIDE (use for the customer_* fields) =====\n"
        + "\n".join(left_parts)
        + "\n\n===== RIGHT COLUMN — RETAILER SIDE (use for the retailer_* fields; "
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
    "email": "string (the customer's email, e.g. a personal address — never sales@adssolar.com.au)",
    "phone": "string (the customer's phone/mobile — never the retailer's 1300 number)",
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
        "amount appears at the END of its row — do not leave a price blank if the FULL "
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
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not set. Add it to your .env file.")

    client = Groq(api_key=GROQ_API_KEY)

    system_prompt = (
        "You are a precise data extraction engine for solar agreements. "
        "The document has a CUSTOMER side and a RETAILER side. "
        "Extract ONLY the CUSTOMER's details for the customer fields. "
        "NEVER mix in retailer details: the retailer is 'ADS Pty Ltd t/as ADS Solar', "
        "its contact person is 'Nik', its postal address contains 'PO Box 6208 / Norwest', "
        "its street address is 'Solent Circuit, Baulkham Hills', its email is "
        "'sales@adssolar.com.au', and its phone is a 1300 number — none of these belong "
        "in customer fields. The CUSTOMER section is under the 'LEFT COLUMN' heading "
        "and is the source for the customer_* fields. The RETAILER section is under the "
        "'RIGHT COLUMN' heading and IS the source for the retailer_* fields: fill "
        "retailer_name, retailer_contact_person, retailer_postal_address, "
        "retailer_street_address, retailer_phone, and retailer_email from that RIGHT "
        "section (e.g. retailer_contact_person='Nik', retailer_email='sales@adssolar.com.au'). "
        "Do NOT use the RIGHT section for any customer field, and do NOT use the LEFT "
        "section for any retailer field. For the line_items pricing table, take the PRICE for "
        "each row from the 'FULL TEXT' section (the column-split text clips the price "
        "column, so prices may be missing there) — every row that shows a dollar amount "
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
    """Common context for any render of home.html."""
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

@app.get("/login", response_class=HTMLResponse)
def login_user_page(request: Request):
    """Public user login page."""
    user = _get_session(request)
    if user and user.get("role") == "user":
        return RedirectResponse(url="/home", status_code=302)
    response = templates.TemplateResponse(request, "login_user.html", {"error": None})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/login", response_class=HTMLResponse)
@limiter.limit("5/minute")
def login_user_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = adb.verify_user(username, password)
    if not user or user.get("role") == "admin":
        resp = templates.TemplateResponse(
            request, "login_user.html",
            {"error": "Invalid username or password."}
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp
    response = RedirectResponse(url="/home", status_code=303)
    _set_session(response, user)
    return response


@app.get("/admin-dashboard", response_class=HTMLResponse)
def login_admin_page(request: Request):
    """Secret admin login — URL not publicly linked anywhere."""
    user = _get_session(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    response = templates.TemplateResponse(request, "login_admin.html", {"error": None})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/admin-dashboard", response_class=HTMLResponse)
@limiter.limit("5/minute")
def login_admin_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = adb.verify_user(username, password)
    if not user or user.get("role") != "admin":
        resp = templates.TemplateResponse(
            request, "login_admin.html",
            {"error": "Invalid credentials."}
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
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


@app.get("/toggle-theme")
def toggle_theme(request: Request, user=Depends(require_login)):
    current = _resolve_theme(user)
    adb.set_user_theme(user["id"], "light" if current == "dark" else "dark")
    referer = request.headers.get("referer", "/home")
    return RedirectResponse(url=referer, status_code=303)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user=Depends(require_admin)):
    response = templates.TemplateResponse(request, "admin.html", {
        "current_user": user,
        "users": adb.list_users(),
        "records": db.get_recent(),
        "templates": tdb.list_templates(),
        "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
        "error": None,
        "success": None,
    })
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
    # H2 — allowlist role to prevent arbitrary role injection via crafted POST
    if role not in {"user", "admin"}:
        role = "user"
    ok = adb.create_user(username, password, role)
    ctx = {
        "current_user": user,
        "users": adb.list_users(),
        "records": db.get_recent(),
        "templates": tdb.list_templates(),
        "error": (
            f"Username '{username}' already exists, is invalid, or password is too short (min. 8 characters)."
            if not ok else None
        ),
        "success": f"User '{username}' created successfully." if ok else None,
    }
    return templates.TemplateResponse(request, "admin.html", ctx)


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(user_id: int, request: Request, user=Depends(require_admin)):
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    response = templates.TemplateResponse(request, "admin_user.html", {
        "current_user": user,
        "target": target,
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
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/settings/theme")
def admin_set_theme(request: Request, theme: str = Form(...), user=Depends(require_admin)):
    if theme in ("dark", "light"):
        adb.set_setting("user_panel_theme", theme)
    return RedirectResponse(url="/admin#settings", status_code=303)


# ---------------------------------------------------------------------------
# App routes (require login)
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def root_redirect():
    return RedirectResponse(url="/home", status_code=302)


@app.get("/home", response_class=HTMLResponse)
def home(request: Request, user=Depends(require_login)):
    response = templates.TemplateResponse(request, "home.html", _index_context(user=user))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


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

    # Extract ZIP into a flat dict: lowercase filename → bytes
    data = await zip_file.read()

    # H1 — file size cap
    _no_cache = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

    if len(data) > MAX_UPLOAD_BYTES:
        resp = templates.TemplateResponse(request, "home.html",
            _index_context(user=user, error="ZIP file is too large. Maximum allowed size is 30 MB."))
        resp.headers.update(_no_cache)
        return resp

    zip_files: dict[str, bytes] = {}
    try:
        with zipmod.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                # H3 — use os.path.basename to handle both / and \ separators safely
                basename = os.path.basename(name).strip()
                if basename:
                    zip_files[basename.lower()] = zf.read(name)
    except zipmod.BadZipFile:
        resp = templates.TemplateResponse(request, "home.html",
            _index_context(user=user, error="Uploaded file is not a valid ZIP archive."))
        resp.headers.update(_no_cache)
        return resp

    client = Groq(api_key=GROQ_API_KEY)
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

    def _analyse_item(item: dict) -> dict:
        ref = (item.get("reference") or "").strip()
        prompt_text = (item.get("prompt") or "").strip()

        if not ref:
            return {"status": "N/A", "remark": "No reference file specified."}
        file_bytes = zip_files.get(ref.lower())
        if file_bytes is None:
            return {"status": "N/A", "remark": f"File not found in ZIP: {ref}"}
        if not prompt_text:
            return {"status": "N/A", "remark": "No prompt defined for this item."}

        mime, _ = mimetypes.guess_type(ref)
        mime = mime or "application/octet-stream"
        context = f"Checklist item: {item['text']}\nRequirement: {prompt_text}"

        try:
            if mime == "application/pdf":
                try:
                    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                        pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                except Exception:
                    pdf_text = ""
                if not pdf_text.strip():
                    return {"status": "N/A", "remark": "Could not extract text from PDF."}
                messages = [
                    {"role": "system", "content": QC_SYSTEM},
                    {"role": "user", "content": f"{context}\n\nDocument content:\n{pdf_text[:6000]}"},
                ]
                return _groq_check(GROQ_MODEL, messages)

            elif mime.startswith("image/"):
                b64 = base64.standard_b64encode(file_bytes).decode()
                messages = [
                    {"role": "system", "content": QC_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": context},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]},
                ]
                return _groq_check("meta-llama/llama-4-scout-17b-16e-instruct", messages)

            else:
                return {"status": "N/A", "remark": f"Unsupported file type: {mime}"}

        except Exception:
            # M6 — never expose internal exception details to the user
            return {"status": "N/A", "remark": "Error analysing file. Please check the file format and try again."}

    # Run analysis for every non-section item that has a reference
    checklist_rows = []
    filled: dict[int, dict] = {}
    for item in items:
        if item.get("is_section"):
            checklist_rows.append({**item, "status": "", "remark": ""})
            continue
        result = _analyse_item(item)
        filled[item["position"]] = result
        checklist_rows.append({**item, "status": result["status"], "remark": result["remark"]})

    # Build a downloadable Excel blob with the results filled in
    headers = {
        "customer_label": tpl.get("customer_label"),
        "address_label": tpl.get("address_label"),
        "job_label": tpl.get("job_label"),
    }
    xlsx_blob = cx.build_xlsx(items, headers, tpl.get("note_text", ""), filled=filled)

    # Store blob temporarily in session via a signed token so download works
    xlsx_b64 = base64.b64encode(xlsx_blob).decode()
    dl_token = _signer.dumps({"xlsx": xlsx_b64, "name": tpl["name"]})

    resp = templates.TemplateResponse(request, "checklist_result.html", {
        "current_user": user,
        "tpl": tpl,
        "checklist_rows": checklist_rows,
        "dl_token": dl_token,
        "quote_id": quote_id,
    })
    resp.headers.update(_no_cache)
    return resp


@app.get("/checklist-download", response_class=Response)
def checklist_download(token: str, _auth=Depends(require_login)):
    try:
        payload = _signer.loads(token, max_age=3600)
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
        request, "quote_detail.html",
        {"record": record, "display_num": display_num, "current_user": user},
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail_legacy(request: Request, quote_id: int, user=Depends(require_login)):
    return RedirectResponse(url=f"/pdf?id={quote_id}", status_code=302)


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...), user=Depends(require_login)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    file_bytes = await file.read()

    # H1 — file size cap
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        resp = templates.TemplateResponse(
            request, "home.html",
            _index_context(user=user, error="PDF file is too large. Maximum allowed size is 30 MB."),
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    text = extract_pdf_text(file_bytes)

    try:
        data = extract_with_groq(text)
    except AuthenticationError:
        resp = templates.TemplateResponse(
            request, "home.html",
            _index_context(user=user, error="Invalid Groq API key. Check the GROQ_API_KEY value in your .env file "
                                 "(it should start with 'gsk_') and restart the server."),
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp
    except GroqError:
        resp = templates.TemplateResponse(
            request, "home.html",
            _index_context(user=user, error="AI service error. Please try again in a moment."),
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    db.save_extraction(file.filename, data)
    resp = templates.TemplateResponse(
        request, "home.html", _index_context(user=user, result=data),
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
        request, "templates.html",
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
        request, "templates.html",
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
    user=Depends(require_admin),  # M4 — admin only
):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx checklist template.")
    blob = await file.read()
    items, headers, note = cx.parse_xlsx(blob)
    tid = tdb.create_template(name, blob, items, note)
    return RedirectResponse(url="/Excel", status_code=303)


@app.post("/templates/{template_id}/items/add")
def item_add(template_id: int, text: str = Form(...), sno: str = Form(""),
             is_section: str = Form(""), reference: str = Form(""),
             user=Depends(require_admin)):  # M4 — admin only
    tdb.add_item(template_id, text=text, sno=sno,
                 is_section=bool(is_section), reference=reference)
    return RedirectResponse(url="/Excel", status_code=303)


@app.post("/templates/{template_id}/items/{item_id}/update")
def item_update(template_id: int, item_id: int,
                text: str = Form(...), sno: str = Form(""),
                reference: str = Form(""), prompt: str = Form(""),
                user=Depends(require_admin)):  # M4 — admin only
    tdb.update_item(item_id, text=text, sno=sno, reference=reference, prompt=prompt)
    return RedirectResponse(url="/Excel", status_code=303)


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
    return RedirectResponse(url="/Excel", status_code=303)


@app.post("/templates/{template_id}/items/{item_id}/delete")
def item_delete(template_id: int, item_id: int, user=Depends(require_admin)):  # M4
    tdb.delete_item(item_id)
    return RedirectResponse(url="/Excel", status_code=303)


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
    return RedirectResponse(url="/Excel", status_code=303)


@app.post("/templates/{template_id}/delete")
def template_delete(template_id: int, return_to: str = Form(""),
                    user=Depends(require_admin)):  # M4
    tdb.delete_template(template_id)
    if return_to.isdigit() and int(return_to) != template_id:
        return RedirectResponse(url="/Excel", status_code=303)
    remaining = tdb.list_templates()
    if remaining:
        return RedirectResponse(url="/Excel", status_code=303)
    return RedirectResponse(url="/home", status_code=303)


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