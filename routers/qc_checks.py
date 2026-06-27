"""
routers/qc_checks.py — QC checklist execution and history routes.

Routes:
  GET  /qc-check
  POST /run-checklist
  GET  /checklist-result
  POST /checklist-save-edits
  GET  /checklist-download
  POST /checklist-confirm
  POST /checklist-save-draft
  GET  /user/history
  GET  /user/qc-version/{version_id}
"""
import asyncio
import base64
import io
import json
import mimetypes
import os
import urllib.parse
import zipfile as zipmod
from datetime import datetime

import pdfplumber

try:
    import fitz as _fitz   # PyMuPDF — optional; graceful fallback to text-only if missing
except ImportError:
    _fitz = None

_PDF_DPI = 200   # 200 DPI: signatures and checkboxes render sharply for Claude vision

import db.quote_repo as db
import db.checklist_repo as tdb
from reports.xlsx_builder import build_xlsx
from db.connection import get_db

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature

from config import (
    CLAUDE_MODEL,
    MAX_UPLOAD_BYTES,
    _NO_CACHE,
    _get_claude,
    _resolve_theme,
    _signer,
    require_login,
    templates,
)

router = APIRouter()

# Tracks which user IDs currently have a checklist run in progress.
# Prevents a user from submitting two overlapping jobs that would race
# on the same signed tokens. Each worker process has its own set, which
# is fine — jobs from the same user can land on different workers safely.
_active_jobs: set[int] = set()

QC_SYSTEM = (
    "You are a QC document checker. "
    "Determine if the requirement is met based on the provided content. "
    'Reply with JSON only: {"status": "Yes" or "No", "remark": "one sentence explanation"}'
)


def _render_pdf(fname: str, fdata: bytes) -> tuple[str, list[bytes]]:
    """Return (extracted_text, [page_png_bytes]) for a PDF.

    Called once per unique PDF before the parallel checklist loop so every
    checklist item that references the same file shares the rendered pages.
    """
    text = ""
    try:
        with pdfplumber.open(io.BytesIO(fdata)) as pdf:
            pages_text = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages_text)
    except Exception:
        pass

    pages: list[bytes] = []
    if _fitz is not None:
        try:
            doc = _fitz.open(stream=fdata, filetype="pdf")
            for page in doc:
                pix = page.get_pixmap(dpi=_PDF_DPI)
                pages.append(pix.tobytes("png"))
            doc.close()
        except Exception:
            pass

    return text, pages


def _mime_of(fname: str, fbytes: bytes) -> str:
    mime, _ = mimetypes.guess_type(fname)
    if mime:
        return mime
    if fbytes[:4] == b"%PDF":
        return "application/pdf"
    if fbytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if fbytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if fbytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if fbytes[:4] == b"RIFF" and fbytes[8:12] == b"WEBP":
        return "image/webp"
    if fbytes[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


def _claude_check(client, user_content) -> dict:
    resp = client.messages.create(
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


# ---------------------------------------------------------------------------
# Email verify (Step 2) — upload .eml, extract attachments, match vs reference PDF
# ---------------------------------------------------------------------------

def _parse_eml(raw: bytes) -> list[dict]:
    """
    Parse a raw .eml file and return a list of attachment dicts:
      {name, mime, data, size_kb}
    Inline images and text parts are skipped — only named attachments are returned.
    Uses Python's stdlib email parser — no extra dependencies needed.
    """
    import email as _email
    import email.policy as _policy

    msg = _email.message_from_bytes(raw, policy=_policy.compat32)
    attachments = []
    for part in msg.walk():
        disposition = part.get_content_disposition() or ""
        fname = part.get_filename()
        if not fname:
            continue
        # Decode RFC-2047 encoded filenames (e.g. =?utf-8?q?...?=)
        from email.header import decode_header as _dh
        decoded_parts = _dh(fname)
        fname = "".join(
            (t.decode(enc or "utf-8") if isinstance(t, bytes) else t)
            for t, enc in decoded_parts
        )
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append({
            "name": fname,
            "mime": part.get_content_type(),
            "data": payload,
            "size_kb": round(len(payload) / 1024, 1),
        })
    return attachments


def _match_attachment_with_claude(client, attachment: dict, reference_text: str) -> dict:
    """
    Send one email attachment to Claude with the reference PDF text and ask
    whether the attachment details match the signed agreement.
    Returns {name, size_kb, mime, status, remark}.
    """
    name = attachment["name"]
    data = attachment["data"]
    mime = attachment["mime"]
    size_kb = attachment["size_kb"]

    prompt = (
        f"You are verifying whether an email attachment sent to a customer "
        f"matches the details of their signed solar agreement.\n\n"
        f"Attachment filename: {name}\n\n"
        f"--- SIGNED AGREEMENT DETAILS ---\n{reference_text}\n\n"
        f"Based on the attachment content and the agreement details above, "
        f"answer: does this attachment appear to be correct and consistent "
        f"with the signed agreement? "
        f'Reply with JSON only: {{"status": "Yes" or "No" or "N/A", '
        f'"remark": "one concise sentence explaining your finding"}}'
    )

    user_content: list = [{"type": "text", "text": prompt}]

    # If it is a PDF, render pages and send as images
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        pages: list[bytes] = []
        if _fitz is not None:
            try:
                doc = _fitz.open(stream=data, filetype="pdf")
                for page in doc:
                    pix = page.get_pixmap(dpi=_PDF_DPI)
                    pages.append(pix.tobytes("png"))
                doc.close()
            except Exception:
                pass
        # Also extract text
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                txt = "\n".join(p.extract_text() or "" for p in pdf.pages)
            if txt.strip():
                user_content.append({"type": "text", "text": f"Attachment text:\n{txt[:6000]}"})
        except Exception:
            pass
        for png in pages:
            b64 = base64.standard_b64encode(png).decode()
            user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    elif mime.startswith("image/"):
        b64 = base64.standard_b64encode(data).decode()
        user_content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
    else:
        # Plain text / docx / xlsx — send raw text if decodable
        try:
            user_content.append({"type": "text", "text": f"Attachment content (raw text):\n{data.decode('utf-8', errors='replace')[:6000]}"})
        except Exception:
            return {"name": name, "size_kb": size_kb, "mime": mime,
                    "status": "N/A", "remark": "Unsupported attachment format — cannot read content."}

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_resp = resp.content[0].text.strip()
        if raw_resp.startswith("```"):
            raw_resp = raw_resp.split("```", 2)[1]
            if raw_resp.startswith("json"):
                raw_resp = raw_resp[4:]
            raw_resp = raw_resp.rsplit("```", 1)[0].strip()
        r = json.loads(raw_resp)
        return {"name": name, "size_kb": size_kb, "mime": mime,
                "status": r.get("status", "N/A"), "remark": r.get("remark", "")}
    except Exception as e:
        return {"name": name, "size_kb": size_kb, "mime": mime,
                "status": "N/A", "remark": f"Error analysing attachment: {e}"}


@router.get("/email-verify", response_class=HTMLResponse)
def email_verify_page(request: Request, quote_id: str = "", user=Depends(require_login)):
    resp = templates.TemplateResponse(request, "user_email_verify.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "results": None,
        "error": None,
        "email_subject": None,
        "email_from": None,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.post("/email-verify", response_class=HTMLResponse)
async def email_verify_post(
    request: Request,
    eml_file: UploadFile = File(...),
    quote_id: str = Form(""),
    user=Depends(require_login),
):
    def _err(msg: str):
        resp = templates.TemplateResponse(request, "user_email_verify.html", {
            "current_user": user,
            "theme": _resolve_theme(user),
            "quote_id": quote_id,
            "results": None,
            "error": msg,
            "email_subject": None,
            "email_from": None,
        })
        resp.headers.update(_NO_CACHE)
        return resp

    if not eml_file.filename.lower().endswith(".eml"):
        return _err("Please upload a valid .eml file (Outlook email format).")

    raw = await eml_file.read()
    if len(raw) > 50 * 1024 * 1024:  # 50 MB cap for email files
        return _err("Email file is too large. Maximum allowed size is 50 MB.")

    # Extract email metadata
    import email as _email_mod
    import email.policy as _policy
    msg = _email_mod.message_from_bytes(raw, policy=_policy.compat32)
    email_subject = msg.get("Subject", "(no subject)")
    email_from    = msg.get("From", "(unknown sender)")

    attachments = _parse_eml(raw)
    if not attachments:
        return _err("No attachments found in this email. Please check the .eml file.")

    # Build reference text from the linked quote
    reference_text = ""
    if quote_id:
        try:
            ref_record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            ref_record = db.find_by_quote_number(quote_id)
        if ref_record:
            fields = [
                ("Quote Number",    ref_record.get("quote_number", "")),
                ("Customer Name",   ref_record.get("customer_name", "")),
                ("Install Date",    ref_record.get("install_date", "")),
                ("System Price",    ref_record.get("system_price", "")),
                ("Total Price",     ref_record.get("total_price", "")),
                ("Deposit",         ref_record.get("deposit", "")),
                ("Balance",         ref_record.get("balance", "")),
                ("Payment Terms",   ref_record.get("payment_terms", "")),
                ("Billing Address", ref_record.get("billing_address", "")),
                ("Email",           ref_record.get("email", "")),
                ("Phone",           ref_record.get("phone", "")),
            ]
            reference_text = "\n".join(f"{k}: {v}" for k, v in fields if v)

    client = _get_claude()

    # Analyse all attachments concurrently
    results = await asyncio.gather(
        *[asyncio.to_thread(_match_attachment_with_claude, client, att, reference_text)
          for att in attachments]
    )

    resp = templates.TemplateResponse(request, "user_email_verify.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "results": list(results),
        "error": None,
        "email_subject": email_subject,
        "email_from": email_from,
    })
    resp.headers.update(_NO_CACHE)
    return resp


# ---------------------------------------------------------------------------
# QC check page
# ---------------------------------------------------------------------------

@router.get("/qc-check", response_class=HTMLResponse)
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


# ---------------------------------------------------------------------------
# Run checklist
# ---------------------------------------------------------------------------

@router.post("/run-checklist", response_class=HTMLResponse)
async def upload_zip(
    request: Request,
    zip_file: UploadFile = File(...),
    quote_id: str = Form(""),
    template_id: int = Form(...),
    user=Depends(require_login),
):
    uid = user["id"]
    if uid in _active_jobs:
        saved = tdb.list_templates()
        resp = templates.TemplateResponse(request, "user_qc.html", {
            "current_user": user,
            "theme": _resolve_theme(user),
            "quote_id": quote_id,
            "saved_templates": saved,
            "error": "A checklist run is already in progress for your account. Please wait for it to finish.",
        })
        resp.headers.update(_NO_CACHE)
        return resp

    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)

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

    _ZIP_ENTRY_MAX = 500 * 1024 * 1024
    zip_files: dict[str, bytes] = {}
    try:
        with zipmod.ZipFile(io.BytesIO(data)) as zf:
            total_uncompressed = sum(e.file_size for e in zf.infolist())
            if total_uncompressed > _ZIP_ENTRY_MAX:
                return _qc_error("ZIP contents are too large. Total uncompressed size must not exceed 500 MB.")
            for name in zf.namelist():
                basename = os.path.basename(name).strip()
                if basename:
                    zip_files[basename.lower()] = zf.read(name)
    except zipmod.BadZipFile:
        return _qc_error("Uploaded file is not a valid ZIP archive.")

    # Build reference context from the linked quote record
    reference_pdf_text = ""
    if quote_id:
        try:
            ref_record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            ref_record = db.find_by_quote_number(quote_id)
        if ref_record:
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
            reference_pdf_text = "\n".join(f"{k}: {v}" for k, v in fields if v)
            line_items = ref_record.get("line_items") or []
            if line_items:
                reference_pdf_text += "\n\nSystem Components:\n" + "\n".join(
                    f"- {li.get('item','')}: {li.get('specification','')}"
                    for li in line_items if li.get("item")
                )

    _claude_client = _get_claude()

    def _resolve_file(name: str):
        key = name.lower()
        if (fdata := zip_files.get(key)) is not None:
            return key, fdata

        key_nospace  = key.replace(" ", "")
        stem         = os.path.splitext(key)[0]
        stem_nospace = stem.replace(" ", "")

        best_name, best_data = None, None
        for zname, zbytes in zip_files.items():
            if zname.replace(" ", "") == key_nospace:
                return zname, zbytes
            zstem = os.path.splitext(zname)[0]
            if zstem == stem:
                best_name, best_data = zname, zbytes
            elif zstem.replace(" ", "") == stem_nospace and best_data is None:
                best_name, best_data = zname, zbytes

        return (best_name, best_data) if best_data is not None else (name, None)

    # Pre-render every PDF in the ZIP exactly once (cache avoids re-rendering per item)
    pdf_cache: dict[str, tuple[str, list[bytes]]] = {}
    for fname, fdata in zip_files.items():
        if _mime_of(fname, fdata) == "application/pdf":
            pdf_cache[fname] = _render_pdf(fname, fdata)

    def _analyse_item(item: dict) -> dict:
        ref         = (item.get("reference") or "").strip()
        prompt_text = (item.get("prompt") or "").strip()

        if not ref:
            return {"status": "N/A", "remark": "No reference file specified."}
        if not prompt_text:
            return {"status": "N/A", "remark": "No prompt defined for this item."}

        ref_names = [r.strip() for r in ref.split("+") if r.strip()]
        resolved  = [_resolve_file(r) for r in ref_names]

        missing = [name for name, d in resolved if d is None]
        found   = [(name, d) for name, d in resolved if d is not None]
        if not found:
            return {"status": "N/A", "remark": f"File not found in ZIP: {', '.join(missing)}"}
        resolved = found
        missing_note = (
            f" (Note: {', '.join(missing)} not found in ZIP, working with available files only.)"
            if missing else ""
        )

        ref_section = (
            f"\n\n--- MAIN REFERENCE PDF (Signed Agreement) ---\n{reference_pdf_text}"
            if reference_pdf_text else ""
        )
        context = f"Checklist item: {item['text']}\nRequirement: {prompt_text}{ref_section}{missing_note}"

        try:
            pdf_parts, image_parts, unsupported = [], [], []
            for fname, fdata in resolved:
                mime = _mime_of(fname, fdata)
                if mime == "application/pdf":
                    pdf_parts.append((fname, fdata))
                elif mime.startswith("image/"):
                    image_parts.append((fname, fdata, mime))
                else:
                    unsupported.append(fname)

            if unsupported:
                return {"status": "N/A", "remark": f"Unsupported file type: {', '.join(unsupported)}"}

            combined_pdf_text  = ""
            pdf_page_images: list[bytes] = []
            for fname, _ in pdf_parts:
                cached_text, cached_pages = pdf_cache.get(fname, ("", []))
                if cached_text.strip():
                    combined_pdf_text += f"\n\n--- {fname} ---\n{cached_text}"
                pdf_page_images.extend(cached_pages)
                if not cached_pages and not cached_text.strip():
                    if _fitz is None:
                        return {"status": "N/A", "remark": f"Could not render '{fname}' as image. Install PyMuPDF (pip install pymupdf)."}

            user_content = [{"type": "text", "text": context}]
            if combined_pdf_text.strip():
                user_content.append({"type": "text", "text": f"Document text (for reference):{combined_pdf_text[:8000]}"})
            for page_png in pdf_page_images:
                b64 = base64.standard_b64encode(page_png).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
            for fname, fdata, mime in image_parts:
                b64 = base64.standard_b64encode(fdata).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})

            if len(user_content) == 1:
                if combined_pdf_text.strip():
                    return _claude_check(_claude_client, f"{context}\n\nDocument content:{combined_pdf_text[:8000]}")
                return {"status": "N/A", "remark": "No readable content found in reference file(s)."}

            return _claude_check(_claude_client, user_content)

        except Exception as e:
            return {"status": "N/A", "remark": f"Error analysing file: {e}"}

    non_section = [(i, item) for i, item in enumerate(items) if not item.get("is_section")]

    _active_jobs.add(uid)
    try:
        results = await asyncio.gather(
            *[asyncio.to_thread(_analyse_item, item) for _, item in non_section]
        )
    finally:
        _active_jobs.discard(uid)

    checklist_rows = []
    filled: dict[int, dict] = {}
    ns_iter  = iter(zip(non_section, results))
    next_ns  = next(ns_iter, None)
    for i, item in enumerate(items):
        if item.get("is_section"):
            checklist_rows.append({**item, "status": "", "remark": ""})
        else:
            (_, _item), result = next_ns
            filled[item["position"]] = result
            checklist_rows.append({**item, "status": result["status"], "remark": result["remark"]})
            next_ns = next(ns_iter, None)

    headers = {
        "customer_label": tpl.get("customer_label"),
        "address_label":  tpl.get("address_label"),
        "job_label":      tpl.get("job_label"),
    }
    xlsx_blob = build_xlsx(items, headers, tpl.get("note_text", ""), filled=filled)

    xlsx_b64   = base64.b64encode(xlsx_blob).decode()
    dl_token   = _signer.dumps({"xlsx": xlsx_b64, "name": tpl["name"]})
    tpl_safe   = {k: v for k, v in tpl.items() if not isinstance(v, (bytes, bytearray))}
    yes_count  = sum(1 for r in checklist_rows if r.get("status") == "Yes")
    no_count   = sum(1 for r in checklist_rows if r.get("status") == "No")
    na_count   = sum(1 for r in checklist_rows if r.get("status") == "N/A")
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


# ---------------------------------------------------------------------------
# Checklist result
# ---------------------------------------------------------------------------

@router.get("/checklist-result", response_class=HTMLResponse)
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


@router.post("/checklist-save-edits")
async def checklist_save_edits(request: Request, _auth=Depends(require_login)):
    body       = await request.json()
    rows       = body.get("rows", [])
    tpl        = body.get("tpl", {})
    orig_token = body.get("dl_token", "")

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

    filled = {}
    for row in rows:
        if not row.get("is_section") and row.get("position") is not None:
            filled[row["position"]] = {
                "status": row.get("status", "N/A"),
                "remark": row.get("remark", ""),
            }

    xlsx_blob    = build_xlsx(rows, headers, note_text, filled=filled)
    xlsx_b64     = base64.b64encode(xlsx_blob).decode()
    new_dl_token = _signer.dumps({"xlsx": xlsx_b64, "name": tpl_name})
    return {"dl_token": new_dl_token}


@router.get("/checklist-download", response_class=Response)
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


@router.post("/checklist-confirm")
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
    tpl_name     = tpl_name[:200].replace("\n", "").replace("\r", "")
    zip_filename = zip_filename[:260].replace("\n", "").replace("\r", "")

    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)

    if dl_token and record:
        try:
            payload    = _signer.loads(dl_token, max_age=7200)
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

    install_date = (record or {}).get("install_date", "").strip() if record else ""
    cal_param = ""
    if install_date:
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


@router.post("/checklist-save-draft")
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
    tpl_name     = tpl_name[:200].replace("\n", "").replace("\r", "")
    zip_filename = zip_filename[:260].replace("\n", "").replace("\r", "")

    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)

    if dl_token and record:
        try:
            payload    = _signer.loads(dl_token, max_age=7200)
            xlsx_bytes = base64.b64decode(payload["xlsx"])
            db.add_qc_version(
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


# ---------------------------------------------------------------------------
# User history
# ---------------------------------------------------------------------------

@router.get("/user/history", response_class=HTMLResponse)
def user_history(request: Request, user=Depends(require_login)):
    history = db.get_qc_history_for_user(user["id"])
    resp = templates.TemplateResponse(request, "user_history.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "history": history,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.get("/user/qc-version/{version_id}", response_class=HTMLResponse)
def user_qc_version_revisit(request: Request, version_id: int, user=Depends(require_login)):
    with get_db() as conn:
        row = conn.execute(
            """SELECT qv.*, q.customer_name, q.quote_number, q.install_date
               FROM qc_versions qv JOIN quotes q ON q.id = qv.quote_id
               WHERE qv.id = ?""",
            (version_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
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

    tpl = {
        "id": 0, "name": v.get("template_name", ""),
        "customer_label": "", "address_label": "", "job_label": "", "note_text": "",
    }
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
