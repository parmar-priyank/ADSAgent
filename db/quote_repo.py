"""
db/quote_repo.py — SQLite data layer for quotes, line items, QC versions,
and the pending-PDF duplicate-confirmation flow.
"""
import secrets as _secrets

from db.connection import get_db
from db import blob_store

QUOTE_FIELDS = [
    # Customer & Quote Details
    "quote_number", "quote_valid_until", "customer_name", "contact_person",
    "billing_address", "delivery_address", "email", "phone", "roof_type",
    # Retailer Details
    "retailer_name", "retailer_contact_person", "retailer_postal_address",
    "retailer_street_address", "retailer_phone", "retailer_email",
    # Payment Details
    "system_price", "stc_incentive", "vic_rebate", "battery_rebate",
    "total_price", "deposit", "balance", "payment_terms", "install_date",
    "balance_due_date", "notes",
    # User-requested installation date (set by customer at upload time,
    # distinct from install_date which is AI-extracted from the PDF)
    "preferred_install_date",
]


def init_db():
    quote_cols = ",\n                ".join(f"{f} TEXT" for f in QUOTE_FIELDS)
    with get_db() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS quotes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                filename     TEXT,
                {quote_cols},
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_quote_number "
            "ON quotes(quote_number)"
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(quotes)").fetchall()]
        if "qc_excel" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN qc_excel BLOB")
        if "preferred_install_date" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN preferred_install_date TEXT")
        if "draft_email_results_json" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN draft_email_results_json TEXT")
        # Running Claude-token total for THIS quote's not-yet-consumed Verify
        # Email work, accumulated across however many /email-verify rounds
        # happen before the user clicks "Continue to Upload ZIP" (each round
        # re-renders the page fresh, so a single hidden field alone would
        # lose earlier rounds' tokens). Reset to 0 once a checklist run
        # actually consumes them, so a later Verify Email session for a
        # future run doesn't double-count old spend.
        if "draft_email_verify_input_tokens" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN draft_email_verify_input_tokens INTEGER DEFAULT 0")
        if "draft_email_verify_output_tokens" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN draft_email_verify_output_tokens INTEGER DEFAULT 0")
        # On-disk path to the latest QC Excel for this quote — supersedes the
        # in-DB qc_excel blob. Old rows keep their blob and read fine via the
        # disk-first-then-blob fallback in get_quote consumers.
        if "qc_excel_path" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN qc_excel_path TEXT")
        # Soft delete: a Team Leader's "Delete" only flags the row (is_deleted=1)
        # so it vanishes from every active view but stays in the DB. Only a
        # super admin, from the Deleted Records trash, can restore it or delete
        # it permanently. Every active-record read filters is_deleted = 0.
        if "is_deleted" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN is_deleted INTEGER DEFAULT 0")
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN deleted_at TIMESTAMP")
        if "deleted_by_user_id" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN deleted_by_user_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qc_versions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id      INTEGER NOT NULL,
                version       INTEGER NOT NULL,
                template_name TEXT,
                zip_filename  TEXT,
                yes_count     INTEGER DEFAULT 0,
                no_count      INTEGER DEFAULT 0,
                na_count      INTEGER DEFAULT 0,
                excel_blob    BLOB,
                rows_json     TEXT,
                confirmed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (quote_id) REFERENCES quotes(id) ON DELETE CASCADE
            )
            """
        )
        qcv_cols = [r[1] for r in conn.execute("PRAGMA table_info(qc_versions)").fetchall()]
        for col, defn in [
            ("rows_json",            "TEXT"),
            ("confirmed_by_user_id", "INTEGER"),
            ("status",               "TEXT DEFAULT 'confirmed'"),
            ("saved_by_user_id",     "INTEGER"),
            ("saved_at",             "TIMESTAMP"),
            ("email_results_json",   "TEXT DEFAULT ''"),
            # Which checklist stage this run was against — 'pre' for every
            # version that existed before Post-QC, hence the default.
            ("kind",                 "TEXT DEFAULT 'pre'"),
            # On-disk path to this version's Excel — supersedes excel_blob.
            # Old versions keep their blob; reads fall back to it when path
            # is NULL (lazy migration, no bulk rewrite).
            ("excel_path",           "TEXT"),
            # Total Claude tokens actually spent producing this version:
            # the checklist run (single-item + batched calls), any Verify
            # Email attachment matching, and any per-row re-analysis while
            # editing. NULL on versions saved before this tracking existed —
            # shown as "not tracked" rather than a misleading 0.
            ("input_tokens",         "INTEGER"),
            ("output_tokens",        "INTEGER"),
        ]:
            if col not in qcv_cols:
                conn.execute(f"ALTER TABLE qc_versions ADD COLUMN {col} {defn}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS post_qc_assignments (
                quote_id    INTEGER PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (quote_id) REFERENCES quotes(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS line_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_number  TEXT NOT NULL,
                position      INTEGER,
                item          TEXT,
                quantity      TEXT,
                specification TEXT,
                price         TEXT,
                FOREIGN KEY (quote_number)
                    REFERENCES quotes(quote_number) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_pdfs (
                token      TEXT PRIMARY KEY,
                filename   TEXT NOT NULL,
                pdf_bytes  BLOB NOT NULL,
                pdf_text   TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "DELETE FROM pending_pdfs "
            "WHERE created_at < datetime('now', '-1 hour')"
        )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def save_extraction(filename: str, data: dict) -> int:
    quote_number = (data.get("quote_number") or "").strip()
    _orphan_paths = []
    with get_db() as conn:
        if quote_number:
            # Re-uploading an existing quote number replaces whatever is there
            # — active OR soft-deleted (the UNIQUE index on quote_number would
            # otherwise block the insert). Collect any on-disk Excel files of
            # the rows we're about to remove so they don't leak.
            dup_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM quotes WHERE quote_number = ?", (quote_number,)
                ).fetchall()
            ]
            if dup_ids:
                ph = ",".join("?" * len(dup_ids))
                for r in conn.execute(
                    f"SELECT qc_excel_path FROM quotes WHERE id IN ({ph}) AND qc_excel_path IS NOT NULL",
                    dup_ids,
                ).fetchall():
                    _orphan_paths.append(r["qc_excel_path"])
                for r in conn.execute(
                    f"SELECT excel_path FROM qc_versions WHERE quote_id IN ({ph}) AND excel_path IS NOT NULL",
                    dup_ids,
                ).fetchall():
                    _orphan_paths.append(r["excel_path"])
            conn.execute("DELETE FROM quotes WHERE quote_number = ?", (quote_number,))
        columns = ["filename"] + QUOTE_FIELDS
        placeholders = ",".join("?" for _ in columns)
        values = [filename] + [
            quote_number if f == "quote_number" else (data.get(f, "") or "")
            for f in QUOTE_FIELDS
        ]
        cur = conn.execute(
            f"INSERT INTO quotes ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        quote_id = cur.lastrowid
        for pos, li in enumerate(data.get("line_items") or []):
            conn.execute(
                """
                INSERT INTO line_items
                    (quote_number, position, item, quantity, specification, price)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    quote_number, pos,
                    li.get("item", ""), li.get("quantity", ""),
                    li.get("specification", ""), li.get("price", ""),
                ),
            )
    # Remove any on-disk Excel files that belonged to the rows we replaced,
    # after the DB write has committed.
    for p in _orphan_paths:
        blob_store.delete_excel(p)
    return quote_id


def _attach_line_items(conn, quotes: list) -> list:
    if not quotes:
        return quotes
    numbers = [q["quote_number"] for q in quotes]
    placeholders = ",".join("?" * len(numbers))
    rows = conn.execute(
        f"SELECT quote_number, item, quantity, specification, price "
        f"FROM line_items WHERE quote_number IN ({placeholders}) ORDER BY position ASC",
        numbers,
    ).fetchall()
    items_by_qn: dict[str, list] = {}
    for r in rows:
        items_by_qn.setdefault(r["quote_number"], []).append(dict(r))
    for q in quotes:
        q["line_items"] = items_by_qn.get(q["quote_number"], [])
        q["data"] = {**{f: q.get(f, "") for f in QUOTE_FIELDS}, "line_items": q["line_items"]}
    return quotes


def get_quote(quote_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if not row:
            return None
        return _attach_line_items(conn, [dict(row)])[0]


def update_preferred_install_date(quote_id: int, value: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE quotes SET preferred_install_date = ? WHERE id = ?",
            (value, quote_id),
        )


def save_draft_email_results(quote_id: int, results_json: str):
    """Persist Step 2 (Verify Email)'s analyzed attachment results against the
    quote, so reopening that quote's Verify Email step later restores the
    same Yes/No/remarks without re-uploading the .eml file(s)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE quotes SET draft_email_results_json = ? WHERE id = ?",
            (results_json, quote_id),
        )


def get_draft_email_results(quote_id: int) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT draft_email_results_json FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()
    return (row["draft_email_results_json"] if row else "") or ""


def add_draft_email_verify_tokens(quote_id: int, input_tokens: int, output_tokens: int) -> tuple[int, int]:
    """Add this /email-verify round's token spend to the quote's running,
    not-yet-consumed total (read-add-write, so multiple rounds before the
    user clicks "Continue to Upload ZIP" all get counted). Returns the new
    running total, which the caller passes forward via a hidden form field."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(draft_email_verify_input_tokens, 0) as i, "
            "COALESCE(draft_email_verify_output_tokens, 0) as o "
            "FROM quotes WHERE id = ?", (quote_id,),
        ).fetchone()
        new_in  = (row["i"] if row else 0) + (input_tokens or 0)
        new_out = (row["o"] if row else 0) + (output_tokens or 0)
        conn.execute(
            "UPDATE quotes SET draft_email_verify_input_tokens = ?, "
            "draft_email_verify_output_tokens = ? WHERE id = ?",
            (new_in, new_out, quote_id),
        )
    return new_in, new_out


def take_draft_email_verify_tokens(quote_id: int) -> tuple[int, int]:
    """Read and reset the quote's running Verify Email token total to 0 —
    called once a checklist run actually consumes it, so a later Verify
    Email session for a future run starts from zero instead of double-
    counting spend a previous run already accounted for."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(draft_email_verify_input_tokens, 0) as i, "
            "COALESCE(draft_email_verify_output_tokens, 0) as o "
            "FROM quotes WHERE id = ?", (quote_id,),
        ).fetchone()
        conn.execute(
            "UPDATE quotes SET draft_email_verify_input_tokens = 0, "
            "draft_email_verify_output_tokens = 0 WHERE id = ?",
            (quote_id,),
        )
    return (row["i"], row["o"]) if row else (0, 0)


def count_quotes() -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM quotes WHERE COALESCE(is_deleted, 0) = 0"
        ).fetchone()[0]


def get_recent(limit: int | None = 20):
    """limit=None returns every quote record — used by the Customer Records
    page, which must show the full list, not just the most recent ones.
    Soft-deleted records are excluded; they live in the super-admin trash."""
    with get_db() as conn:
        if limit is None:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE COALESCE(is_deleted, 0) = 0 ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE COALESCE(is_deleted, 0) = 0 "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        quotes = _attach_line_items(conn, [dict(r) for r in rows])
        if quotes:
            ids = [q["id"] for q in quotes]
            placeholders = ",".join("?" * len(ids))
            counts = conn.execute(
                f"SELECT quote_id, COUNT(*) as cnt FROM qc_versions "
                f"WHERE quote_id IN ({placeholders}) GROUP BY quote_id",
                ids,
            ).fetchall()
            count_map = {r["quote_id"]: r["cnt"] for r in counts}
            kind_counts = conn.execute(
                f"SELECT quote_id, COALESCE(kind, 'pre') as kind, COUNT(*) as cnt "
                f"FROM qc_versions WHERE quote_id IN ({placeholders}) GROUP BY quote_id, kind",
                ids,
            ).fetchall()
            pre_count_map: dict = {}
            post_count_map: dict = {}
            for r in kind_counts:
                if r["kind"] == "post":
                    post_count_map[r["quote_id"]] = r["cnt"]
                else:
                    pre_count_map[r["quote_id"]] = r["cnt"]

            # Latest version's status per (quote, kind) — 'draft' or
            # 'confirmed' — so Customer Records can filter/show Draft
            # distinctly from Confirmed instead of lumping both into "Done".
            latest_status_rows = conn.execute(
                f"""
                SELECT qv.quote_id, COALESCE(qv.kind, 'pre') as kind,
                       COALESCE(qv.status, 'confirmed') as status
                FROM qc_versions qv
                JOIN (
                    SELECT quote_id, COALESCE(kind, 'pre') as kind, MAX(version) as max_version
                    FROM qc_versions WHERE quote_id IN ({placeholders})
                    GROUP BY quote_id, kind
                ) latest
                  ON latest.quote_id = qv.quote_id
                 AND COALESCE(qv.kind, 'pre') = latest.kind
                 AND qv.version = latest.max_version
                """,
                ids,
            ).fetchall()
            pre_status_map: dict = {}
            post_status_map: dict = {}
            for r in latest_status_rows:
                target = post_status_map if r["kind"] == "post" else pre_status_map
                target[r["quote_id"]] = r["status"]

            assignees = conn.execute(
                f"SELECT pqa.quote_id, pqa.user_id, u.username "
                f"FROM post_qc_assignments pqa JOIN users u ON u.id = pqa.user_id "
                f"WHERE pqa.quote_id IN ({placeholders})",
                ids,
            ).fetchall()
            assignee_map = {r["quote_id"]: {"user_id": r["user_id"], "username": r["username"]} for r in assignees}
            for q in quotes:
                q["qc_version_count"] = count_map.get(q["id"], 0)
                q["pre_qc_count"] = pre_count_map.get(q["id"], 0)
                q["post_qc_count"] = post_count_map.get(q["id"], 0)
                q["post_qc_assignee"] = assignee_map.get(q["id"])
                # 'none' | 'draft' | 'confirmed' — the latest version's real
                # status per kind, distinct from the plain has-any-run count.
                q["pre_qc_status"] = pre_status_map.get(q["id"], "none")
                q["post_qc_status"] = post_status_map.get(q["id"], "none")
    for display_num, q in enumerate(quotes, start=1):
        q["display_num"] = display_num
    return quotes


def find_by_quote_number(quote_number: str):
    if not quote_number:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE quote_number = ? ORDER BY id DESC LIMIT 1",
            (quote_number,),
        ).fetchone()
        if not row:
            return None
        return _attach_line_items(conn, [dict(row)])[0]


def soft_delete_quote(quote_id: int, deleted_by_user_id: int = None) -> bool:
    """Flag a quote as deleted (a Team Leader's "Delete") without removing it.
    It disappears from every active view but stays in the DB, visible only in
    the super-admin Deleted Records trash. Returns False if the quote doesn't
    exist or is already deleted."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE quotes SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP, "
            "deleted_by_user_id = ? WHERE id = ? AND COALESCE(is_deleted, 0) = 0",
            (deleted_by_user_id, quote_id),
        )
        return cur.rowcount > 0


def restore_quote(quote_id: int) -> bool:
    """Undo a soft delete — bring the quote back into every active view.
    Super-admin only. Returns False if the quote isn't currently deleted."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE quotes SET is_deleted = 0, deleted_at = NULL, "
            "deleted_by_user_id = NULL WHERE id = ? AND COALESCE(is_deleted, 0) = 1",
            (quote_id,),
        )
        return cur.rowcount > 0


def get_deleted_quotes() -> list:
    """All soft-deleted quotes, newest-deleted first — powers the super-admin
    Deleted Records trash page. Includes who deleted it and when."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT q.*, u.username AS deleted_by_username, u.full_name AS deleted_by_full_name
            FROM quotes q
            LEFT JOIN users u ON u.id = q.deleted_by_user_id
            WHERE COALESCE(q.is_deleted, 0) = 1
            ORDER BY q.deleted_at DESC, q.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def delete_quote(quote_id: int):
    """PERMANENT delete — removes the quote, its QC versions (cascade), and all
    associated on-disk Excel files. Super-admin only (from the trash page).
    A Team Leader's Delete goes through soft_delete_quote instead."""
    # Gather every on-disk Excel this quote owns (its own latest copy + one per
    # QC version) so the files can be removed after the DB rows are gone. The
    # qc_versions rows cascade-delete with the quote, so collect paths first.
    with get_db() as conn:
        paths = []
        q = conn.execute(
            "SELECT qc_excel_path FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()
        if q and q["qc_excel_path"]:
            paths.append(q["qc_excel_path"])
        for r in conn.execute(
            "SELECT excel_path FROM qc_versions WHERE quote_id = ? AND excel_path IS NOT NULL",
            (quote_id,),
        ).fetchall():
            if r["excel_path"]:
                paths.append(r["excel_path"])
        conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
    for p in paths:
        blob_store.delete_excel(p)


# ---------------------------------------------------------------------------
# QC Excel snapshots
# ---------------------------------------------------------------------------

def save_qc_excel(quote_id: int, xlsx_bytes: bytes):
    """Store the latest QC Excel for this quote on disk (not in the DB), and
    remove any previous on-disk file for this quote so old copies don't pile
    up. The legacy in-DB qc_excel blob is cleared for rows we newly write."""
    new_path = blob_store.save_excel(xlsx_bytes, kind="quote", ident=quote_id)
    with get_db() as conn:
        old = conn.execute(
            "SELECT qc_excel_path FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()
        conn.execute(
            "UPDATE quotes SET qc_excel_path = ?, qc_excel = NULL WHERE id = ?",
            (new_path, quote_id),
        )
    if old and old["qc_excel_path"]:
        blob_store.delete_excel(old["qc_excel_path"])


def add_qc_version(
    quote_id: int,
    xlsx_bytes: bytes,
    template_name: str = "",
    zip_filename: str = "",
    yes_count: int = 0,
    no_count: int = 0,
    na_count: int = 0,
    rows_json: str = "",
    email_results_json: str = "",
    confirmed_by_user_id: int = None,
    status: str = "confirmed",
    saved_by_user_id: int = None,
    kind: str = "pre",
    input_tokens: int = None,
    output_tokens: int = None,
) -> tuple:
    if kind not in ("pre", "post"):
        kind = "pre"
    # Excel goes to disk, not into the DB — excel_blob stays NULL for new rows.
    excel_path = blob_store.save_excel(xlsx_bytes, kind="version", ident=quote_id) if xlsx_bytes else None
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM qc_versions WHERE quote_id = ?",
            (quote_id,),
        ).fetchone()
        next_version = (row[0] or 0) + 1
        conn.execute(
            """
            INSERT INTO qc_versions
                (quote_id, version, template_name, zip_filename,
                 yes_count, no_count, na_count, excel_blob, excel_path, rows_json,
                 email_results_json, confirmed_by_user_id, status, saved_by_user_id, saved_at, kind,
                 input_tokens, output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
            """,
            (quote_id, next_version, template_name, zip_filename,
             yes_count, no_count, na_count, excel_path, rows_json,
             email_results_json or "", confirmed_by_user_id, status, saved_by_user_id, kind,
             input_tokens, output_tokens),
        )
        version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return version_id, next_version


def update_qc_version(version_id: int, xlsx_bytes: bytes, rows_json: str,
                      yes_count: int, no_count: int, na_count: int,
                      confirm: bool = False, confirmed_by_user_id: int = None,
                      email_results_json: str = None,
                      input_tokens: int = None, output_tokens: int = None):
    """email_results_json is optional — pass None (default) to leave the
    version's stored email-verification results untouched, or a JSON string
    to overwrite them (used when the Email Verification table was edited
    alongside the checklist rows).

    input_tokens/output_tokens are the TOTAL tokens spent on this version to
    date (the browser accumulates the running total across the initial run
    plus any per-row re-analysis edits, same as it already does for
    yes_count/no_count) — pass None (default) to leave the stored total
    untouched, or a number to overwrite it. Never added/subtracted here;
    the caller always sends the full current total.

    The rebuilt Excel is written to disk (excel_path) and excel_blob is
    cleared; any previous on-disk file for this version is removed afterward,
    and a previous in-DB blob is dropped by the excel_blob=NULL set."""
    # Look up the current on-disk path first so we can delete it after a
    # successful update (a fresh file is written below with a new name).
    with get_db() as conn:
        prev = conn.execute(
            "SELECT quote_id, excel_path FROM qc_versions WHERE id = ?", (version_id,)
        ).fetchone()
    quote_id_for_name = prev["quote_id"] if prev else version_id
    old_path = prev["excel_path"] if prev else None
    new_path = blob_store.save_excel(xlsx_bytes, kind="version", ident=quote_id_for_name) if xlsx_bytes else None

    set_tokens_sql = ""
    token_params: tuple = ()
    if input_tokens is not None or output_tokens is not None:
        set_tokens_sql = ", input_tokens=?, output_tokens=?"
        token_params = (input_tokens, output_tokens)

    with get_db() as conn:
        if email_results_json is not None:
            if confirm:
                conn.execute(
                    f"""
                    UPDATE qc_versions
                       SET excel_blob=NULL, excel_path=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                           email_results_json=?,
                           status='confirmed', confirmed_at=CURRENT_TIMESTAMP,
                           confirmed_by_user_id=?{set_tokens_sql}
                     WHERE id=?
                    """,
                    (new_path, rows_json, yes_count, no_count, na_count,
                     email_results_json, confirmed_by_user_id, *token_params, version_id),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE qc_versions
                       SET excel_blob=NULL, excel_path=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                           email_results_json=?,
                           status='draft'{set_tokens_sql}
                     WHERE id=?
                    """,
                    (new_path, rows_json, yes_count, no_count, na_count,
                     email_results_json, *token_params, version_id),
                )
        elif confirm:
            conn.execute(
                f"""
                UPDATE qc_versions
                   SET excel_blob=NULL, excel_path=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                       status='confirmed', confirmed_at=CURRENT_TIMESTAMP,
                       confirmed_by_user_id=?{set_tokens_sql}
                 WHERE id=?
                """,
                (new_path, rows_json, yes_count, no_count, na_count,
                 confirmed_by_user_id, *token_params, version_id),
            )
        else:
            conn.execute(
                f"""
                UPDATE qc_versions
                   SET excel_blob=NULL, excel_path=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                       status='draft'{set_tokens_sql}
                 WHERE id=?
                """,
                (new_path, rows_json, yes_count, no_count, na_count, *token_params, version_id),
            )

    if old_path and old_path != new_path:
        blob_store.delete_excel(old_path)


def get_confirmed_quote_ids() -> set:
    """Return the set of quote_ids that have at least one confirmed QC
    version — used to filter the admin calendar to confirmed installs only,
    excluding drafts and quotes with no QC run yet."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT qv.quote_id FROM qc_versions qv "
            "JOIN quotes q ON q.id = qv.quote_id "
            "WHERE COALESCE(qv.status, 'confirmed') = 'confirmed' "
            "AND COALESCE(q.is_deleted, 0) = 0"
        ).fetchall()
    return {r["quote_id"] for r in rows}


def get_qc_analysis_months() -> list:
    """Distinct 'YYYY-MM' months that have at least one confirmed QC version,
    newest first — powers the Analysis page's month picker. Excludes versions
    belonging to soft-deleted quotes."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT strftime('%Y-%m', qv.confirmed_at) as ym FROM qc_versions qv "
            "JOIN quotes q ON q.id = qv.quote_id "
            "WHERE COALESCE(qv.status, 'confirmed') = 'confirmed' AND qv.confirmed_at IS NOT NULL "
            "AND COALESCE(q.is_deleted, 0) = 0 "
            "ORDER BY ym DESC"
        ).fetchall()
    return [r["ym"] for r in rows if r["ym"]]


def get_qc_monthly_stats(year_month: str) -> dict:
    """Confirmed QC throughput for one 'YYYY-MM' month: total, pre/post
    split, and a per-technician breakdown (who confirmed how many, of each
    kind) — used by the super-admin Analysis page to track team throughput."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT qv.id, qv.quote_id, COALESCE(qv.kind, 'pre') as kind,
                   qv.confirmed_at, qv.confirmed_by_user_id,
                   qv.input_tokens, qv.output_tokens,
                   u.username, u.full_name,
                   q.customer_name, q.quote_number
            FROM qc_versions qv
            LEFT JOIN users u ON u.id = qv.confirmed_by_user_id
            JOIN quotes q ON q.id = qv.quote_id
            WHERE COALESCE(qv.status, 'confirmed') = 'confirmed'
              AND COALESCE(q.is_deleted, 0) = 0
              AND strftime('%Y-%m', qv.confirmed_at) = ?
            ORDER BY qv.confirmed_at DESC
            """,
            (year_month,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    pre_count  = sum(1 for r in rows if r["kind"] == "pre")
    post_count = sum(1 for r in rows if r["kind"] == "post")
    total_input_tokens  = sum(r.get("input_tokens")  or 0 for r in rows)
    total_output_tokens = sum(r.get("output_tokens") or 0 for r in rows)
    # Token tracking was added after some already-confirmed versions existed,
    # so their input_tokens/output_tokens are NULL (untracked), not 0 — these
    # counts let the Analysis page say "N of M runs tracked" instead of
    # implying a summed $0.00 is the true cost when it's really "no data yet"
    # for most of the month.
    tracked_count      = sum(1 for r in rows if r.get("input_tokens") is not None)
    pre_tracked_count  = sum(1 for r in rows if r["kind"] == "pre"  and r.get("input_tokens") is not None)
    post_tracked_count = sum(1 for r in rows if r["kind"] == "post" and r.get("input_tokens") is not None)
    pre_input_tokens   = sum(r.get("input_tokens")  or 0 for r in rows if r["kind"] == "pre")
    pre_output_tokens  = sum(r.get("output_tokens") or 0 for r in rows if r["kind"] == "pre")
    post_input_tokens  = sum(r.get("input_tokens")  or 0 for r in rows if r["kind"] == "post")
    post_output_tokens = sum(r.get("output_tokens") or 0 for r in rows if r["kind"] == "post")

    # Full team roster: every active non-super-admin account appears in the
    # per-member breakdown even with zero runs this month, so the table reads
    # as the whole team, not just whoever happened to confirm something.
    # Super-admin accounts never appear here (per the privacy rule); their
    # runs still count in the month totals and daily chart above.
    with get_db() as conn:
        roster = conn.execute(
            "SELECT id, username, full_name, COALESCE(is_super_admin, 0) AS is_super_admin, "
            "COALESCE(is_active, 1) AS is_active FROM users"
        ).fetchall()
    roster = [dict(r) for r in roster]
    super_admin_ids = {r["id"] for r in roster if r["is_super_admin"]}

    by_user = {}
    for r in roster:
        if r["is_super_admin"] or not r["is_active"]:
            continue
        by_user[r["id"]] = {
            "user_id": r["id"],
            "label": (r["full_name"] or r["username"] or "Unknown"),
            "username": r["username"] or "",
            "pre": 0, "post": 0, "total": 0,
        }

    for r in rows:
        uid = r.get("confirmed_by_user_id")
        if uid in super_admin_ids:
            continue
        key = uid if uid is not None else "unknown"
        if key not in by_user:
            # Deactivated or deleted account that still has runs this month —
            # keep its row so the numbers stay attributable.
            by_user[key] = {
                "user_id": uid,
                "label": (r.get("full_name") or r.get("username") or "Unknown"),
                "username": r.get("username") or "",
                "pre": 0, "post": 0, "total": 0,
            }
        by_user[key]["total"] += 1
        if r["kind"] == "post":
            by_user[key]["post"] += 1
        else:
            by_user[key]["pre"] += 1

    per_user = sorted(by_user.values(), key=lambda x: (-x["total"], x["label"].lower()))

    # One entry per calendar day of the month (even zero-count days), so the
    # Analysis page's daily trend chart has a consistent, gap-free x-axis.
    import calendar as _calendar
    y, m = (int(p) for p in year_month.split("-"))
    days_in_month = _calendar.monthrange(y, m)[1]
    by_day = {
        f"{year_month}-{d:02d}": {"day": d, "pre": 0, "post": 0, "total": 0}
        for d in range(1, days_in_month + 1)
    }
    for r in rows:
        confirmed_at = r.get("confirmed_at") or ""
        day_key = confirmed_at[:10]
        if day_key in by_day:
            by_day[day_key]["total"] += 1
            if r["kind"] == "post":
                by_day[day_key]["post"] += 1
            else:
                by_day[day_key]["pre"] += 1
    daily = [by_day[k] for k in sorted(by_day.keys())]

    return {
        "year_month": year_month,
        "total": len(rows),
        "pre_count": pre_count,
        "post_count": post_count,
        "per_user": per_user,
        "runs": rows,
        "daily": daily,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "pre_input_tokens": pre_input_tokens,
        "pre_output_tokens": pre_output_tokens,
        "post_input_tokens": post_input_tokens,
        "post_output_tokens": post_output_tokens,
        "tracked_count": tracked_count,
        "pre_tracked_count": pre_tracked_count,
        "post_tracked_count": post_tracked_count,
    }


def get_qc_versions(quote_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, version, template_name, zip_filename,
                   yes_count, no_count, na_count, confirmed_at, saved_at,
                   COALESCE(status, 'confirmed') as status,
                   COALESCE(kind, 'pre') as kind
            FROM qc_versions WHERE quote_id = ?
            ORDER BY version DESC
            """,
            (quote_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_qc_version(quote_id: int, kind: str):
    """Latest (highest version number) qc_versions row of a given kind for
    this quote, or None if that kind has no run yet — used to pull in the
    "other" kind's checklist alongside whichever version an admin is
    currently viewing, so both Pre-QC and Post-QC results can be shown on
    one page instead of two separate ones."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM qc_versions WHERE quote_id = ? AND COALESCE(kind, 'pre') = ? "
            "ORDER BY version DESC LIMIT 1",
            (quote_id, kind),
        ).fetchone()
    return dict(row) if row else None


def get_quote_qc_excel(quote_id: int) -> bytes | None:
    """Latest QC Excel for a quote — prefer the on-disk file (qc_excel_path),
    fall back to the legacy in-DB qc_excel blob for older rows."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT qc_excel_path, qc_excel FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()
    if not row:
        return None
    from_disk = blob_store.read_excel(row["qc_excel_path"])
    if from_disk is not None:
        return from_disk
    return bytes(row["qc_excel"]) if row["qc_excel"] else None


def quote_has_qc_excel(quote_id: int) -> bool:
    """True if a quote has a QC Excel available either on disk or as a legacy
    in-DB blob — used for the 'download available?' flag without loading bytes."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT qc_excel_path, (qc_excel IS NOT NULL) AS has_blob FROM quotes WHERE id = ?",
            (quote_id,),
        ).fetchone()
    if not row:
        return False
    return bool(row["qc_excel_path"]) or bool(row["has_blob"])


def get_qc_version_excel(version_id: int) -> bytes | None:
    """Prefer the on-disk file (excel_path); fall back to the legacy in-DB
    blob for older rows that predate the filesystem migration."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT excel_path, excel_blob FROM qc_versions WHERE id = ?", (version_id,)
        ).fetchone()
    if not row:
        return None
    from_disk = blob_store.read_excel(row["excel_path"])
    if from_disk is not None:
        return from_disk
    return bytes(row["excel_blob"]) if row["excel_blob"] else None


def get_qc_history_by_user(user_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT qv.id, qv.version, qv.template_name, qv.zip_filename,
                   qv.yes_count, qv.no_count, qv.na_count,
                   qv.status, qv.confirmed_at, qv.saved_at,
                   qv.quote_id, COALESCE(qv.kind, 'pre') as kind,
                   q.customer_name, q.quote_number,
                   q.install_date, q.total_price
            FROM qc_versions qv
            JOIN quotes q ON q.id = qv.quote_id
            WHERE (qv.saved_by_user_id = ? OR qv.confirmed_by_user_id = ?)
              AND COALESCE(q.is_deleted, 0) = 0
            ORDER BY COALESCE(qv.confirmed_at, qv.saved_at) DESC
            """,
            (user_id, user_id),
        ).fetchall()
    return [dict(r) for r in rows]


# Alias kept for any call sites that use the old name
get_qc_history_for_user = get_qc_history_by_user


# ---------------------------------------------------------------------------
# Post-QC customer assignment
# ---------------------------------------------------------------------------

def assign_post_qc(quote_id: int, user_id: int):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO post_qc_assignments (quote_id, user_id, assigned_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(quote_id) DO UPDATE SET user_id = excluded.user_id, "
            "assigned_at = CURRENT_TIMESTAMP",
            (quote_id, user_id),
        )


def unassign_post_qc(quote_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM post_qc_assignments WHERE quote_id = ?", (quote_id,))


def get_post_qc_assignee(quote_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT pqa.user_id, u.username FROM post_qc_assignments pqa "
            "JOIN users u ON u.id = pqa.user_id WHERE pqa.quote_id = ?",
            (quote_id,),
        ).fetchone()
    return dict(row) if row else None


def get_assigned_quotes_for_user(user_id: int) -> list:
    """Customers assigned to this user for Post-QC, most recently assigned
    first — the list a Post-QC user sees on their home page."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT q.*, pqa.assigned_at
            FROM post_qc_assignments pqa
            JOIN quotes q ON q.id = pqa.quote_id
            WHERE pqa.user_id = ? AND COALESCE(q.is_deleted, 0) = 0
            ORDER BY pqa.assigned_at DESC
            """,
            (user_id,),
        ).fetchall()
        quotes = _attach_line_items(conn, [dict(r) for r in rows])
        if quotes:
            ids = [q["id"] for q in quotes]
            placeholders = ",".join("?" * len(ids))
            kind_counts = conn.execute(
                f"SELECT quote_id, COALESCE(kind, 'pre') as kind, COUNT(*) as cnt "
                f"FROM qc_versions WHERE quote_id IN ({placeholders}) GROUP BY quote_id, kind",
                ids,
            ).fetchall()
            post_count_map: dict = {}
            for r in kind_counts:
                if r["kind"] == "post":
                    post_count_map[r["quote_id"]] = r["cnt"]
            for q in quotes:
                q["post_qc_count"] = post_count_map.get(q["id"], 0)
    return quotes


def get_quotes_pending_post_qc() -> list:
    """Every customer with a confirmed Pre-QC but no confirmed Post-QC yet,
    most recently confirmed first — the admin/super-admin view of "what
    Post-QC work is left", independent of per-user assignment (unlike
    get_assigned_quotes_for_user, which only shows a technician their own
    assigned customers)."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT q.*, MAX(qv.confirmed_at) AS pre_confirmed_at
            FROM qc_versions qv
            JOIN quotes q ON q.id = qv.quote_id
            WHERE COALESCE(qv.kind, 'pre') = 'pre'
              AND COALESCE(qv.status, 'confirmed') = 'confirmed'
              AND COALESCE(q.is_deleted, 0) = 0
              AND q.id NOT IN (
                  SELECT quote_id FROM qc_versions
                  WHERE COALESCE(kind, 'pre') = 'post'
                    AND COALESCE(status, 'confirmed') = 'confirmed'
              )
            GROUP BY q.id
            ORDER BY pre_confirmed_at DESC
            """
        ).fetchall()
        if not rows:
            return []
        quotes = _attach_line_items(conn, [dict(r) for r in rows])
        # Always 0 by construction (the query already excludes anyone with a
        # confirmed post version) — set explicitly so the shared template's
        # r['post_qc_count'] > 0 check has a real value, not a missing key.
        for q in quotes:
            q["post_qc_count"] = 0
        return quotes


# ---------------------------------------------------------------------------
# Pending-PDF helpers (duplicate-confirmation flow)
# ---------------------------------------------------------------------------

def store_pending_pdf(filename: str, pdf_bytes: bytes, pdf_text: str) -> str:
    token = _secrets.token_urlsafe(24)
    with get_db() as conn:
        conn.execute(
            "DELETE FROM pending_pdfs WHERE created_at < datetime('now', '-1 hour')"
        )
        conn.execute(
            "INSERT INTO pending_pdfs (token, filename, pdf_bytes, pdf_text) "
            "VALUES (?, ?, ?, ?)",
            (token, filename, pdf_bytes, pdf_text),
        )
    return token


def pop_pending_pdf(token: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT filename, pdf_bytes, pdf_text FROM pending_pdfs WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_pdfs WHERE token = ?", (token,))
    return {
        "filename": row["filename"],
        "pdf_bytes": bytes(row["pdf_bytes"]),
        "pdf_text": row["pdf_text"],
    }


init_db()
