"""
routers/uploads.py — PDF upload and quote detail routes.

Routes:
  GET  /
  GET  /home
  GET  /user_home
  GET  /user_upload
  POST /user_upload
  POST /user_upload/confirm
  GET  /pdf
  GET  /quotes/{quote_id}
"""
import re

import anthropic

import db.quote_repo as db
import db.user_repo as adb
from services.ai_service import extract_pdf_text, extract_with_claude

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from config import (
    MAX_UPLOAD_BYTES,
    _NO_CACHE,
    _get_session,
    _index_context,
    _resolve_theme,
    limiter,
    require_login,
    require_qc_access,
    templates,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Home / redirect
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
@router.get("/home", response_class=HTMLResponse)
def root_redirect(request: Request):
    user = _get_session(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(url="/admin", status_code=302)
    return RedirectResponse(url="/user_home", status_code=302)


@router.get("/user/heartbeat")
def user_heartbeat(request: Request, user=Depends(require_login)):
    """No-op ping so genuine reading/thinking time (no clicks) still counts
    as activity — passing through require_login lets
    InactivityTimeoutMiddleware refresh the session's idle timer."""
    return {"ok": True}


@router.get("/user_home", response_class=HTMLResponse)
def home(request: Request, verify_required: int = 0, user=Depends(require_login)):
    ctx = _index_context(user=user)
    if verify_required:
        ctx["verify_required"] = True
    response = templates.TemplateResponse(request, "user_home.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


# ---------------------------------------------------------------------------
# PDF upload
# ---------------------------------------------------------------------------

@router.get("/user_upload", response_class=HTMLResponse)
def upload_get(request: Request, id: int = None, user=Depends(require_qc_access)):
    result = None
    if id is not None:
        record = db.get_quote(id)
        if record:
            result = record.get("data") or record
            result["id"] = id
    response = templates.TemplateResponse(
        request, "user_home.html", _index_context(user=user, result=result)
    )
    response.headers.update(_NO_CACHE)
    return response


@router.post("/user_upload", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    preferred_install_date: str = Form(""),
    user=Depends(require_qc_access),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    file_bytes = await file.read()

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="PDF file is too large. Maximum allowed size is 200 MB."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    text = extract_pdf_text(file_bytes)
    _qn_match = re.search(r'(?i)quote\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z0-9\-]{4,20})', text)
    _detected_qn = _qn_match.group(1).strip() if _qn_match else None
    existing = db.find_by_quote_number(_detected_qn) if _detected_qn else None
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
            "preferred_install_date": preferred_install_date.strip(),
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

    _key_fields = ("customer_name", "quote_number", "total_price", "billing_address")
    if not any(str(data.get(f) or "").strip() for f in _key_fields):
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="No agreement data could be extracted from this PDF. Please upload a valid signed solar agreement."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    data["preferred_install_date"] = preferred_install_date.strip()
    quote_id = db.save_extraction(file.filename, data)
    return RedirectResponse(url=f"/user_upload?id={quote_id}", status_code=303)


@router.post("/user_upload/confirm", response_class=HTMLResponse)
async def upload_confirm(
    request: Request,
    action: str = Form(...),
    pending_token: str = Form(...),
    existing_id: int = Form(...),
    preferred_install_date: str = Form(""),
    user=Depends(require_qc_access),
):
    if action == "keep":
        db.pop_pending_pdf(pending_token)
        return RedirectResponse(url=f"/user_upload?id={existing_id}", status_code=303)

    pending = db.pop_pending_pdf(pending_token)
    if not pending:
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="Session expired. Please upload the PDF again."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

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

    _key_fields = ("customer_name", "quote_number", "total_price", "billing_address")
    if not any(str(data.get(f) or "").strip() for f in _key_fields):
        resp = templates.TemplateResponse(
            request, "user_home.html",
            _index_context(user=user, error="No agreement data could be extracted from this PDF. Please upload a valid signed solar agreement."),
        )
        resp.headers.update(_NO_CACHE)
        return resp

    data["preferred_install_date"] = preferred_install_date.strip()
    quote_id = db.save_extraction(filename, data)
    return RedirectResponse(url=f"/user_upload?id={quote_id}", status_code=303)


# ---------------------------------------------------------------------------
# Quote detail
# ---------------------------------------------------------------------------

@router.get("/pdf", response_class=HTMLResponse)
def quote_detail(request: Request, id: int = None, user=Depends(require_qc_access)):
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
    response.headers.update(_NO_CACHE)
    return response


@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail_legacy(request: Request, quote_id: int, user=Depends(require_qc_access)):
    return RedirectResponse(url=f"/pdf?id={quote_id}", status_code=302)
