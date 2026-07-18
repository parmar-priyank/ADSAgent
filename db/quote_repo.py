"""
db/quote_repo.py — SQLite data layer for quotes, line items, QC versions,
and the pending-PDF duplicate-confirmation flow.
"""
import secrets as _secrets

from db.connection import get_db

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
    with get_db() as conn:
        if quote_number:
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


def count_quotes() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]


def get_recent(limit: int | None = 20):
    """limit=None returns every quote record — used by the Customer Records
    page, which must show the full list, not just the most recent ones."""
    with get_db() as conn:
        if limit is None:
            rows = conn.execute("SELECT * FROM quotes ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quotes ORDER BY id DESC LIMIT ?", (limit,)
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


def delete_quote(quote_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))


# ---------------------------------------------------------------------------
# QC Excel snapshots
# ---------------------------------------------------------------------------

def save_qc_excel(quote_id: int, xlsx_bytes: bytes):
    with get_db() as conn:
        conn.execute(
            "UPDATE quotes SET qc_excel = ? WHERE id = ?", (xlsx_bytes, quote_id)
        )


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
) -> tuple:
    if kind not in ("pre", "post"):
        kind = "pre"
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
                 yes_count, no_count, na_count, excel_blob, rows_json,
                 email_results_json, confirmed_by_user_id, status, saved_by_user_id, saved_at, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """,
            (quote_id, next_version, template_name, zip_filename,
             yes_count, no_count, na_count, xlsx_bytes, rows_json,
             email_results_json or "", confirmed_by_user_id, status, saved_by_user_id, kind),
        )
        version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return version_id, next_version


def update_qc_version(version_id: int, xlsx_bytes: bytes, rows_json: str,
                      yes_count: int, no_count: int, na_count: int,
                      confirm: bool = False, confirmed_by_user_id: int = None,
                      email_results_json: str = None):
    """email_results_json is optional — pass None (default) to leave the
    version's stored email-verification results untouched, or a JSON string
    to overwrite them (used when the Email Verification table was edited
    alongside the checklist rows)."""
    with get_db() as conn:
        if email_results_json is not None:
            if confirm:
                conn.execute(
                    """
                    UPDATE qc_versions
                       SET excel_blob=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                           email_results_json=?,
                           status='confirmed', confirmed_at=CURRENT_TIMESTAMP,
                           confirmed_by_user_id=?
                     WHERE id=?
                    """,
                    (xlsx_bytes, rows_json, yes_count, no_count, na_count,
                     email_results_json, confirmed_by_user_id, version_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE qc_versions
                       SET excel_blob=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                           email_results_json=?,
                           status='draft'
                     WHERE id=?
                    """,
                    (xlsx_bytes, rows_json, yes_count, no_count, na_count,
                     email_results_json, version_id),
                )
        elif confirm:
            conn.execute(
                """
                UPDATE qc_versions
                   SET excel_blob=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                       status='confirmed', confirmed_at=CURRENT_TIMESTAMP,
                       confirmed_by_user_id=?
                 WHERE id=?
                """,
                (xlsx_bytes, rows_json, yes_count, no_count, na_count,
                 confirmed_by_user_id, version_id),
            )
        else:
            conn.execute(
                """
                UPDATE qc_versions
                   SET excel_blob=?, rows_json=?, yes_count=?, no_count=?, na_count=?,
                       status='draft'
                 WHERE id=?
                """,
                (xlsx_bytes, rows_json, yes_count, no_count, na_count, version_id),
            )


def get_confirmed_quote_ids() -> set:
    """Return the set of quote_ids that have at least one confirmed QC
    version — used to filter the admin calendar to confirmed installs only,
    excluding drafts and quotes with no QC run yet."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT quote_id FROM qc_versions "
            "WHERE COALESCE(status, 'confirmed') = 'confirmed'"
        ).fetchall()
    return {r["quote_id"] for r in rows}


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


def get_qc_version_excel(version_id: int) -> bytes | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT excel_blob FROM qc_versions WHERE id = ?", (version_id,)
        ).fetchone()
    return bytes(row["excel_blob"]) if row and row["excel_blob"] else None


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
            LEFT JOIN quotes q ON q.id = qv.quote_id
            WHERE qv.saved_by_user_id = ? OR qv.confirmed_by_user_id = ?
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
            WHERE pqa.user_id = ?
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
