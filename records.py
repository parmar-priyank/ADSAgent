"""
db.py — SQLite database layer for the Solar Agreement Extractor.

Two-table design:
  - quotes:      one row per quote (Customer & Quote, Retailer, and Payment
                 fields — all 1-to-1 with a quote). This is the "Saved Records"
                 list.
  - line_items:  many rows per quote (the System & Pricing table), linked to a
                 quote by quote_number.

The database file and both tables are created automatically on first import
if they don't already exist (see ensure_db / init_db).
"""
from database import get_db

# Scalar fields stored on the quotes table (everything that is 1-to-1 with a
# quote). line_items are stored separately.
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
]


def init_db():
    """Create the quotes and line_items tables if they don't already exist."""
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
        # Enforce one row per quote_number so re-uploads can replace cleanly.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_quote_number "
            "ON quotes(quote_number)"
        )
        # Add qc_excel column if it doesn't exist yet (safe migration).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(quotes)").fetchall()]
        if "qc_excel" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN qc_excel BLOB")
        # Versioned QC history table.
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
        # Migrate existing qc_versions rows if columns are missing.
        qcv_cols = [r[1] for r in conn.execute("PRAGMA table_info(qc_versions)").fetchall()]
        if "rows_json" not in qcv_cols:
            conn.execute("ALTER TABLE qc_versions ADD COLUMN rows_json TEXT")
        if "confirmed_by_user_id" not in qcv_cols:
            conn.execute("ALTER TABLE qc_versions ADD COLUMN confirmed_by_user_id INTEGER")
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
        # Temporary store for PDFs awaiting duplicate-confirmation.
        # Keeps PDF bytes + already-extracted text server-side so the browser
        # never has to carry a multi-MB base64 blob in a form field.
        # Rows are purged on next startup and after 1 h by store_pending_pdf.
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
        # Purge stale pending rows (older than 1 hour) on every startup.
        conn.execute(
            "DELETE FROM pending_pdfs "
            "WHERE created_at < datetime('now', '-1 hour')"
        )


def save_extraction(filename: str, data: dict) -> int:
    """
    Save one extracted quote into the two-table schema and return the quote row id.

    If a quote with the same (non-empty) quote_number already exists, the old
    quote and its line items are removed first so the latest upload replaces it
    (line_items cascade-delete with the parent quote).
    """
    quote_number = (data.get("quote_number") or "").strip()

    with get_db() as conn:
        # Replace any previous quote with the same number (cascades to line_items).
        if quote_number:
            conn.execute("DELETE FROM quotes WHERE quote_number = ?", (quote_number,))

        # --- insert the quote row ---
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

        # --- insert the line items ---
        for pos, li in enumerate(data.get("line_items") or []):
            conn.execute(
                """
                INSERT INTO line_items
                    (quote_number, position, item, quantity, specification, price)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    quote_number,
                    pos,
                    li.get("item", ""),
                    li.get("quantity", ""),
                    li.get("specification", ""),
                    li.get("price", ""),
                ),
            )

        return quote_id


def _attach_line_items(conn, quotes: list) -> list:
    """Fetch all line items for the given quotes in a single query and attach them."""
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
    """Return a single quote by its database id, including line_items."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()
        if not row:
            return None
        quotes = [dict(row)]
        return _attach_line_items(conn, quotes)[0]


def get_recent(limit: int = 20):
    """
    Return the most recent quotes, each as a dict with all quote fields plus a
    'line_items' list and 'qc_version_count', so callers can render the full record.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM quotes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        quotes = [dict(r) for r in rows]
        quotes = _attach_line_items(conn, quotes)
        # Attach QC version counts in a single query.
        if quotes:
            ids = [q["id"] for q in quotes]
            placeholders = ",".join("?" * len(ids))
            counts = conn.execute(
                f"SELECT quote_id, COUNT(*) as cnt FROM qc_versions WHERE quote_id IN ({placeholders}) GROUP BY quote_id",
                ids,
            ).fetchall()
            count_map = {r["quote_id"]: r["cnt"] for r in counts}
            for q in quotes:
                q["qc_version_count"] = count_map.get(q["id"], 0)

    for display_num, q in enumerate(quotes, start=1):
        q["display_num"] = display_num
    return quotes


def find_by_filename(filename: str):
    """Return the most recent quote saved from this filename, or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE filename = ? ORDER BY id DESC LIMIT 1", (filename,)
        ).fetchone()
        if not row:
            return None
        quotes = [dict(row)]
        return _attach_line_items(conn, quotes)[0]


def find_by_quote_number(quote_number: str):
    """Return the existing quote with this quote_number, or None."""
    if not quote_number:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE quote_number = ? ORDER BY id DESC LIMIT 1",
            (quote_number,),
        ).fetchone()
        if not row:
            return None
        quotes = [dict(row)]
        return _attach_line_items(conn, quotes)[0]


def save_qc_excel(quote_id: int, xlsx_bytes: bytes):
    """Attach a QC Excel blob to an existing quote row (latest snapshot)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE quotes SET qc_excel = ? WHERE id = ?",
            (xlsx_bytes, quote_id),
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
    confirmed_by_user_id: int = None,
) -> int:
    """Append a new QC version for this quote and return the version number."""
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
                 yes_count, no_count, na_count, excel_blob, rows_json, confirmed_by_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (quote_id, next_version, template_name, zip_filename,
             yes_count, no_count, na_count, xlsx_bytes, rows_json, confirmed_by_user_id),
        )
    return next_version


def get_qc_history_by_user(user_id: int) -> list:
    """Return all QC versions confirmed by a specific user, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT qv.id, qv.version, qv.template_name, qv.zip_filename,
                   qv.yes_count, qv.no_count, qv.na_count, qv.confirmed_at,
                   q.id as quote_id, q.customer_name, q.quote_number
            FROM qc_versions qv
            JOIN quotes q ON q.id = qv.quote_id
            WHERE qv.confirmed_by_user_id = ?
            ORDER BY qv.confirmed_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_qc_version(version_id: int, xlsx_bytes: bytes, rows_json: str,
                      yes_count: int, no_count: int, na_count: int):
    """Overwrite the Excel blob, rows, and counts for an existing QC version."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE qc_versions
               SET excel_blob=?, rows_json=?, yes_count=?, no_count=?, na_count=?
             WHERE id=?
            """,
            (xlsx_bytes, rows_json, yes_count, no_count, na_count, version_id),
        )


def get_qc_versions(quote_id: int) -> list:
    """Return all QC versions for a quote, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, version, template_name, zip_filename,
                   yes_count, no_count, na_count, confirmed_at
            FROM qc_versions WHERE quote_id = ?
            ORDER BY version DESC
            """,
            (quote_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_qc_version_excel(version_id: int) -> bytes | None:
    """Return the Excel blob for a specific qc_versions row."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT excel_blob FROM qc_versions WHERE id = ?", (version_id,)
        ).fetchone()
    return bytes(row["excel_blob"]) if row and row["excel_blob"] else None


def delete_quote(quote_id: int):
    """Delete a quote and its line items (cascade handles line_items)."""
    with get_db() as conn:
        conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))


# ---------------------------------------------------------------------------
# Pending-PDF helpers (duplicate-confirmation flow)
# ---------------------------------------------------------------------------
import secrets as _secrets

def store_pending_pdf(filename: str, pdf_bytes: bytes, pdf_text: str) -> str:
    """
    Save PDF bytes + pre-extracted text server-side and return a short opaque
    token.  Rows older than 1 hour are pruned on each call so the table stays
    small even without a background job.
    """
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
    """
    Retrieve and delete the pending PDF row for *token*.
    Returns {"filename": ..., "pdf_bytes": ..., "pdf_text": ...} or None.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT filename, pdf_bytes, pdf_text FROM pending_pdfs WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_pdfs WHERE token = ?", (token,))
    return {"filename": row["filename"], "pdf_bytes": bytes(row["pdf_bytes"]), "pdf_text": row["pdf_text"]}


init_db()