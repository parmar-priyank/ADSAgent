"""
templates_db.py — storage for QC checklist templates.

Two tables (in the same SQLite database as the rest of the app):
  - templates       : one row per named template (id, name, raw .xlsx blob,
                      header labels, created_at). The blob preserves the
                      originally-uploaded formatting for reference.
  - checklist_items : many rows per template (the editable checklist). Items
                      are the source of truth for editing and for regenerating
                      the downloadable Excel.

The structured items support add / edit / delete / reorder; ordering is held
by the `position` column. Sub-points (i, ii, iii) nest under a numbered parent
via `parent_position`.
"""
import sqlite3

from database import get_db


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
        # Migrations: add columns to older databases that don't have them.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(checklist_items)")]
        if "reference" not in cols:
            conn.execute("ALTER TABLE checklist_items ADD COLUMN reference TEXT DEFAULT ''")
        if "prompt" not in cols:
            conn.execute("ALTER TABLE checklist_items ADD COLUMN prompt TEXT DEFAULT ''")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def create_template(name: str, blob: bytes, items: list,
                    note_text: str = "") -> int:
    """
    Create a named template plus its checklist items.
    If a template with the same name already exists it is replaced, so
    re-uploading the same checklist never creates duplicates.
    """
    with get_db() as conn:
        # Delete any existing template with the same name (cascades to items).
        conn.execute("DELETE FROM templates WHERE LOWER(name) = LOWER(?)", (name,))
        cur = conn.execute(
            "INSERT INTO templates (name, blob, note_text) VALUES (?,?,?)",
            (name, blob, note_text),
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
        # shift existing items at/after position down by 1
        conn.execute(
            "UPDATE checklist_items SET position = position + 1 "
            "WHERE template_id = ? AND position >= ?",
            (template_id, position),
        )
        default_prompt = "" if is_section else f"Verify that the '{text}' document is present, valid, and matches the agreement details."
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
    """Set position for each item by its order in ordered_ids (1-based)."""
    with get_db() as conn:
        for pos, item_id in enumerate(ordered_ids, start=1):
            conn.execute(
                "UPDATE checklist_items SET position = ? "
                "WHERE id = ? AND template_id = ?",
                (pos, item_id, template_id),
            )


def _needs_backfill() -> bool:
    """Return True only if there are templates whose items all have empty references."""
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
    """
    One-time migration: fill the reference column from the stored xlsx blob for
    templates that still have all-empty references. Skips immediately if nothing
    needs backfilling so it is cheap on every subsequent start.
    """
    if not _needs_backfill():
        return

    import excel as cx

    with get_db() as conn:
        templates = conn.execute(
            "SELECT id, blob FROM templates WHERE blob IS NOT NULL"
        ).fetchall()
        for t in templates:
            tid, blob = t["id"], t["blob"]
            has_refs = conn.execute(
                "SELECT 1 FROM checklist_items WHERE template_id = ? AND reference <> '' LIMIT 1",
                (tid,),
            ).fetchone()
            if has_refs:
                continue

            try:
                parsed_items, _, _ = cx.parse_xlsx(bytes(blob))
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
    """
    One-time migration: set a default prompt for every non-section item
    that has an empty prompt, so the AI verifier has something to work with.
    """
    with get_db() as conn:
        empty = conn.execute(
            "SELECT 1 FROM checklist_items WHERE is_section = 0 AND (prompt IS NULL OR prompt = '') LIMIT 1"
        ).fetchone()
        if not empty:
            return
        rows = conn.execute(
            "SELECT id, text FROM checklist_items WHERE is_section = 0 AND (prompt IS NULL OR prompt = '')"
        ).fetchall()
        for row in rows:
            default_prompt = f"Verify that the '{row['text']}' document is present, valid, and matches the agreement details."
            conn.execute(
                "UPDATE checklist_items SET prompt = ? WHERE id = ?",
                (default_prompt, row["id"]),
            )


# Create tables on import.
init_templates_db()
backfill_references()
backfill_prompts()