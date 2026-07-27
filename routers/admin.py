"""
routers/admin.py — admin-only routes.

Routes:
  GET  /admin
  GET  /admin/users
  GET  /admin/records
  GET  /admin/analysis
  GET  /admin/templates
  GET  /admin/api-status
  GET  /admin/database
  POST /admin/users/create
  GET  /admin/users/{user_id}
  POST /admin/users/{user_id}/change-password
  POST /admin/users/{user_id}/change-role
  POST /admin/users/{user_id}/toggle-active
  POST /admin/users/{user_id}/qc-access
  POST /admin/users/{user_id}/delete
  POST /admin/records/{record_id}/delete
  GET  /admin/deleted-records
  POST /admin/deleted-records/{record_id}/restore
  POST /admin/deleted-records/{record_id}/delete-permanent
  GET  /admin/logs
  GET  /admin/logs/tail
  POST /admin/records/{record_id}/assign-post-qc
  POST /admin/records/bulk-assign-post-qc
  POST /admin/settings/theme
  POST /admin/settings/login-ip-restriction
  GET  /admin/qc-download/{quote_id}
  GET  /admin/qc-version/{version_id}/download
  GET  /admin/qc-version/{version_id}
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
from datetime import datetime

import db.user_repo as adb
import db.quote_repo as db
import db.checklist_repo as tdb
import db.audit_repo as audit
from reports.xlsx_builder import build_xlsx
from db.connection import get_db

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from config import (
    CLAUDE_MODEL,
    CLAUDE_PRICE_PER_M_INPUT,
    CLAUDE_PRICE_PER_M_OUTPUT,
    _NO_CACHE,
    _admin_ctx,
    _anthropic_client,
    _build_install_map,
    _resolve_theme,
    _signer,
    is_login_ip_restricted,
    require_admin,
    require_superadmin,
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
def admin_dashboard(request: Request, user=Depends(require_admin), cal: str = "", success: str = None):
    all_records = db.get_recent(limit=None)
    ctx = _admin_ctx(user,
        users_count=len(adb.list_users()),
        records_count=db.count_quotes(),
        templates_count=len(tdb.list_templates()),
        install_map_json=_build_install_map(all_records),
        cal_jump=cal,  # e.g. "2026-08" — tells the calendar JS which month to open
        success=success,
    )
    response = templates.TemplateResponse(request, "admin_dashboard.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, user=Depends(require_admin), success: str = None):
    # Super-admin accounts never appear in this list — for anyone, including
    # another super admin viewing their own account — to keep who holds
    # super-admin privileges from being visible/browsable here at all.
    visible_users = [u for u in adb.list_users() if not u.get("is_super_admin")]
    ctx = _admin_ctx(user,
        users=visible_users,
        success="User created successfully." if success else None,
        error=None,
    )
    response = templates.TemplateResponse(request, "admin_users.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/records", response_class=HTMLResponse)
def admin_records_page(request: Request, user=Depends(require_admin)):
    post_qc_users = [u for u in adb.list_users() if u.get("can_post_qc") and u.get("is_active", 1)]
    ctx = _admin_ctx(user, records=db.get_recent(limit=None), post_qc_users=post_qc_users)
    response = templates.TemplateResponse(request, "admin_records.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/analysis", response_class=HTMLResponse)
def admin_analysis_page(request: Request, month: str = "", user=Depends(require_superadmin)):
    """Super-admin-only QC throughput tracker: pick a month, see how many
    Pre-QC/Post-QC checks were confirmed and by whom."""
    available_months = db.get_qc_analysis_months()
    selected_month = month.strip() if month.strip() in available_months else (
        available_months[0] if available_months else datetime.now().strftime("%Y-%m")
    )
    stats = db.get_qc_monthly_stats(selected_month)
    ctx = _admin_ctx(
        user,
        available_months=available_months,
        selected_month=selected_month,
        stats=stats,
        claude_price_in=CLAUDE_PRICE_PER_M_INPUT,
        claude_price_out=CLAUDE_PRICE_PER_M_OUTPUT,
    )
    response = templates.TemplateResponse(request, "admin_analysis.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/templates", response_class=HTMLResponse)
def admin_templates_page(request: Request, user=Depends(require_admin)):
    ctx = _admin_ctx(
        user,
        templates=tdb.list_templates(),
        pre_templates=tdb.list_templates(kind="pre"),
        post_templates=tdb.list_templates(kind="post"),
    )
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
        audit.log_event(request, "user_created", username=user["username"], user_id=user["id"],
                        detail=f"created '{username}' ({role})")
        return RedirectResponse(url="/admin/users?success=1", status_code=303)
    ctx = _admin_ctx(user,
        users=adb.list_users(),
        error=f"Username '{username}' already exists, is invalid, or password doesn't meet the requirements "
              f"(min. 8 characters, with uppercase, lowercase, a number, and a symbol).",
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
    # A super admin's profile is never visible here — not even to another
    # super admin — same rule as the Users list; direct-URL access must not
    # bypass it, so this is a 404 (not 403) to avoid even confirming the
    # account exists.
    if target.get("is_super_admin"):
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
                          new_password: str = Form(...),
                          user=Depends(require_admin)):
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    # Changing another admin's password is a super-admin-only action; a
    # regular admin can still reset any plain user's password freely.
    if target.get("role") == "admin" and not user.get("is_super_admin"):
        raise HTTPException(403, "Only a super admin can change another admin's password.")
    ok = adb.change_password(user_id, new_password)
    if ok:
        audit.log_event(request, "password_reset", username=user["username"], user_id=user["id"],
                        detail=f"reset password of '{target.get('username')}'")
        return RedirectResponse(url=f"/admin/users/{user_id}?success=Password+changed+successfully.", status_code=303)
    return RedirectResponse(
        url=f"/admin/users/{user_id}?error=Password+must+be+8-256+characters+with+uppercase,+lowercase,+a+number,+and+a+symbol.",
        status_code=303,
    )


@router.post("/admin/users/{user_id}/change-role")
def admin_change_role(user_id: int, request: Request,
                      new_role: str = Form(...),
                      user=Depends(require_admin)):
    if user_id == user["id"]:
        return RedirectResponse(url=f"/admin/users/{user_id}?error=Cannot+change+your+own+role.", status_code=303)
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    # Changing another admin's role (e.g. demoting them) is super-admin-only;
    # promoting a plain user to admin is still allowed for any admin.
    if target.get("role") == "admin" and not user.get("is_super_admin"):
        raise HTTPException(403, "Only a super admin can change another admin's role.")
    ok = adb.change_role(user_id, new_role)
    if ok:
        audit.log_event(request, "role_changed", username=user["username"], user_id=user["id"],
                        detail=f"'{target.get('username')}' -> {new_role}")
        label = "Admin" if new_role == "admin" else "User"
        return RedirectResponse(url=f"/admin/users/{user_id}?success=Role+changed+to+{label}.", status_code=303)
    return RedirectResponse(url=f"/admin/users/{user_id}?error=Invalid+role.", status_code=303)


@router.post("/admin/users/{user_id}/toggle-active")
def admin_toggle_active(user_id: int, request: Request, user=Depends(require_admin)):
    if user_id == user["id"]:
        return RedirectResponse(url=f"/admin/users/{user_id}?error=Cannot+deactivate+your+own+account.", status_code=303)
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    if target.get("role") == "admin":
        raise HTTPException(403, "Admin accounts cannot be deactivated.")
    new_active = not target.get("is_active", 1)
    adb.set_active(user_id, new_active)
    label = "activated" if new_active else "deactivated"
    audit.log_event(request, f"user_{label}", username=user["username"], user_id=user["id"],
                    detail=f"'{target.get('username')}'")
    return RedirectResponse(url=f"/admin/users/{user_id}?success=User+{label}.", status_code=303)


@router.post("/admin/users/{user_id}/qc-access")
def admin_set_qc_access(user_id: int, request: Request,
                        can_pre_qc: str = Form(""), can_post_qc: str = Form(""),
                        user=Depends(require_admin)):
    target = adb.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    if target.get("role") == "admin":
        raise HTTPException(403, "QC access does not apply to admin accounts.")
    adb.set_qc_access(user_id, can_pre_qc=bool(can_pre_qc), can_post_qc=bool(can_post_qc))
    audit.log_event(request, "qc_access_changed", username=user["username"], user_id=user["id"],
                    detail=f"'{target.get('username')}': pre={bool(can_pre_qc)}, post={bool(can_post_qc)}")
    return RedirectResponse(url=f"/admin/users/{user_id}?success=QC+access+updated.", status_code=303)


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
    audit.log_event(request, "user_deleted", username=user["username"], user_id=user["id"],
                    detail=f"deleted account '{target.get('username')}'")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/record/{record_id}", response_class=HTMLResponse)
def admin_record_detail(record_id: int, request: Request, user=Depends(require_admin)):
    record = db.get_quote(record_id)
    if not record:
        raise HTTPException(404, "Record not found.")
    # A soft-deleted record is invisible to regular Team Leaders — a saved link
    # or guessed URL 404s just as if it were gone. Super admins reach it only
    # through the Deleted Records trash, not here.
    if record.get("is_deleted") and not user.get("is_super_admin"):
        raise HTTPException(404, "Record not found.")
    qc_versions = db.get_qc_versions(record_id)
    # Jump straight to the checklist page (which now shows both Pre-QC and
    # Post-QC tiles together — see admin_qc_version_view) instead of an
    # intermediate summary page. Picks whichever kind has the most recent
    # activity; the other kind still shows up there as the 4th tile if it
    # exists. Only a customer with zero QC runs of any kind has no
    # checklist page to land on, so that case alone still shows the (now
    # necessarily empty) box-summary page.
    if qc_versions:
        most_recent = max(qc_versions, key=lambda v: v.get("confirmed_at") or v.get("saved_at") or "")
        return RedirectResponse(url=f"/admin/qc-version/{most_recent['id']}", status_code=303)
    pre_versions  = [v for v in qc_versions if v.get("kind", "pre") == "pre"]
    post_versions = [v for v in qc_versions if v.get("kind") == "post"]
    assignee = db.get_post_qc_assignee(record_id)
    ctx = _admin_ctx(
        user, record=record,
        pre_versions=pre_versions, post_versions=post_versions,
        latest_pre=None, latest_post=None,
        post_qc_assignee=assignee,
    )
    response = templates.TemplateResponse(request, "admin_record.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.post("/admin/records/{record_id}/reschedule")
def admin_reschedule_install_date(record_id: int, request: Request,
                                   preferred_install_date: str = Form(""),
                                   user=Depends(require_admin)):
    """Let admin/super-admin push a job's install date out (e.g. a rain
    delay) directly, without needing to reopen and resave a QC version —
    a standalone action separate from the checklist Save/Confirm flow.
    Writes the same preferred_install_date column the technician-facing
    Confirm screen already edits, so the calendar picks the new date up
    immediately (it already prefers preferred_install_date over the
    original AI-extracted install_date)."""
    record = db.get_quote(record_id)
    if not record:
        raise HTTPException(404, "Record not found.")
    new_date = preferred_install_date.strip()
    old_date = record.get("preferred_install_date") or ""
    changed = new_date != old_date
    if changed:
        db.update_preferred_install_date(record_id, new_date)
        audit.log_event(request, "install_date_rescheduled", username=user["username"], user_id=user["id"],
                        detail=f"quote {record.get('quote_number') or record_id} ({record.get('customer_name') or ''}): "
                               f"'{old_date or '(none)'}' -> '{new_date or '(none)'}'")
    # Land back on the Dashboard (where the calendar lives) rather than the
    # QC-version page the request came from — that page was itself reached
    # through a chain of earlier redirects, so bouncing back there just made
    # the browser's Back button retrace that whole chain instead of reaching
    # the Dashboard in one step. Jump the calendar to the new date's month so
    # the moved job is immediately visible, confirming the change worked.
    cal_param = ""
    if new_date:
        try:
            cal_param = f"&cal={new_date[:7]}"  # "YYYY-MM-DD" -> "YYYY-MM"
        except Exception:
            cal_param = ""
    msg = urllib.parse.quote(f"Installation date updated to {new_date}." if new_date and changed
                              else "Installation date cleared." if changed
                              else "No change — installation date was already set to that value.")
    return RedirectResponse(url=f"/admin?success={msg}{cal_param}", status_code=303)


@router.post("/admin/records/{record_id}/delete")
def admin_delete_record(record_id: int, request: Request, user=Depends(require_admin)):
    """A Team Leader's Delete is a SOFT delete — the record leaves every active
    view but stays in the DB, recoverable from the super-admin Deleted Records
    trash. Permanent deletion happens only from there."""
    _rec = db.get_quote(record_id)
    db.soft_delete_quote(record_id, deleted_by_user_id=user["id"])
    audit.log_event(request, "record_deleted", username=user["username"], user_id=user["id"],
                    detail=f"quote {(_rec or {}).get('quote_number') or record_id} ({(_rec or {}).get('customer_name') or ''})")
    return RedirectResponse(url="/admin/records", status_code=303)


@router.post("/admin/qc-version/{version_id}/delete")
def admin_delete_qc_version(version_id: int, request: Request, user=Depends(require_admin)):
    """Delete ONE Pre-QC or Post-QC run (e.g. a duplicate or mistaken
    upload) without touching the rest of that customer's record — a Team
    Leader's Delete here is also a SOFT delete, recoverable from the
    super-admin Deleted Records trash, same as the whole-record delete."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT qv.quote_id, qv.version, qv.template_name, COALESCE(qv.kind, 'pre') as kind,
                      q.quote_number, q.customer_name
               FROM qc_versions qv JOIN quotes q ON q.id = qv.quote_id
               WHERE qv.id = ?""",
            (version_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
    db.soft_delete_qc_version(version_id, deleted_by_user_id=user["id"])
    audit.log_event(request, "qc_version_deleted", username=user["username"], user_id=user["id"],
                    detail=f"{v['kind']}-QC v{v['version']} ({v.get('template_name') or ''}) for quote "
                           f"{v.get('quote_number') or v['quote_id']} ({v.get('customer_name') or ''})")
    return RedirectResponse(url=f"/admin/record/{v['quote_id']}", status_code=303)


@router.get("/admin/deleted-records", response_class=HTMLResponse)
def admin_deleted_records_page(request: Request, user=Depends(require_superadmin)):
    """Super-admin-only trash: every soft-deleted customer record AND every
    individually-deleted QC version, with the option to restore or delete
    each permanently."""
    ctx = _admin_ctx(
        user,
        deleted_records=db.get_deleted_quotes(),
        deleted_qc_versions=db.get_deleted_qc_versions(),
    )
    response = templates.TemplateResponse(request, "admin_deleted_records.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.post("/admin/deleted-records/{record_id}/restore")
def admin_restore_record(record_id: int, request: Request, user=Depends(require_superadmin)):
    """Restore a soft-deleted record back into the active list. Super-admin only."""
    db.restore_quote(record_id)
    _rec = db.get_quote(record_id)
    audit.log_event(request, "record_restored", username=user["username"], user_id=user["id"],
                    detail=f"quote {(_rec or {}).get('quote_number') or record_id} ({(_rec or {}).get('customer_name') or ''})")
    return RedirectResponse(url="/admin/deleted-records", status_code=303)


@router.post("/admin/deleted-records/{record_id}/delete-permanent")
def admin_delete_record_permanent(record_id: int, request: Request, user=Depends(require_superadmin)):
    """Permanently remove a record from the database, including its QC versions
    and on-disk Excel files. Super-admin only, and only reachable from the
    trash — this is the irreversible delete."""
    _rec = db.get_quote(record_id)
    db.delete_quote(record_id)
    audit.log_event(request, "record_purged", username=user["username"], user_id=user["id"],
                    detail=f"permanently deleted quote {(_rec or {}).get('quote_number') or record_id} ({(_rec or {}).get('customer_name') or ''})")
    return RedirectResponse(url="/admin/deleted-records", status_code=303)


@router.post("/admin/deleted-qc-versions/{version_id}/restore")
def admin_restore_qc_version(version_id: int, request: Request, user=Depends(require_superadmin)):
    """Restore an individually-deleted QC version. Super-admin only."""
    db.restore_qc_version(version_id)
    audit.log_event(request, "qc_version_restored", username=user["username"], user_id=user["id"],
                    detail=f"QC version {version_id}")
    return RedirectResponse(url="/admin/deleted-records", status_code=303)


@router.post("/admin/deleted-qc-versions/{version_id}/delete-permanent")
def admin_delete_qc_version_permanent(version_id: int, request: Request, user=Depends(require_superadmin)):
    """Permanently remove one QC version and its on-disk Excel file.
    Super-admin only, and only reachable from the trash — irreversible."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT qv.version, COALESCE(qv.kind, 'pre') as kind, q.quote_number, q.customer_name
               FROM qc_versions qv JOIN quotes q ON q.id = qv.quote_id WHERE qv.id = ?""",
            (version_id,),
        ).fetchone()
    v = dict(row) if row else {}
    db.delete_qc_version_permanent(version_id)
    audit.log_event(request, "qc_version_purged", username=user["username"], user_id=user["id"],
                    detail=f"permanently deleted {v.get('kind','')}-QC v{v.get('version','')} for quote "
                           f"{v.get('quote_number') or version_id} ({v.get('customer_name') or ''})")
    return RedirectResponse(url="/admin/deleted-records", status_code=303)


# ---------------------------------------------------------------------------
# Backend logs (super admin only)
# ---------------------------------------------------------------------------

_APP_LOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "logs", "app.log")
)


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_logs_page(request: Request, user=Depends(require_superadmin)):
    """Super-admin-only live view of the backend log (logs/app.log — app
    errors, DB warnings, and crashes). The page polls /admin/logs/tail."""
    ctx = _admin_ctx(user, login_ip_restricted=is_login_ip_restricted())
    response = templates.TemplateResponse(request, "admin_logs.html", ctx)
    response.headers.update(_NO_CACHE)
    return response


@router.get("/admin/logs/tail")
def admin_logs_tail(lines: int = 300, user=Depends(require_superadmin)):
    """Last N lines of logs/app.log as JSON — polled by the Logs page every
    few seconds for a live view. Reads only the file's tail (max 512 KB), so
    it stays cheap even when the log approaches its 10 MB rotation cap."""
    lines = max(10, min(lines, 2000))
    if not os.path.exists(_APP_LOG_PATH):
        return {"lines": [], "size": 0, "mtime": None}
    size = os.path.getsize(_APP_LOG_PATH)
    read_bytes = min(size, 512 * 1024)
    with open(_APP_LOG_PATH, "rb") as f:
        if size > read_bytes:
            f.seek(size - read_bytes)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    tail_lines = text.splitlines()[-lines:]
    mtime = datetime.fromtimestamp(os.path.getmtime(_APP_LOG_PATH)).strftime("%Y-%m-%d %H:%M:%S")
    return {"lines": tail_lines, "size": size, "mtime": mtime}


@router.get("/admin/logs/audit")
def admin_logs_audit(limit: int = 200, user=Depends(require_superadmin)):
    """Recent security-audit events (logins, failed logins, account changes,
    record deletions, DB access) with actor, source IP, and device — polled
    by the Logs page. Super admin only."""
    return {"events": audit.get_recent(limit)}


@router.post("/admin/records/{record_id}/assign-post-qc")
def admin_assign_post_qc(record_id: int, request: Request,
                         assignee_id: str = Form(""), user=Depends(require_admin)):
    record = db.get_quote(record_id)
    if not record:
        raise HTTPException(404, "Customer record not found.")
    if not assignee_id.strip():
        db.unassign_post_qc(record_id)
        return RedirectResponse(url="/admin/records", status_code=303)
    target = adb.get_user(int(assignee_id))
    if not target or target.get("role") != "user" or not target.get("can_post_qc"):
        raise HTTPException(400, "Selected user is not eligible for Post-QC assignment.")
    db.assign_post_qc(record_id, int(assignee_id))
    return RedirectResponse(url="/admin/records", status_code=303)


@router.post("/admin/records/bulk-assign-post-qc")
async def admin_bulk_assign_post_qc(request: Request, user=Depends(require_admin)):
    """Assign every selected customer to one Post-QC user in a single action
    — the checkbox + toolbar workflow on Customer Records, so an admin
    doesn't have to pick a user from the row dropdown one customer at a
    time. Only customers with at least one Pre-QC run are ever selectable
    in the UI, but skip anything that isn't (or doesn't exist) defensively
    rather than trusting the submitted IDs."""
    form = await request.form()
    record_ids = form.getlist("record_ids")
    assignee_id = form.get("assignee_id", "")
    if not assignee_id:
        raise HTTPException(400, "No user selected.")
    target = adb.get_user(int(assignee_id))
    if not target or target.get("role") != "user" or not target.get("can_post_qc"):
        raise HTTPException(400, "Selected user is not eligible for Post-QC assignment.")
    for rid in record_ids:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            continue
        record = db.get_quote(rid_int)
        if not record:
            continue
        db.assign_post_qc(rid_int, int(assignee_id))
    return RedirectResponse(url="/admin/records", status_code=303)


@router.post("/admin/settings/theme")
def admin_set_theme(request: Request, theme: str = Form(...), user=Depends(require_admin),
                    referer: str = None):
    if theme in ("dark", "light"):
        adb.set_setting("user_panel_theme", theme)
    return RedirectResponse(url=_redirect_back_to_admin(request), status_code=303)


@router.post("/admin/settings/login-ip-restriction")
def admin_set_login_ip_restriction(request: Request, enabled: str = Form(...),
                                    user=Depends(require_superadmin)):
    """Toggle whether /login (the user-side login page) is restricted to
    the office IP allowlist. Off = anyone can reach /login from any
    network. On = only LOGIN_ALLOWED_IPS can. Admin login is never
    affected by this either way. Super-admin only — the control lives on
    the Logs page with the rest of the security tooling."""
    set_login_ip_restricted(enabled == "1")
    audit.log_event(request, "ip_restriction_changed", username=user["username"], user_id=user["id"],
                    detail="turned ON" if enabled == "1" else "turned OFF")
    return RedirectResponse(url=_redirect_back_to_admin(request), status_code=303)


# ---------------------------------------------------------------------------
# QC download / versioned view (admin)
# ---------------------------------------------------------------------------

@router.get("/admin/qc-download/{quote_id}", response_class=Response)
def admin_qc_download(quote_id: int, user=Depends(require_admin)):
    """Download the latest QC Excel for a quote (admin only)."""
    record = db.get_quote(quote_id)
    xlsx = db.get_quote_qc_excel(quote_id)
    if not record or not xlsx:
        raise HTTPException(404, "No QC report found for this record.")
    raw_name = (record.get("customer_name") or f"quote_{quote_id}").replace(" ", "_")
    safe_name = raw_name.replace('"', "").replace("\n", "").replace("\r", "")[:80]
    return Response(
        content=xlsx,
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
    """Admin view of a specific QC version — renders the same 3-tile page the
    user side uses (templates/user_result.html), so both roles share one
    template/one set of edit/save/upload behaviors instead of two drifting
    copies. is_admin=True gates the handful of pieces that differ (Full
    Record link vs. the fresh-run step tracker/rerun panel/bottom save card,
    which never apply to admin since admin always revisits a saved version)."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT qv.*, q.customer_name, q.quote_number, q.install_date,
                      q.email, q.phone, q.total_price, q.system_price,
                      q.deposit, q.balance, q.payment_terms, q.billing_address,
                      q.preferred_install_date, q.is_deleted AS quote_is_deleted
               FROM qc_versions qv JOIN quotes q ON q.id = qv.quote_id
               WHERE qv.id = ?""",
            (version_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "QC version not found.")
    v = dict(row)
    # QC data for a soft-deleted customer is off-limits to regular Team Leaders,
    # same as the record detail page — only the trash exposes deleted records.
    if v.get("quote_is_deleted") and not user.get("is_super_admin"):
        raise HTTPException(404, "QC version not found.")
    # Same rule for an individually deleted version (the customer record
    # itself may still be active) — only super-admin can reach it, via
    # the trash, once it's been deleted.
    if v.get("is_deleted") and not user.get("is_super_admin"):
        raise HTTPException(404, "QC version not found.")
    try:
        rows = json.loads(v["rows_json"]) if v.get("rows_json") else []
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    try:
        email_results = json.loads(v.get("email_results_json") or "[]") or []
    except Exception:
        email_results = []

    tpl = {
        "id": 0, "name": v.get("template_name", ""),
        "customer_label": "", "address_label": "", "job_label": "", "note_text": "",
        "kind": v.get("kind") or "pre",
    }
    # Same tmp-file token trick user_qc_version_revisit uses, so admin's
    # Download Excel goes through the same /checklist-download mechanism.
    # get_qc_version_excel is disk-first with a legacy-blob fallback.
    dl_token = ""
    _xlsx_bytes = db.get_qc_version_excel(v["id"])
    if _xlsx_bytes:
        xlsx_dir = os.path.join(os.path.dirname(__file__), "..", "tmp_xlsx")
        os.makedirs(xlsx_dir, exist_ok=True)
        xlsx_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", dir=xlsx_dir, delete=False)
        try:
            xlsx_tmp.write(_xlsx_bytes)
        finally:
            xlsx_tmp.close()
        dl_token = _signer.dumps({"xlsx_path": xlsx_tmp.name, "name": tpl["name"]})

    yes_count = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "Yes")
    no_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "No")
    na_count  = sum(1 for r in rows if not r.get("is_section") and r.get("status") == "N/A")

    # Pull in the OTHER kind's latest version too, so an admin viewing
    # either Pre-QC or Post-QC sees both on one page — a 4th tile appears
    # only when that other kind actually has a run; otherwise the page
    # looks exactly like it always has (3 tiles).
    other_ctx = db.get_other_kind_context(v["quote_id"], tpl["kind"], email_results)

    # Full PDF-extracted quote record — the Signed Agreement summary block is
    # view-only here (editing it belongs on the Customer Record page), but
    # it's shown so an admin can cross-check without leaving this page.
    record = db.get_quote(v["quote_id"])
    # Only needed for the Post-QC tile's empty state (who it's assigned to,
    # if no Post-QC run has happened yet).
    post_qc_assignee = (
        db.get_post_qc_assignee(v["quote_id"])
        if other_ctx["other_kind"] == "post" and not other_ctx["other_version"] else None
    )

    resp = templates.TemplateResponse(request, "user_result.html", {
        "current_user": user,
        "theme": _resolve_theme(user),
        "is_admin": True,
        "tpl": tpl,
        "checklist_rows": rows,
        "dl_token": dl_token,
        "quote_id": v["quote_id"],
        "zip_filename": v.get("zip_filename", ""),
        "yes_count": yes_count,
        "no_count": no_count,
        "na_count": na_count,
        "email_results": email_results,
        "revisit_version": v,
        "preferred_install_date": v.get("preferred_install_date") or "",
        "today": datetime.now().strftime("%Y-%m-%d"),
        "record": record,
        "post_qc_assignee": post_qc_assignee,
        "claude_price_in": CLAUDE_PRICE_PER_M_INPUT,
        "claude_price_out": CLAUDE_PRICE_PER_M_OUTPUT,
        **other_ctx,
    })
    resp.headers.update(_NO_CACHE)
    return resp


# ---------------------------------------------------------------------------
# Database backup / restore (admin only)
# ---------------------------------------------------------------------------

@router.get("/db/download")
def db_download(request: Request, user=Depends(require_admin)):
    from db.connection import DB_PATH
    audit.log_event(request, "db_downloaded", username=user["username"], user_id=user["id"])
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
        audit.log_event(request, "db_restored", username=user["username"], user_id=user["id"],
                        detail=f"uploaded {len(data)} bytes")
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    return RedirectResponse(url="/admin/database?restored=1", status_code=303)
