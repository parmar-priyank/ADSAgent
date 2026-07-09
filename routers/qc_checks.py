"""
routers/qc_checks.py — QC checklist execution and history routes.

Routes:
  GET  /qc-check
  POST /run-checklist
  GET  /checklist-result
  POST /checklist-analyse-file
  POST /checklist-save-edits
  GET  /checklist-download
  POST /checklist-confirm
  POST /checklist-save-draft
  GET  /user/history
  GET  /user/qc-version/{version_id}
  POST /user/qc-version/{version_id}/save
  POST /user/qc-version/{version_id}/add-email
"""
import asyncio
import base64
import io
import json
import mimetypes
import os
import zipfile as zipmod
from datetime import datetime

import pdfplumber

try:
    import fitz as _fitz   # PyMuPDF — optional; graceful fallback to text-only if missing
except ImportError:
    _fitz = None

_PDF_DPI = 100        # 100 DPI is enough for Claude vision and cuts payload to 1/4
_PDF_MAX_PAGES = 2    # send at most 2 pages per PDF to Claude — most checks need page 1 only

import db.quote_repo as db
import db.checklist_repo as tdb
from db.checklist_repo import store_pending_result, fetch_pending_result
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
    require_qc_access,
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

    Images are only rendered when text extraction yields nothing (scanned PDFs).
    Called once per unique PDF; the cache ensures each file is processed once.
    """
    text = ""
    try:
        with pdfplumber.open(io.BytesIO(fdata)) as pdf:
            pages_text = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages_text)
    except Exception:
        pass

    # Skip image rendering if we already have usable text — saves time and payload size
    if text.strip():
        return text, []

    pages: list[bytes] = []
    if _fitz is not None:
        try:
            doc = _fitz.open(stream=fdata, filetype="pdf")
            for page in doc[:_PDF_MAX_PAGES]:
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


def _analyse_single_file(client, item_text: str, prompt_text: str, reference_pdf_text: str,
                         fname: str, fdata: bytes) -> dict:
    """
    Analyse one uploaded file against a single checklist item's prompt —
    the per-row "Upload File" path in the result-page edit mode, so a user
    can fix one item's evidence without re-uploading the whole ZIP.
    Mirrors the relevant half of _analyse_item()'s body (run-checklist),
    minus the ZIP-lookup and multi-file/eml-rollup logic, since here there
    is always exactly one file already in hand.
    """
    ref_section = (
        f"\n\n--- MAIN REFERENCE PDF (Signed Agreement) ---\n{reference_pdf_text}"
        if reference_pdf_text else ""
    )
    context = f"Checklist item: {item_text}\nRequirement: {prompt_text}{ref_section}"

    try:
        if fname.lower().endswith(".eml"):
            atts = _parse_eml(fdata)
            if not atts:
                return {"status": "N/A", "remark": "No attachments found in the uploaded .eml file."}
            att_results = [_match_attachment_with_claude(client, att, reference_pdf_text) for att in atts]
            no_hits  = [r for r in att_results if r.get("status") == "No"]
            yes_hits = [r for r in att_results if r.get("status") == "Yes"]
            if no_hits:
                names = ", ".join(r["name"] for r in no_hits)
                return {"status": "No", "remark": f"Mismatch in attachment(s): {names}. " + no_hits[0].get("remark", "")}
            if yes_hits:
                names = ", ".join(r["name"] for r in yes_hits)
                return {"status": "Yes", "remark": f"Attachment(s) match agreement: {names}."}
            return {"status": "N/A", "remark": att_results[0].get("remark", "Could not verify email attachments.")}

        mime = _mime_of(fname, fdata)
        user_content = [{"type": "text", "text": context}]

        if mime == "application/pdf":
            text, pages = _render_pdf(fname, fdata)
            if text.strip():
                user_content.append({"type": "text", "text": f"Document text (for reference):{text[:8000]}"})
            for page_png in pages[:_PDF_MAX_PAGES]:
                b64 = base64.standard_b64encode(page_png).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
            if len(user_content) == 1:
                return {"status": "N/A", "remark": "No readable content found in the uploaded file."}
        elif mime.startswith("image/"):
            b64 = base64.standard_b64encode(fdata).decode()
            user_content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
        else:
            return {"status": "N/A", "remark": f"Unsupported file type: {fname}"}

        return _claude_check(client, user_content)

    except Exception as e:
        return {"status": "N/A", "remark": f"Error analysing file: {e}"}


def _build_reference_text(quote_id) -> str:
    """Build the same 'Quote Number: ...\\nCustomer Name: ...' reference
    block used when matching an uploaded file against a quote's signed
    agreement — shared by the "add one more email" endpoints below."""
    if not quote_id:
        return ""
    try:
        ref_record = db.get_quote(int(quote_id))
    except (ValueError, TypeError):
        ref_record = db.find_by_quote_number(quote_id)
    if not ref_record:
        return ""
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
    reference_text = "\n".join(f"{k}: {v}" for k, v in fields if v)
    line_items = ref_record.get("line_items") or []
    if line_items:
        reference_text += "\n\nSystem Components:\n" + "\n".join(
            f"- {li.get('item','')}: {li.get('specification','')}"
            for li in line_items if li.get("item")
        )
    return reference_text


def _derive_email_summary(results: list) -> tuple:
    """From a list of analyzed attachment results, rebuild the "N emails
    uploaded" header panel's data: (emails_meta, email_from, email_subject).
    emails_meta is set (multi-email view) when 2+ distinct emails are
    represented; otherwise email_from/email_subject carry the single one
    (matches the original single-email upload response shape)."""
    seen, seen_keys = [], set()
    for r in results:
        key = (r.get("source_address") or "", r.get("source_subject") or "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        seen.append({"from": r.get("source_email", ""), "subject": r.get("source_subject", "")})
    if len(seen) > 1:
        return seen, None, None
    if seen:
        return None, seen[0]["from"], seen[0]["subject"]
    return None, None, None


@router.get("/email-verify", response_class=HTMLResponse)
def email_verify_page(request: Request, quote_id: str = "", user=Depends(require_qc_access)):
    results = None
    emails_meta = None
    email_subject = None
    email_from = None

    draft_json = ""
    if quote_id:
        try:
            draft_json = db.get_draft_email_results(int(quote_id))
        except (ValueError, TypeError):
            draft_json = ""

    if draft_json.strip():
        try:
            parsed = json.loads(draft_json)
            if isinstance(parsed, list) and parsed:
                results = parsed
                emails_meta, email_from, email_subject = _derive_email_summary(results)
        except Exception:
            results = None

    resp = templates.TemplateResponse(request, "user_email_verify.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "results": results,
        "error": None,
        "email_subject": email_subject,
        "email_from": email_from,
        "emails": emails_meta,
        "from_draft": bool(results),
    })
    resp.headers.update(_NO_CACHE)
    return resp


def _decode_email_field(raw_val: str) -> str:
    """Decode RFC-2047 encoded header (e.g. =?Windows-1252?Q?...?=) to plain text."""
    from email.header import decode_header as _decode_header
    if not raw_val:
        return ""
    parts = _decode_header(raw_val)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded).strip()


def _extract_email_address(from_header: str) -> str:
    """Pull just the bare address out of a From header for dedup matching."""
    import email.utils as _eutils
    _, addr = _eutils.parseaddr(from_header or "")
    return addr.strip().lower()


@router.post("/email-verify", response_class=HTMLResponse)
async def email_verify_post(
    request: Request,
    eml_files: list[UploadFile] = File(..., alias="eml_file"),
    quote_id: str = Form(""),
    user=Depends(require_qc_access),
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
            "emails": None,
        })
        resp.headers.update(_NO_CACHE)
        return resp

    import email as _email_mod
    import email.policy as _policy

    # Build reference text from the linked quote (shared across all emails)
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

    emails_meta = []   # [{subject, from, address}] — one per uploaded .eml
    all_attachments = []  # flattened, each tagged with its source email index
    for idx, eml_file in enumerate(eml_files):
        if not eml_file.filename.lower().endswith(".eml"):
            return _err(f"'{eml_file.filename}' is not a valid .eml file (Outlook email format).")

        raw = await eml_file.read()
        if len(raw) > 50 * 1024 * 1024:  # 50 MB cap per email file
            return _err(f"'{eml_file.filename}' is too large. Maximum allowed size is 50 MB per email.")

        msg = _email_mod.message_from_bytes(raw, policy=_policy.compat32)
        subject = _decode_email_field(msg.get("Subject", "")) or "(no subject)"
        from_hdr = _decode_email_field(msg.get("From", "")) or "(unknown sender)"
        emails_meta.append({
            "subject": subject,
            "from": from_hdr,
            "address": _extract_email_address(from_hdr),
        })

        attachments = _parse_eml(raw)
        for att in attachments:
            all_attachments.append((idx, att))

    if not all_attachments:
        noun = "email" if len(eml_files) == 1 else "emails"
        return _err(f"No attachments found in the uploaded {noun}. Please check the .eml file(s).")

    # Analyse all attachments (across all emails) concurrently
    analysed = await asyncio.gather(
        *[asyncio.to_thread(_match_attachment_with_claude, client, att, reference_text)
          for _, att in all_attachments]
    )

    results = []
    for (idx, _att), r in zip(all_attachments, analysed):
        results.append({
            **r,
            "ai_status": r.get("status"),
            "source_email": emails_meta[idx]["from"],
            "source_subject": emails_meta[idx]["subject"],
            "source_address": emails_meta[idx]["address"],
        })

    # Dedup exact duplicates: same sender address AND same attachment name.
    # Different attachments from the same sender (e.g. contract vs warranty
    # doc in separate emails) are kept — only true re-uploads collapse into
    # a single unified answer.
    seen_keys: set = set()
    deduped = []
    for r in results:
        key = ((r.get("source_address") or ""), (r.get("name") or "").strip().lower())
        if key[0] and key[1] and key in seen_keys:
            continue
        if key[0] and key[1]:
            seen_keys.add(key)
        deduped.append(r)
    results = deduped

    # Merge with any previously-saved draft for this quote, so uploading one
    # more .eml doesn't discard emails already analysed in an earlier visit —
    # new results replace old ones with the same (sender, attachment name),
    # everything else from the saved draft is kept alongside the new results.
    if quote_id:
        try:
            draft_json = db.get_draft_email_results(int(quote_id))
        except (ValueError, TypeError):
            draft_json = ""
        if draft_json.strip():
            try:
                saved = json.loads(draft_json)
                if isinstance(saved, list):
                    new_keys = {
                        ((r.get("source_address") or ""), (r.get("name") or "").strip().lower())
                        for r in results
                    }
                    kept_old = [
                        r for r in saved
                        if ((r.get("source_address") or ""), (r.get("name") or "").strip().lower())
                        not in new_keys
                    ]
                    results = kept_old + results
            except Exception:
                pass

    emails_meta, email_from, email_subject = _derive_email_summary(results)

    resp = templates.TemplateResponse(request, "user_email_verify.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "results": list(results),
        "error": None,
        "email_subject": email_subject,
        "email_from": email_from,
        "emails": emails_meta,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.post("/email-verify/save-draft")
def email_verify_save_draft(
    quote_id: str = Form(""),
    email_results_json: str = Form(""),
    user=Depends(require_qc_access),
):
    """Persist Step 2's analyzed attachment results against the quote, so the
    user doesn't have to re-upload the same .eml file(s) if they come back
    to this quote later — independent of Continue/proceeding to Step 3."""
    if not quote_id:
        raise HTTPException(400, "Missing quote_id.")
    try:
        qid = int(quote_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid quote_id.")

    try:
        parsed = json.loads(email_results_json) if email_results_json.strip() else []
        if not isinstance(parsed, list):
            parsed = []
    except Exception:
        parsed = []

    db.save_draft_email_results(qid, json.dumps(parsed))
    return {"ok": True}


# ---------------------------------------------------------------------------
# QC check page
# ---------------------------------------------------------------------------

@router.get("/qc-check", response_class=HTMLResponse)
def qc_check_page(request: Request, quote_id: str = "", user=Depends(require_qc_access)):
    saved = tdb.list_templates()
    resp = templates.TemplateResponse(request, "user_qc.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "saved_templates": saved,
        "email_results_json": "",
        "error": None,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.post("/qc-check-with-email", response_class=HTMLResponse)
def qc_check_page_with_email(
    request: Request,
    quote_id: str = Form(""),
    email_results_json: str = Form(""),
    user=Depends(require_qc_access),
):
    """Arrive from Step 2 (email verify) carrying the edited email results."""
    saved = tdb.list_templates()
    resp = templates.TemplateResponse(request, "user_qc.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "quote_id": quote_id,
        "saved_templates": saved,
        "email_results_json": email_results_json,
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
    email_results_json: str = Form(""),
    user=Depends(require_qc_access),
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
            "email_results_json": email_results_json,
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

    def _expected_kind(ref_name: str) -> str:
        """Guess what kind of file a reference name implies, from its extension."""
        ext = os.path.splitext(ref_name.lower())[1]
        if ext == ".eml":
            return "eml"
        if ext == ".pdf":
            return "pdf"
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            return "image"
        return "other"

    def _actual_kind(fname: str, fdata: bytes) -> str:
        if fname.lower().endswith(".eml"):
            return "eml"
        mime = _mime_of(fname, fdata)
        if mime == "application/pdf":
            return "pdf"
        if mime.startswith("image/"):
            return "image"
        return "other"

    # ── Resolve every checklist item's reference file(s) up front, sequentially ──
    # (must happen before the concurrent AI analysis below, so two items can't
    # both claim the same fallback-matched file in a race).
    #
    # Pass 1: exact-name matching (same as before) — claims files as it goes so
    # pass 2 knows what's already spoken for.
    claimed: set[str] = set()
    item_resolved: dict[int, list[tuple]] = {}

    for i, item in enumerate(items):
        if item.get("is_section"):
            continue
        ref = (item.get("reference") or "").strip()
        ref_names = [r.strip() for r in ref.split("+") if r.strip()]
        resolved = []
        for rname in ref_names:
            zname, zdata = _resolve_file(rname)
            if zdata is not None:
                claimed.add(zname)
            resolved.append([rname, zname, zdata])
        item_resolved[i] = resolved

    # Pass 2: for references still unmatched, fall back to the single unclaimed
    # file left in the ZIP of the same kind (image/pdf/eml) the reference name
    # implies — only when that leaves exactly one candidate, so it's unambiguous.
    for i, resolved in item_resolved.items():
        for entry in resolved:
            rname, zname, zdata = entry
            if zdata is not None:
                continue
            kind = _expected_kind(rname)
            if kind == "other":
                continue
            candidates = [
                (fn, fd) for fn, fd in zip_files.items()
                if fn not in claimed and _actual_kind(fn, fd) == kind
            ]
            if len(candidates) == 1:
                fn, fd = candidates[0]
                claimed.add(fn)
                entry[1], entry[2] = fn, fd

    # Pre-render every PDF in the ZIP exactly once (cache avoids re-rendering per item)
    pdf_cache: dict[str, tuple[str, list[bytes]]] = {}
    for fname, fdata in zip_files.items():
        if _mime_of(fname, fdata) == "application/pdf":
            pdf_cache[fname] = _render_pdf(fname, fdata)

    def _analyse_eml_item(eml_parts: list, context: str) -> dict:
        """
        Roll up a checklist item whose reference resolves to one or more .eml
        files inside the ZIP into a single Yes/No/N/A verdict, by extracting
        each email's attachments and matching them against the reference
        text — same logic the standalone "Verify Email" step (Step 2) uses.
        """
        all_atts = []
        for fname, fdata in eml_parts:
            atts = _parse_eml(fdata)
            if not atts:
                continue
            all_atts.extend(atts)

        if not all_atts:
            return {"status": "N/A", "remark": "No attachments found in the referenced .eml file(s)."}

        att_results = [
            _match_attachment_with_claude(_claude_client, att, reference_pdf_text)
            for att in all_atts
        ]

        no_hits  = [r for r in att_results if r.get("status") == "No"]
        yes_hits = [r for r in att_results if r.get("status") == "Yes"]
        if no_hits:
            names = ", ".join(r["name"] for r in no_hits)
            return {"status": "No", "remark": f"Mismatch in attachment(s): {names}. " + no_hits[0].get("remark", "")}
        if yes_hits:
            names = ", ".join(r["name"] for r in yes_hits)
            return {"status": "Yes", "remark": f"Attachment(s) match agreement: {names}."}
        return {"status": "N/A", "remark": att_results[0].get("remark", "Could not verify email attachments.")}

    def _analyse_item(i: int, item: dict) -> dict:
        ref         = (item.get("reference") or "").strip()
        prompt_text = (item.get("prompt") or "").strip()

        if not ref:
            return {"status": "N/A", "remark": "No reference file specified."}
        if not prompt_text:
            return {"status": "N/A", "remark": "No prompt defined for this item."}

        # resolved entries are [ref_name, matched_zip_name_or_None, data_or_None],
        # already computed up front in the sequential resolve-and-claim pass above
        # (both the exact-name match and the same-file-type fallback).
        resolved_entries = item_resolved.get(i, [])
        missing = [rname for rname, zname, zdata in resolved_entries if zdata is None]
        found   = [(zname, zdata) for rname, zname, zdata in resolved_entries if zdata is not None]
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
            eml_parts, pdf_parts, image_parts, unsupported = [], [], [], []
            for fname, fdata in resolved:
                if fname.lower().endswith(".eml"):
                    eml_parts.append((fname, fdata))
                    continue
                mime = _mime_of(fname, fdata)
                if mime == "application/pdf":
                    pdf_parts.append((fname, fdata))
                elif mime.startswith("image/"):
                    image_parts.append((fname, fdata, mime))
                else:
                    unsupported.append(fname)

            if eml_parts:
                return _analyse_eml_item(eml_parts, context)

            if unsupported:
                return {"status": "N/A", "remark": f"Unsupported file type: {', '.join(unsupported)}"}

            combined_pdf_text  = ""
            pdf_page_images: list[bytes] = []
            for fname, _ in pdf_parts:
                cached_text, cached_pages = pdf_cache.get(fname, ("", []))
                if cached_text.strip():
                    combined_pdf_text += f"\n\n--- {fname} ---\n{cached_text}"
                pdf_page_images.extend(cached_pages[:_PDF_MAX_PAGES])
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
            *[asyncio.to_thread(_analyse_item, i, item) for i, item in non_section]
        )
    finally:
        _active_jobs.discard(uid)

    checklist_rows = []
    filled: dict[int, dict] = {}
    ns_iter  = iter(zip(non_section, results))
    next_ns  = next(ns_iter, None)
    for i, item in enumerate(items):
        if item.get("is_section"):
            checklist_rows.append({**item, "status": "", "remark": "", "ai_status": ""})
        else:
            (_, _item), result = next_ns
            filled[item["position"]] = result
            checklist_rows.append({**item, "status": result["status"], "remark": result["remark"],
                                    "ai_status": result["status"]})
            next_ns = next(ns_iter, None)

    # Parse email verify results passed from Step 2 (may be empty if step was skipped)
    email_results = []
    if email_results_json.strip():
        try:
            parsed = json.loads(email_results_json)
            if isinstance(parsed, list):
                email_results = [
                    r for r in parsed
                    if isinstance(r, dict) and r.get("name")
                ]
        except Exception:
            pass

    # Dedup exact duplicates: same sender address AND same attachment name
    # (same guard as the Step 2 upload handler) — keeps a single unified
    # answer per attachment even if results were re-submitted with repeats.
    if email_results:
        _seen_keys: set = set()
        _deduped = []
        for r in email_results:
            _key = (
                (r.get("source_address") or "").strip().lower(),
                (r.get("name") or "").strip().lower(),
            )
            if _key[0] and _key[1] and _key in _seen_keys:
                continue
            if _key[0] and _key[1]:
                _seen_keys.add(_key)
            _deduped.append(r)
        email_results = _deduped

    headers = {
        "customer_label": tpl.get("customer_label"),
        "address_label":  tpl.get("address_label"),
        "job_label":      tpl.get("job_label"),
    }
    xlsx_blob = build_xlsx(items, headers, tpl.get("note_text", ""), filled=filled,
                           email_results=email_results)

    # Save Excel to a temp file on disk instead of embedding it in the DB row.
    # The signed token carries only the file path — a few bytes instead of megabytes.
    import tempfile
    xlsx_dir = os.path.join(os.path.dirname(__file__), "..", "tmp_xlsx")
    os.makedirs(xlsx_dir, exist_ok=True)
    xlsx_tmp = tempfile.NamedTemporaryFile(
        suffix=".xlsx", dir=xlsx_dir, delete=False
    )
    try:
        xlsx_tmp.write(xlsx_blob)
    finally:
        xlsx_tmp.close()

    dl_token   = _signer.dumps({"xlsx_path": xlsx_tmp.name, "name": tpl["name"]})
    tpl_safe   = {k: v for k, v in tpl.items() if not isinstance(v, (bytes, bytearray))}
    yes_count  = sum(1 for r in checklist_rows if r.get("status") == "Yes")
    no_count   = sum(1 for r in checklist_rows if r.get("status") == "No")
    na_count   = sum(1 for r in checklist_rows if r.get("status") == "N/A")
    result_token = store_pending_result({
        "tpl": tpl_safe,
        "rows": checklist_rows,
        "quote_id": quote_id,
        "dl_token": dl_token,
        "zip_filename": zip_file.filename or "",
        "yes_count": yes_count,
        "no_count": no_count,
        "na_count": na_count,
        "email_results": email_results,
    })
    return RedirectResponse(url=f"/checklist-result?token={result_token}", status_code=303)


# ---------------------------------------------------------------------------
# Checklist result
# ---------------------------------------------------------------------------

@router.get("/checklist-result", response_class=HTMLResponse)
def checklist_result(request: Request, token: str, user=Depends(require_qc_access)):
    payload = fetch_pending_result(token)
    if not payload:
        raise HTTPException(400, "Result link has expired. Please re-run the checklist.")

    preferred_install_date = ""
    quote_id = payload["quote_id"]
    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)
        if record:
            preferred_install_date = record.get("preferred_install_date") or ""

    resp = templates.TemplateResponse(request, "user_result.html", {
        "current_user": user,
        "tpl": payload["tpl"],
        "checklist_rows": payload["rows"],
        "dl_token": payload["dl_token"],
        "quote_id": quote_id,
        "zip_filename": payload.get("zip_filename", ""),
        "yes_count": payload.get("yes_count", 0),
        "no_count": payload.get("no_count", 0),
        "na_count": payload.get("na_count", 0),
        "email_results": payload.get("email_results", []),
        "preferred_install_date": preferred_install_date,
        "today": datetime.now().strftime("%Y-%m-%d"),
        "theme": _resolve_theme(user),
        "record": record,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.post("/checklist-analyse-file")
async def checklist_analyse_file(
    file: UploadFile = File(...),
    item_text: str = Form(""),
    prompt: str = Form(""),
    quote_id: str = Form(""),
    user=Depends(require_qc_access),
):
    """
    Analyse a single uploaded file against one checklist item's prompt —
    lets the user fix one row's evidence in edit mode without re-uploading
    the whole ZIP (which would re-run AI analysis, and cost, on every item).
    Independent of the manual Yes/No/N/A dropdown — neither depends on the other.
    """
    fdata = await file.read()
    if len(fdata) > MAX_UPLOAD_BYTES:
        return {"status": "N/A", "remark": "File is too large."}

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

    client = _get_claude()
    result = _analyse_single_file(
        client, item_text, prompt, reference_pdf_text, file.filename or "upload", fdata
    )
    return result


@router.post("/checklist-save-edits")
async def checklist_save_edits(request: Request, _auth=Depends(require_qc_access)):
    body       = await request.json()
    rows       = body.get("rows", [])
    tpl        = body.get("tpl", {})
    orig_token = body.get("dl_token", "")
    email_results = body.get("email_results", [])

    tpl_name = tpl.get("name", "checklist")
    try:
        orig_payload = _signer.loads(orig_token, max_age=7200)
        tpl_name = orig_payload.get("name", tpl_name)
        # Preserve email_results from original token if not supplied by client
        if not email_results:
            email_results = orig_payload.get("email_results", [])
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
                "ai_status": row.get("ai_status", ""),
            }

    xlsx_blob = build_xlsx(rows, headers, note_text, filled=filled, email_results=email_results)

    import tempfile
    xlsx_dir = os.path.join(os.path.dirname(__file__), "..", "tmp_xlsx")
    os.makedirs(xlsx_dir, exist_ok=True)
    xlsx_tmp = tempfile.NamedTemporaryFile(
        suffix=".xlsx", dir=xlsx_dir, delete=False
    )
    try:
        xlsx_tmp.write(xlsx_blob)
    finally:
        xlsx_tmp.close()

    new_dl_token = _signer.dumps({"xlsx_path": xlsx_tmp.name, "name": tpl_name})
    return {"dl_token": new_dl_token}


@router.get("/checklist-download", response_class=Response)
def checklist_download(token: str, _auth=Depends(require_qc_access)):
    try:
        payload = _signer.loads(token, max_age=7200)
    except BadSignature:
        raise HTTPException(400, "Invalid or expired download token.")

    # Support both old base64 tokens and new file-path tokens
    if "xlsx_path" in payload:
        xlsx_path = payload["xlsx_path"]
        if not os.path.isfile(xlsx_path):
            raise HTTPException(410, "Download file has expired. Please re-run the checklist.")
        with open(xlsx_path, "rb") as f:
            xlsx_blob = f.read()
    else:
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
    email_results_json: str = Form(""),
    preferred_install_date: str = Form(""),
    version_id: str = Form(""),
    user=Depends(require_qc_access),
):
    tpl_name     = tpl_name[:200].replace("\n", "").replace("\r", "")
    zip_filename = zip_filename[:260].replace("\n", "").replace("\r", "")

    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)

    if record and preferred_install_date.strip() != (record.get("preferred_install_date") or ""):
        db.update_preferred_install_date(record["id"], preferred_install_date.strip())
        record["preferred_install_date"] = preferred_install_date.strip()

    if dl_token and record:
        try:
            payload = _signer.loads(dl_token, max_age=7200)
            if "xlsx_path" in payload:
                xlsx_path = payload["xlsx_path"]
                with open(xlsx_path, "rb") as f:
                    xlsx_bytes = f.read()
                # Clean up the temp file now that it's been saved to DB
                try:
                    os.unlink(xlsx_path)
                except OSError:
                    pass
            else:
                xlsx_bytes = base64.b64decode(payload["xlsx"])
            db.save_qc_excel(record["id"], xlsx_bytes)
            if version_id.strip():
                # Editing an existing (revisited) version — update it in place
                # instead of creating a new one.
                db.update_qc_version(
                    version_id=int(version_id),
                    xlsx_bytes=xlsx_bytes,
                    rows_json=rows_json,
                    yes_count=yes_count,
                    no_count=no_count,
                    na_count=na_count,
                    confirm=True,
                    confirmed_by_user_id=user["id"],
                    email_results_json=email_results_json,
                )
            else:
                db.add_qc_version(
                    quote_id=record["id"],
                    xlsx_bytes=xlsx_bytes,
                    template_name=tpl_name,
                    zip_filename=zip_filename,
                    yes_count=yes_count,
                    no_count=no_count,
                    na_count=na_count,
                    rows_json=rows_json,
                    email_results_json=email_results_json,
                    confirmed_by_user_id=user["id"],
                    saved_by_user_id=user["id"],
                    status="confirmed",
                )
        except Exception:
            pass

    preferred = (record or {}).get("preferred_install_date", "").strip() if record else ""
    install_date = preferred or ((record or {}).get("install_date", "").strip() if record else "")
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
    email_results_json: str = Form(""),
    preferred_install_date: str = Form(""),
    version_id: str = Form(""),
    user=Depends(require_qc_access),
):
    tpl_name     = tpl_name[:200].replace("\n", "").replace("\r", "")
    zip_filename = zip_filename[:260].replace("\n", "").replace("\r", "")

    record = None
    if quote_id:
        try:
            record = db.get_quote(int(quote_id))
        except (ValueError, TypeError):
            record = db.find_by_quote_number(quote_id)

    if record and preferred_install_date.strip() != (record.get("preferred_install_date") or ""):
        db.update_preferred_install_date(record["id"], preferred_install_date.strip())

    if dl_token and record:
        try:
            payload = _signer.loads(dl_token, max_age=7200)
            if "xlsx_path" in payload:
                xlsx_path = payload["xlsx_path"]
                with open(xlsx_path, "rb") as f:
                    xlsx_bytes = f.read()
                try:
                    os.unlink(xlsx_path)
                except OSError:
                    pass
            else:
                xlsx_bytes = base64.b64decode(payload["xlsx"])
            if version_id.strip():
                # Editing an existing (revisited) draft — update it in place
                # instead of creating a new one.
                db.update_qc_version(
                    version_id=int(version_id),
                    xlsx_bytes=xlsx_bytes,
                    rows_json=rows_json,
                    yes_count=yes_count,
                    no_count=no_count,
                    na_count=na_count,
                    confirm=False,
                    email_results_json=email_results_json,
                )
            else:
                db.add_qc_version(
                    quote_id=record["id"],
                    xlsx_bytes=xlsx_bytes,
                    template_name=tpl_name,
                    zip_filename=zip_filename,
                    yes_count=yes_count,
                    no_count=no_count,
                    na_count=na_count,
                    rows_json=rows_json,
                    email_results_json=email_results_json,
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
def user_history(request: Request, user=Depends(require_qc_access)):
    history = db.get_qc_history_for_user(user["id"])
    resp = templates.TemplateResponse(request, "user_history.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "history": history,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.get("/user/qc-version/{version_id}", response_class=HTMLResponse)
def user_qc_version_revisit(request: Request, version_id: int, user=Depends(require_qc_access)):
    with get_db() as conn:
        row = conn.execute(
            """SELECT qv.*, q.customer_name, q.quote_number, q.install_date,
                      q.preferred_install_date
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
    # For revisiting saved versions, write the blob back to a temp file so the
    # dl_token stays small (same pattern as the live checklist flow).
    dl_token = ""
    if v.get("excel_blob"):
        import tempfile
        xlsx_dir = os.path.join(os.path.dirname(__file__), "..", "tmp_xlsx")
        os.makedirs(xlsx_dir, exist_ok=True)
        xlsx_tmp = tempfile.NamedTemporaryFile(
            suffix=".xlsx", dir=xlsx_dir, delete=False
        )
        try:
            xlsx_tmp.write(bytes(v["excel_blob"]))
        finally:
            xlsx_tmp.close()
        dl_token = _signer.dumps({"xlsx_path": xlsx_tmp.name, "name": tpl["name"]})

    yes_count = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "Yes")
    no_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "No")
    na_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "N/A")

    # Load saved email results from DB
    saved_email_results = []
    raw_email_json = v.get("email_results_json") or ""
    if raw_email_json.strip():
        try:
            parsed_email = json.loads(raw_email_json)
            if isinstance(parsed_email, list):
                saved_email_results = parsed_email
        except Exception:
            pass

    # Full PDF-extracted quote record for the Signed Agreement tile/modal —
    # same data admin_qc_version_view fetches for its own equivalent block.
    record = db.get_quote(v["quote_id"])

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
        "email_results": saved_email_results,
        "revisit_version": v,
        "preferred_install_date": v.get("preferred_install_date") or "",
        "today": datetime.now().strftime("%Y-%m-%d"),
        "record": record,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.post("/user/qc-version/{version_id}/save")
async def user_qc_version_save(request: Request, version_id: int, user=Depends(require_qc_access)):
    """Let the user who saved/confirmed a QC version edit it in place —
    same action the admin panel already offers on any version. Overwrites
    the existing version row rather than creating a new one."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM qc_versions WHERE id = ?", (version_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
    if (user.get("role") != "admin"
            and v.get("saved_by_user_id") != user["id"]
            and v.get("confirmed_by_user_id") != user["id"]):
        raise HTTPException(403, "Access denied.")

    body = await request.json()
    rows = body.get("rows", [])
    preferred_install_date = (body.get("preferred_install_date") or "").strip()
    # action is optional — omitted means "keep whatever status this version
    # already had" (the original revisit-save behavior); "draft" or "confirm"
    # explicitly sets the status, same choice the admin panel already offers.
    action = body.get("action")
    if action not in ("draft", "confirm"):
        action = "confirm" if v.get("status") == "confirmed" else "draft"
    # email_results is optional in the payload — omitted (key absent) means
    # "the email table wasn't part of this edit, leave it as stored"; an
    # empty list is a legitimate value (all rows cleared) and must overwrite.
    has_email_edit = "email_results" in body
    email_results = body.get("email_results", [])

    try:
        filled = {}
        for r in rows:
            if not r.get("is_section") and r.get("position") is not None:
                filled[r["position"]] = {
                    "status": r.get("status", "N/A"),
                    "remark": r.get("remark", ""),
                    "ai_status": r.get("ai_status", ""),
                }

        xlsx_blob = build_xlsx(
            rows, filled=filled,
            email_results=email_results if has_email_edit else json.loads(v.get("email_results_json") or "[]"),
        )
        yes_count = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "Yes")
        no_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "No")
        na_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "N/A")

        confirm = action == "confirm"
        db.update_qc_version(
            version_id=version_id,
            xlsx_bytes=xlsx_blob,
            rows_json=json.dumps(rows),
            yes_count=yes_count,
            no_count=no_count,
            na_count=na_count,
            confirm=confirm,
            confirmed_by_user_id=user["id"] if confirm else None,
            email_results_json=json.dumps(email_results) if has_email_edit else None,
        )

        quote_id = v["quote_id"]
        record = db.get_quote(quote_id)
        if record and preferred_install_date != (record.get("preferred_install_date") or ""):
            db.update_preferred_install_date(quote_id, preferred_install_date)
    except Exception as e:
        raise HTTPException(500, f"Failed to save: {e}")

    return {"ok": True, "yes_count": yes_count, "no_count": no_count, "na_count": na_count}


@router.post("/user/qc-version/{version_id}/add-email")
async def user_qc_version_add_email(
    version_id: int,
    eml_file: UploadFile = File(...),
    user=Depends(require_qc_access),
):
    """Analyse one more .eml file against the signed agreement and append its
    attachment results to this version's saved email results immediately —
    independent of the Modify/edit-mode flow, no separate Save step."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM qc_versions WHERE id = ?", (version_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
    if (user.get("role") != "admin"
            and v.get("saved_by_user_id") != user["id"]
            and v.get("confirmed_by_user_id") != user["id"]):
        raise HTTPException(403, "Access denied.")

    if not eml_file.filename.lower().endswith(".eml"):
        raise HTTPException(400, f"'{eml_file.filename}' is not a valid .eml file (Outlook email format).")

    raw = await eml_file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, f"'{eml_file.filename}' is too large. Maximum allowed size is 50 MB.")

    import email as _email_mod
    import email.policy as _policy

    msg = _email_mod.message_from_bytes(raw, policy=_policy.compat32)
    subject = _decode_email_field(msg.get("Subject", "")) or "(no subject)"
    from_hdr = _decode_email_field(msg.get("From", "")) or "(unknown sender)"
    address = _extract_email_address(from_hdr)

    attachments = _parse_eml(raw)
    if not attachments:
        raise HTTPException(400, f"No attachments found in '{eml_file.filename}'. Please check the .eml file.")

    reference_text = _build_reference_text(v["quote_id"])
    client = _get_claude()

    analysed = await asyncio.gather(
        *[asyncio.to_thread(_match_attachment_with_claude, client, att, reference_text)
          for att in attachments]
    )

    new_results = [
        {
            **r,
            "ai_status": r.get("status"),
            "source_email": from_hdr,
            "source_subject": subject,
            "source_address": address,
        }
        for r in analysed
    ]

    try:
        existing_results = json.loads(v.get("email_results_json") or "[]") or []
    except Exception:
        existing_results = []

    new_keys = {
        ((r.get("source_address") or ""), (r.get("name") or "").strip().lower())
        for r in new_results
    }
    kept_old = [
        r for r in existing_results
        if ((r.get("source_address") or ""), (r.get("name") or "").strip().lower())
        not in new_keys
    ]
    merged_results = kept_old + new_results

    rows = json.loads(v["rows_json"]) if v.get("rows_json") else []
    filled = {}
    for r in rows:
        if not r.get("is_section") and r.get("position") is not None:
            filled[r["position"]] = {
                "status": r.get("status", "N/A"),
                "remark": r.get("remark", ""),
                "ai_status": r.get("ai_status", ""),
            }
    xlsx_blob = build_xlsx(rows, filled=filled, email_results=merged_results)

    was_confirmed = v.get("status") == "confirmed"
    db.update_qc_version(
        version_id=version_id,
        xlsx_bytes=xlsx_blob,
        rows_json=v["rows_json"] or json.dumps(rows),
        yes_count=v["yes_count"],
        no_count=v["no_count"],
        na_count=v["na_count"],
        confirm=was_confirmed,
        confirmed_by_user_id=user["id"] if was_confirmed else None,
        email_results_json=json.dumps(merged_results),
    )
    return {"ok": True, "email_results": merged_results}
