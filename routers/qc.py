"""
routers/qc.py — QC checklist execution and history routes.

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

import records as db
import checklists as tdb
import excel as cx
from database import get_db

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature

from core import (
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

QC_SYSTEM = (
    "You are a QC document checker. "
    "Determine if the requirement is met based on the provided content. "
    'Reply with JSON only: {"status": "Yes" or "No", "remark": "one sentence explanation"}'
)


def _render_pdf(fname: str, fdata: bytes) -> tuple[str, list[bytes]]:
    """Return (extracted_text, [page_png_bytes]) for a PDF.

    Called once per unique PDF file before the parallel checklist loop so every
    checklist item that references the same file shares the rendered pages
    instead of re-rendering from scratch.
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
    """Detect MIME type from filename first, then magic-byte sniffing."""
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
    """Send content to Claude and parse the Yes/No/N/A JSON response."""
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
    # Load checklist items for the chosen template
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)

    # Extract ZIP into a flat dict: lowercase filename → bytes
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

    _ZIP_ENTRY_MAX = 500 * 1024 * 1024   # 500 MB max total uncompressed content
    zip_files: dict[str, bytes] = {}
    try:
        with zipmod.ZipFile(io.BytesIO(data)) as zf:
            total_uncompressed = sum(e.file_size for e in zf.infolist())
            if total_uncompressed > _ZIP_ENTRY_MAX:
                return _qc_error("ZIP contents are too large. Total uncompressed size must not exceed 500 MB.")
            for name in zf.namelist():
                # Use os.path.basename to handle both / and \ separators safely
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

    def _resolve_file(name: str):
        """Return (actual_zip_filename, bytes) for a reference name, or (name, None) if not found.

        Matching order:
        1. Exact name (case-insensitive)
        2. Exact name ignoring spaces
        3. Same stem, any extension (e.g. template says Deposit.jpg, ZIP has Deposit.png)
        4. Same stem ignoring spaces, any extension
        """
        key = name.lower()
        if (data := zip_files.get(key)) is not None:
            return key, data

        key_nospace = key.replace(" ", "")
        stem = os.path.splitext(key)[0]
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

    # Pre-render every PDF in the ZIP exactly once.
    # Each entry: fname -> (extracted_text, [page_png_bytes])
    # _analyse_item reads from this cache so the same PDF is never rendered twice
    # even if 10 checklist items all reference it.
    pdf_cache: dict[str, tuple[str, list[bytes]]] = {}
    for fname, fdata in zip_files.items():
        if _mime_of(fname, fdata) == "application/pdf":
            pdf_cache[fname] = _render_pdf(fname, fdata)

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
        resolved = found
        missing_note = f" (Note: {', '.join(missing)} not found in ZIP, working with available files only.)" if missing else ""

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

            # Pull pre-rendered PDF data from cache — no re-rendering per item.
            # Text extraction alone misses handwritten signatures, stamps, and
            # checkbox ticks; the cached PNG pages cover those visual elements.
            combined_pdf_text = ""
            pdf_page_images: list[bytes] = []
            for fname, _ in pdf_parts:
                cached_text, cached_pages = pdf_cache.get(fname, ("", []))
                if cached_text.strip():
                    combined_pdf_text += f"\n\n--- {fname} ---\n{cached_text}"
                pdf_page_images.extend(cached_pages)
                if not cached_pages and not cached_text.strip():
                    if _fitz is None:
                        return {"status": "N/A", "remark": f"Could not render '{fname}' as image. Install PyMuPDF (pip install pymupdf)."}

            # Build vision message: text context first, then all images
            user_content = [{"type": "text", "text": context}]
            if combined_pdf_text.strip():
                user_content.append({"type": "text", "text": f"Document text (for reference):{combined_pdf_text[:8000]}"})
            for page_png in pdf_page_images:
                b64 = base64.standard_b64encode(page_png).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
            for fname, fdata, mime in image_parts:
                b64 = base64.standard_b64encode(fdata).decode()
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})

            # If nothing visual to send, fall back to text-only
            if len(user_content) == 1:
                if combined_pdf_text.strip():
                    return _claude_check(_claude_client, f"{context}\n\nDocument content:{combined_pdf_text[:8000]}")
                return {"status": "N/A", "remark": "No readable content found in reference file(s)."}

            return _claude_check(_claude_client, user_content)

        except Exception as e:
            return {"status": "N/A", "remark": f"Error analysing file: {e}"}

    # Run all checklist items concurrently.
    non_section = [(i, item) for i, item in enumerate(items) if not item.get("is_section")]

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
    """
    Called when the user clicks 'Confirm & Add to Calendar' on the result page.
    Saves the QC Excel as a new version and updates the latest snapshot on the quote.
    """
    # Sanitise string fields from form: strip control chars and cap length.
    tpl_name     = tpl_name[:200].replace("\n", "").replace("\r", "")
    zip_filename = zip_filename[:260].replace("\n", "").replace("\r", "")

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
    """Save QC results as a draft (no calendar entry). User can revisit later."""
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
            payload = _signer.loads(dl_token, max_age=7200)
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
    """Let a user revisit a saved/confirmed QC version."""
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
