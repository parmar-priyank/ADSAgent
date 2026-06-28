"""
db/checklist_repo.py — storage for QC checklist templates and their items.
"""
from db.connection import get_db


def init_templates_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                blob            BLOB,
                customer_label  TEXT DEFAULT 'Customer Name  :',
                address_label   TEXT DEFAULT 'Correct Address  :',
                job_label       TEXT DEFAULT 'Job Details  :',
                note_text       TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checklist_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id      INTEGER NOT NULL,
                position         INTEGER NOT NULL,
                sno              TEXT DEFAULT '',
                parent_position  INTEGER,
                is_section       INTEGER DEFAULT 0,
                text             TEXT DEFAULT '',
                reference        TEXT DEFAULT '',
                active           INTEGER DEFAULT 1,
                FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE
            )
            """
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(checklist_items)")]
        if "reference" not in cols:
            conn.execute("ALTER TABLE checklist_items ADD COLUMN reference TEXT DEFAULT ''")
        if "prompt" not in cols:
            conn.execute("ALTER TABLE checklist_items ADD COLUMN prompt TEXT DEFAULT ''")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def create_template(name: str, blob: bytes, items: list, note_text: str = "",
                    header_labels: dict = None) -> int:
    header_labels = header_labels or {}
    with get_db() as conn:
        conn.execute("DELETE FROM templates WHERE LOWER(name) = LOWER(?)", (name,))
        cur = conn.execute(
            """INSERT INTO templates (name, blob, note_text, customer_label, address_label, job_label)
               VALUES (?,?,?,?,?,?)""",
            (
                name, blob, note_text,
                header_labels.get("customer_label") or "Customer Name  :",
                header_labels.get("address_label") or "Correct Address  :",
                header_labels.get("job_label") or "Job Details  :",
            ),
        )
        tid = cur.lastrowid
        _insert_items(conn, tid, items)
        return tid


def _insert_items(conn, template_id: int, items: list):
    for it in items:
        conn.execute(
            """
            INSERT INTO checklist_items
                (template_id, position, sno, parent_position, is_section,
                 text, reference, prompt, active)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                template_id,
                it.get("position"),
                it.get("sno", ""),
                it.get("parent_position"),
                1 if it.get("is_section") else 0,
                it.get("text", ""),
                it.get("reference", ""),
                it.get("prompt", ""),
                1 if it.get("active", 1) else 0,
            ),
        )


def list_templates():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, note_text, created_at FROM templates ORDER BY id DESC"
        ).fetchall()
    result = []
    for display_num, row in enumerate(rows, start=1):
        d = dict(row)
        d["display_num"] = display_num
        result.append(d)
    return result


def get_template(template_id: int):
    with get_db() as conn:
        t = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    return dict(t) if t else None


def delete_template(template_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


# ---------------------------------------------------------------------------
# Checklist items
# ---------------------------------------------------------------------------

def get_items(template_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM checklist_items WHERE template_id = ? ORDER BY position ASC",
            (template_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_item(template_id: int, text: str, sno: str = "",
             is_section: bool = False, position: int = None,
             reference: str = ""):
    items = get_items(template_id)
    if position is None:
        position = (max([i["position"] for i in items], default=0)) + 1
    with get_db() as conn:
        conn.execute(
            "UPDATE checklist_items SET position = position + 1 "
            "WHERE template_id = ? AND position >= ?",
            (template_id, position),
        )
        default_prompt = "" if is_section else (
            f"Verify that the '{text}' document is present, valid, "
            "and matches the agreement details."
        )
        _insert_items(conn, template_id, [{
            "position": position, "sno": sno, "parent_position": None,
            "is_section": is_section, "text": text, "reference": reference,
            "prompt": default_prompt, "active": 1,
        }])


def update_item(item_id: int, text: str = None, sno: str = None,
                active: int = None, is_section: int = None,
                reference: str = None, prompt: str = None):
    sets, vals = [], []
    if text is not None:
        sets.append("text = ?"); vals.append(text)
    if sno is not None:
        sets.append("sno = ?"); vals.append(sno)
    if reference is not None:
        sets.append("reference = ?"); vals.append(reference)
    if prompt is not None:
        sets.append("prompt = ?"); vals.append(prompt)
    if active is not None:
        sets.append("active = ?"); vals.append(1 if active else 0)
    if is_section is not None:
        sets.append("is_section = ?"); vals.append(1 if is_section else 0)
    if not sets:
        return
    vals.append(item_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE checklist_items SET {', '.join(sets)} WHERE id = ?", vals
        )


def delete_item(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,))


def reorder_items(template_id: int, ordered_ids: list):
    with get_db() as conn:
        for pos, item_id in enumerate(ordered_ids, start=1):
            conn.execute(
                "UPDATE checklist_items SET position = ? "
                "WHERE id = ? AND template_id = ?",
                (pos, item_id, template_id),
            )


def _needs_backfill() -> bool:
    with get_db() as conn:
        unfilled = conn.execute(
            """
            SELECT 1 FROM templates t
            WHERE blob IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM checklist_items c
                  WHERE c.template_id = t.id AND c.reference <> ''
              )
            LIMIT 1
            """
        ).fetchone()
    return unfilled is not None


def backfill_references():
    if not _needs_backfill():
        return
    from reports.xlsx_builder import parse_xlsx
    with get_db() as conn:
        tmpls = conn.execute(
            "SELECT id, blob FROM templates WHERE blob IS NOT NULL"
        ).fetchall()
        for t in tmpls:
            tid, blob = t["id"], t["blob"]
            has_refs = conn.execute(
                "SELECT 1 FROM checklist_items WHERE template_id = ? AND reference <> '' LIMIT 1",
                (tid,),
            ).fetchone()
            if has_refs:
                continue
            try:
                parsed_items, _, _ = parse_xlsx(bytes(blob))
            except Exception:
                continue
            ref_map = {
                (it.get("sno", ""), it.get("text", "")): it.get("reference", "")
                for it in parsed_items
            }
            db_items = conn.execute(
                "SELECT id, sno, text FROM checklist_items WHERE template_id = ?",
                (tid,),
            ).fetchall()
            for row in db_items:
                ref = ref_map.get((row["sno"], row["text"]), "")
                if ref:
                    conn.execute(
                        "UPDATE checklist_items SET reference = ? WHERE id = ?",
                        (ref, row["id"]),
                    )


def backfill_prompts():
    with get_db() as conn:
        empty = conn.execute(
            "SELECT 1 FROM checklist_items WHERE is_section = 0 "
            "AND (prompt IS NULL OR prompt = '') LIMIT 1"
        ).fetchone()
        if not empty:
            return
        rows = conn.execute(
            "SELECT id, text FROM checklist_items "
            "WHERE is_section = 0 AND (prompt IS NULL OR prompt = '')"
        ).fetchall()
        for row in rows:
            default_prompt = (
                f"Verify that the '{row['text']}' document is present, "
                "valid, and matches the agreement details."
            )
            conn.execute(
                "UPDATE checklist_items SET prompt = ? WHERE id = ?",
                (default_prompt, row["id"]),
            )


init_templates_db()
backfill_references()
backfill_prompts()
