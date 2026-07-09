"""
routers/admin.py — admin-only routes.

Routes:
  GET  /admin
  GET  /admin/users
  GET  /admin/records
  GET  /admin/templates
  GET  /admin/api-status
  GET  /admin/database
  POST /admin/users/create
  GET  /admin/users/{user_id}
  POST /admin/users/{user_id}/change-password
  POST /admin/users/{user_id}/change-role
  POST /admin/users/{user_id}/delete
  POST /admin/records/{record_id}/delete
  POST /admin/settings/theme
  POST /admin/settings/login-ip-restriction
  GET  /admin/qc-download/{quote_id}
  GET  /admin/qc-version/{version_id}/download
  GET  /admin/qc-version/{version_id}
  POST /admin/qc-version/{version_id}/save
  GET  /db/download
  POST /db/restore
"""
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse

import db.user_repo as adb
import db.quote_repo as db
import db.checklist_repo as tdb
from reports.xlsx_builder import build_xlsx
from db.connection import get_db

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from config import (
    CLAUDE_MODEL,
    _NO_CACHE,
    _admin_ctx,
    _anthropic_client,
    _build_install_map,
    _resolve_theme,
    is_login_ip_restricted,
    require_admin,
    set_login_ip_restricted,
    templates,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _redirect_back_to_admin(request: Request, fallback: str = "/admin") -> str:
    """Return the referer's path+query (so a settings toggle redirects back
    to whatever admin page the form was submitted from), or `fallback` if
    there's no usable referer.

    Only compares the URL PATH, not scheme/host — request.base_url can't be
    trusted to match the referer's scheme here, since this app sits behind
    an nginx reverse proxy over a Unix socket and gunicorn's UvicornWorker
    doesn't have proxy-header trust configured for that transport, so
    request.base_url may resolve to "http://..." even when the site is only
    ever served over https. Comparing full origins (scheme+host) against
    the referer would then never match, silently sending every redirect to
    `fallback` instead of back to the originating page.
    """
    ref = request.headers.get("referer", "")
    if not ref:
        return fallback
    path = urllib.parse.urlparse(ref).path
    if path.startswith("/admin"):
        parsed = urllib.parse.urlparse(ref)
        return urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, ""))
    return fallback


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
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
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, user=Depends(require_admin), success: str = None):
    ctx = _admin_ctx(user,
        users=adb.list_users(),
        success="User created successfully." if success else None,
        error=None,
        login_ip_restricted=is_login_ip_restricted(),
    )
    response = templates.TemplateResponse(request, "admin_users.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/records", response_class=HTMLResponse)
def admin_records_page(request: Request, user=Depends(require_admin)):
    ctx = _admin_ctx(user, records=db.get_recent())
    response = templates.TemplateResponse(request, "admin_records.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/templates", response_class=HTMLResponse)
def admin_templates_page(request: Request, user=Depends(require_admin)):
    ctx = _admin_ctx(user, templates=tdb.list_templates())
    response = templates.TemplateResponse(request, "admin_templates.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/heartbeat")
def admin_heartbeat(user=Depends(require_admin)):
    """No-op ping so genuine reading/thinking time (no clicks) still counts
    as activity — passing through require_admin lets
    InactivityTimeoutMiddleware refresh the session's idle timer."""
    return {"ok": True}


@router.get("/admin/api-status")
def admin_api_status(user=Depends(require_admin)):
    """Ping Claude and return latency + status for the dashboard meter."""
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


@router.get("/admin/database", response_class=HTMLResponse)
def admin_database_page(request: Request, restored: int = 0, user=Depends(require_admin)):
    ctx = _admin_ctx(user, restored=bool(restored))
    response = templates.TemplateResponse(request, "admin_database.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.post("/admin/users/create", response_class=HTMLResponse)
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
    if ok:
        return RedirectResponse(url="/admin/users?success=1", status_code=303)
    ctx = _admin_ctx(user,
        users=adb.list_users(),
        error=f"Username '{username}' already exists, is invalid, or password is too short (min. 8 characters).",
        success=None,
    )
    response = templates.TemplateResponse(request, "admin_users.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
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
    response.headers.update(_NO_CACHE)
    return response


@router.post("/admin/users/{user_id}/update-profile")
def admin_update_profile(user_id: int, request: Request,
                         full_name: str = Form(""),
                         email: str = Form(""),
                         phone: str = Form(""),
                         phone_country: str = Form(""),
                         phone_number: str = Form(""),
                         user=Depends(require_admin)):
    if user_id != user["id"]:
        raise HTTPException(403, "Cannot edit another user's profile here.")
    # `phone` is the combined hidden field set by JS; fallback to building it from parts
    combined_phone = phone.strip()
    if not combined_phone and phone_number.strip():
        digits = "".join(c for c in phone_number if c.isdigit())
        combined_phone = f"{phone_country.strip()} {digits}".strip() if phone_country.strip() else digits
    adb.update_profile(user_id, full_name.strip(), email.strip(), combined_phone)
    return RedirectResponse(url=_redirect_back_to_admin(request), status_code=303)


@router.post("/admin/users/{user_id}/change-password")
def admin_change_password(user_id: int, request: Request,
                          old_password: str = Form(...),
                          new_password: str = Form(...),
                          user=Depends(require_admin)):
    import bcrypt
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    # Verify old password matches stored hash
    if not bcrypt.checkpw(old_password.encode(), target["password"].encode()):
        return RedirectResponse(url=f"/admin/users/{user_id}?error=Current+password+is+incorrect.", status_code=303)
    ok = adb.change_password(user_id, new_password)
    if ok:
        return RedirectResponse(url=f"/admin/users/{user_id}?success=Password+changed+successfully.", status_code=303)
    return RedirectResponse(url=f"/admin/users/{user_id}?error=Password+must+be+at+least+8+characters.", status_code=303)


@router.post("/admin/users/{user_id}/change-role")
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


@router.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request, user=Depends(require_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account.")
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    if target.get("role") == "admin":
        raise HTTPException(403, "Cannot delete another admin account.")
    adb.delete_user(user_id)
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/record/{record_id}", response_class=HTMLResponse)
def admin_record_detail(record_id: int, request: Request, user=Depends(require_admin)):
    record = db.get_quote(record_id)
    if not record:
        raise HTTPException(404, "Record not found.")
    qc_versions = db.get_qc_versions(record_id)
    ctx = _admin_ctx(user, record=record, qc_versions=qc_versions)
    response = templates.TemplateResponse(request, "admin_record.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.post("/admin/records/{record_id}/delete")
def admin_delete_record(record_id: int, request: Request, user=Depends(require_admin)):
    db.delete_quote(record_id)
    return RedirectResponse(url="/admin/records", status_code=303)


@router.post("/admin/settings/theme")
def admin_set_theme(request: Request, theme: str = Form(...), user=Depends(require_admin),
                    referer: str = None):
    if theme in ("dark", "light"):
        adb.set_setting("user_panel_theme", theme)
    return RedirectResponse(url=_redirect_back_to_admin(request), status_code=303)


@router.post("/admin/settings/login-ip-restriction")
def admin_set_login_ip_restriction(request: Request, enabled: str = Form(...),
                                    user=Depends(require_admin)):
    """Toggle whether /login (the user-side login page) is restricted to
    the office IP allowlist. Off = anyone can reach /login from any
    network. On = only LOGIN_ALLOWED_IPS can. Admin login is never
    affected by this either way."""
    set_login_ip_restricted(enabled == "1")
    return RedirectResponse(url=_redirect_back_to_admin(request), status_code=303)


# ---------------------------------------------------------------------------
# QC download / versioned view (admin)
# ---------------------------------------------------------------------------

@router.get("/admin/qc-download/{quote_id}", response_class=Response)
def admin_qc_download(quote_id: int, user=Depends(require_admin)):
    """Download the latest QC Excel for a quote (admin only)."""
    record = db.get_quote(quote_id)
    if not record or not record.get("qc_excel"):
        raise HTTPException(404, "No QC report found for this record.")
    raw_name = (record.get("customer_name") or f"quote_{quote_id}").replace(" ", "_")
    safe_name = raw_name.replace('"', "").replace("\n", "").replace("\r", "")[:80]
    return Response(
        content=bytes(record["qc_excel"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_QC.xlsx"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@router.get("/admin/qc-version/{version_id}/download", response_class=Response)
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


@router.get("/admin/qc-version/{version_id}", response_class=HTMLResponse)
def admin_qc_version_view(request: Request, version_id: int, user=Depends(require_admin)):
    """Admin view of a specific QC version with inline edit capability."""
    with get_db() as conn:
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
    try:
        email_results = json.loads(v.get("email_results_json") or "[]") or []
    except Exception:
        email_results = []
    resp = templates.TemplateResponse(request, "admin_qc_version.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "user_panel_theme": adb.get_setting("user_panel_theme", "dark"),
        "v": v,
        "rows": rows,
        "email_results": email_results,
    })
    resp.headers.update(_NO_CACHE)
    return resp


@router.post("/admin/qc-version/{version_id}/save")
async def admin_qc_version_save(request: Request, version_id: int, user=Depends(require_admin)):
    """Save admin edits to a QC version — rebuilds Excel and updates DB."""
    body = await request.json()
    rows   = body.get("rows", [])
    action = body.get("action", "draft")  # "draft" or "confirm"

    # Rebuild Excel from edited rows
    filled = {}
    for row in rows:
        if not row.get("is_section") and row.get("position") is not None:
            filled[row["position"]] = {
                "status": row.get("status", "N/A"),
                "remark": row.get("remark", ""),
                "ai_status": row.get("ai_status", ""),
            }

    xlsx_blob = build_xlsx(rows, filled=filled)
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
    )
    return {"ok": True, "yes_count": yes_count, "no_count": no_count, "na_count": na_count}


# ---------------------------------------------------------------------------
# Database backup / restore (admin only)
# ---------------------------------------------------------------------------

@router.get("/db/download")
def db_download(user=Depends(require_admin)):
    from db.connection import DB_PATH
    src = sqlite3.connect(DB_PATH)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        dst = sqlite3.connect(tmp.name)
        src.backup(dst)
        dst.close()
        src.close()
        with open(tmp.name, "rb") as f:
            content = f.read()
    finally:
        # Always delete the temp file even if backup or read fails
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    filename = os.path.basename(DB_PATH).replace(".db", "") + "_backup.db"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_DB_RESTORE_MAX = 512 * 1024 * 1024  # 512 MB hard cap for DB restore uploads
_DB_BACKUP_KEEP = 5  # keep only the N most recent pre-restore backups


def _prune_old_backups(db_path: str, keep: int = _DB_BACKUP_KEEP):
    """Delete all but the `keep` most recent pre_restore_backup files, so
    backups don't accumulate on disk forever."""
    backup_dir = os.path.dirname(db_path) or "."
    backup_prefix = os.path.basename(db_path) + ".pre_restore_backup."
    try:
        candidates = [
            os.path.join(backup_dir, f)
            for f in os.listdir(backup_dir)
            if f.startswith(backup_prefix)
        ]
        candidates.sort(key=os.path.getmtime, reverse=True)
        for stale in candidates[keep:]:
            try:
                os.remove(stale)
            except OSError as e:
                logger.warning("DB restore: failed to prune old backup %s (%s)", stale, e)
    except OSError as e:
        logger.warning("DB restore: failed to list backups for pruning (%s)", e)


@router.post("/db/restore")
async def db_restore(request: Request, file: UploadFile = File(...), user=Depends(require_admin)):
    from db.connection import DB_PATH
    data = await file.read()
    if len(data) > _DB_RESTORE_MAX:
        raise HTTPException(400, "Uploaded file is too large. Maximum allowed size is 512 MB.")
    if not data.startswith(b"SQLite format 3"):
        raise HTTPException(400, "Invalid file — must be a SQLite database.")
    # Snapshot the live DB before overwriting so the admin can recover if the
    # uploaded file turns out to be corrupt despite passing the header check.
    # Each attempt gets its own timestamped filename so it can never collide
    # with (or inherit bad ownership from) a backup left by a previous run.
    if os.path.exists(DB_PATH):
        backup_path = f"{DB_PATH}.pre_restore_backup.{int(time.time())}"
        try:
            shutil.copy2(DB_PATH, backup_path)
            _prune_old_backups(DB_PATH)
        except OSError as e:
            # The backup is a safety net, not the point of this endpoint —
            # don't let a backup failure block the restore the admin asked for.
            logger.warning("DB restore: pre-restore backup failed (%s), continuing anyway.", e)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        shutil.move(tmp.name, DB_PATH)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    return RedirectResponse(url="/admin/database?restored=1", status_code=303)
