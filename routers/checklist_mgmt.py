"""
routers/checklist_mgmt.py — checklist template management and editor routes.

Routes:
  GET  /Excel
  GET  /Excel/{template_id}
  POST /templates/upload
  POST /templates/{template_id}/items/add
  POST /templates/{template_id}/items/{item_id}/update
  POST /templates/{template_id}/save-all
  POST /templates/{template_id}/items/{item_id}/delete
  POST /templates/{template_id}/items/{item_id}/move
  POST /templates/{template_id}/reorder
  GET  /admin/templates/{template_id}/fragment
  POST /templates/{template_id}/delete
  GET  /templates/{template_id}/download
"""
import io

import db.checklist_repo as tdb
from reports.xlsx_builder import build_template_xlsx, parse_xlsx

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from config import (
    _NO_CACHE,
    _resolve_theme,
    require_admin,
    require_login,
    templates,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Checklist template viewer / editor
# ---------------------------------------------------------------------------

@router.get("/Excel", response_class=HTMLResponse)
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
    response.headers.update(_NO_CACHE)
    return response


@router.get("/Excel/{template_id}", response_class=HTMLResponse)
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
    response.headers.update(_NO_CACHE)
    return response


@router.post("/templates/upload")
async def templates_upload(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    kind: str = Form("pre"),
    user=Depends(require_admin),
):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx checklist template.")
    if kind not in ("pre", "post"):
        kind = "pre"
    blob = await file.read()
    items, headers, note, title, has_header_row = parse_xlsx(blob)
    tdb.create_template(name, blob, items, note, headers, kind=kind,
                        title=title, has_header_row=has_header_row)
    return RedirectResponse(url="/admin/templates", status_code=303)


@router.post("/templates/{template_id}/items/add")
def item_add(template_id: int, text: str = Form(...), sno: str = Form(""),
             is_section: str = Form(""), reference: str = Form(""),
             user=Depends(require_admin)):
    tdb.add_item(template_id, text=text, sno=sno,
                 is_section=bool(is_section), reference=reference)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/templates/{template_id}/items/{item_id}/update")
def item_update(template_id: int, item_id: int,
                text: str = Form(...), sno: str = Form(""),
                reference: str = Form(""), prompt: str = Form(""),
                is_section: str = Form(""),
                user=Depends(require_admin)):
    tdb.update_item(item_id, text=text, sno=sno, reference=reference, prompt=prompt,
                     is_section=bool(is_section))
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/templates/{template_id}/save-all")
async def items_save_all(template_id: int, request: Request, user=Depends(require_admin)):
    form = await request.form()
    item_ids    = form.getlist("item_id")
    snos        = form.getlist("sno")
    texts       = form.getlist("text")
    refs        = form.getlist("reference")
    prompts     = form.getlist("prompt")
    is_sections = form.getlist("is_section")
    for item_id, sno, text, ref, prompt, is_section in zip(item_ids, snos, texts, refs, prompts, is_sections):
        tdb.update_item(int(item_id), text=text, sno=sno, reference=ref, prompt=prompt,
                         is_section=bool(is_section))
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/templates/{template_id}/items/{item_id}/delete")
def item_delete(template_id: int, item_id: int, user=Depends(require_admin)):
    tdb.delete_item(item_id)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/templates/{template_id}/items/{item_id}/move")
def item_move(template_id: int, item_id: int, direction: str = Form(...),
              user=Depends(require_admin)):
    items = tdb.get_items(template_id)
    ids = [it["id"] for it in items]
    if item_id in ids:
        i = ids.index(item_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            tdb.reorder_items(template_id, ids)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/templates/{template_id}/reorder")
async def item_reorder(template_id: int, request: Request, user=Depends(require_admin)):
    form = await request.form()
    item_ids = form.getlist("item_id")
    if item_ids:
        tdb.reorder_items(template_id, [int(i) for i in item_ids])
    return Response(status_code=204)


@router.get("/admin/templates/{template_id}/fragment", response_class=HTMLResponse)
def admin_template_fragment(template_id: int, request: Request, user=Depends(require_admin)):
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)
    response = templates.TemplateResponse(
        request, "user_fragment.html",
        {"tpl": tpl, "items": items, "current_user": user, "theme": _resolve_theme(user)},
    )
    response.headers.update(_NO_CACHE)
    return response


@router.post("/templates/{template_id}/delete")
def template_delete(template_id: int, user=Depends(require_admin)):
    tdb.delete_template(template_id)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/templates/{template_id}/download")
def template_download(template_id: int, user=Depends(require_admin)):
    tpl = tdb.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    items = tdb.get_items(template_id)
    hdrs = {
        "customer_label": tpl.get("customer_label"),
        "address_label":  tpl.get("address_label"),
        "job_label":      tpl.get("job_label"),
    }
    build_kwargs = {"has_header_row": bool(tpl.get("has_header_row", 1))}
    if tpl.get("title"):
        build_kwargs["title"] = tpl["title"]
    blob = build_template_xlsx(items, hdrs, tpl.get("note_text", ""), **build_kwargs)
    safe = (tpl["name"] or "checklist").replace(" ", "_")
    safe = "".join(c for c in safe if c.isalnum() or c in "_-")[:80] or "checklist"
    return StreamingResponse(
        io.BytesIO(blob),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
    )
