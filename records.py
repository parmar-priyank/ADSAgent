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
    'line_items' list, so callers can render the full record.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM quotes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        quotes = [dict(r) for r in rows]
        quotes = _attach_line_items(conn, quotes)

    for display_num, q in enumerate(quotes, start=1):
        q["display_num"] = display_num
    return quotes


init_db()