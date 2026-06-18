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
import os
import sqlite3
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "extractions.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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
        # Migration: add reference column to older databases that don't have it.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(checklist_items)")]
        if "reference" not in cols:
            conn.execute("ALTER TABLE checklist_items ADD COLUMN reference TEXT DEFAULT ''")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def create_template(name: str, blob: bytes, items: list,
                    note_text: str = "") -> int:
    """
    Create a named template plus its checklist items.
    `items` is a list of dicts: {position, sno, parent_position, is_section,
    text, active}.
    """
    with get_db() as conn:
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
                 text, reference, active)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                template_id,
                it.get("position"),
                it.get("sno", ""),
                it.get("parent_position"),
                1 if it.get("is_section") else 0,
                it.get("text", ""),
                it.get("reference", ""),
                1 if it.get("active", 1) else 0,
            ),
        )


def list_templates():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, note_text, created_at FROM templates ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_template(template_id: int):
    with get_db() as conn:
        t = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    return dict(t) if t else None


def get_template_blob(template_id: int):
    with get_db() as conn:
        r = conn.execute(
            "SELECT blob FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    return r["blob"] if r else None


def delete_template(template_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


def rename_template(template_id: int, name: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE templates SET name = ? WHERE id = ?", (name, template_id)
        )


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


def replace_items(template_id: int, items: list):
    """Wholesale replace a template's items (used by the editor on save)."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM checklist_items WHERE template_id = ?", (template_id,)
        )
        _insert_items(conn, template_id, items)


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
        _insert_items(conn, template_id, [{
            "position": position, "sno": sno, "parent_position": None,
            "is_section": is_section, "text": text, "reference": reference,
            "active": 1,
        }])


def update_item(item_id: int, text: str = None, sno: str = None,
                active: int = None, is_section: int = None,
                reference: str = None):
    sets, vals = [], []
    if text is not None:
        sets.append("text = ?"); vals.append(text)
    if sno is not None:
        sets.append("sno = ?"); vals.append(sno)
    if reference is not None:
        sets.append("reference = ?"); vals.append(reference)
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


# Create tables on import.
init_templates_db()