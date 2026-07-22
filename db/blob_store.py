"""
db/blob_store.py — filesystem storage for QC Excel snapshots.

Excel files used to be stored as BLOBs directly inside the SQLite database
(qc_versions.excel_blob, quotes.qc_excel), one per QC version. That made the
database file grow without bound and slowed backups. They now live on disk
under qc_files/ instead, with only a relative path kept in the DB.

Old rows that still carry an in-DB blob keep working — the read paths in
quote_repo prefer the on-disk file and fall back to the blob when a row has
no path yet (lazy migration; nothing is rewritten in bulk).

Paths stored in the DB are RELATIVE to _BASE_DIR so the app directory can be
moved without breaking references. All filenames are generated here (never
derived from user input), so there is no path-traversal surface.
"""
import os

# qc_files/ lives beside the app (…/ADSAgent/qc_files), which is already in the
# systemd service's ReadWritePaths. _BASE_DIR resolves to the project root:
# this file is at <root>/db/blob_store.py, so root is two levels up.
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_QC_FILES_DIRNAME = "qc_files"
_QC_FILES_DIR = os.path.join(_BASE_DIR, _QC_FILES_DIRNAME)


def _ensure_dir():
    os.makedirs(_QC_FILES_DIR, exist_ok=True)


def save_excel(xlsx_bytes: bytes, *, kind: str = "version", ident: str = "") -> str:
    """Write Excel bytes to a new file under qc_files/ and return the path to
    store in the DB, RELATIVE to the app root (e.g. 'qc_files/version_42.xlsx').

    kind/ident only shape the filename for human readability; uniqueness is
    guaranteed by appending a short random token, so re-saving the same version
    never clobbers a file another request might still be reading.
    """
    _ensure_dir()
    import secrets
    safe_ident = "".join(c for c in str(ident) if c.isalnum()) or "x"
    token = secrets.token_hex(6)
    filename = f"{kind}_{safe_ident}_{token}.xlsx"
    abs_path = os.path.join(_QC_FILES_DIR, filename)
    with open(abs_path, "wb") as f:
        f.write(xlsx_bytes)
    return os.path.join(_QC_FILES_DIRNAME, filename)


def read_excel(rel_path: str) -> bytes | None:
    """Read Excel bytes back given a DB-stored relative path. Returns None if
    the path is empty or the file is missing (caller then falls back to any
    in-DB blob)."""
    if not rel_path:
        return None
    abs_path = os.path.join(_BASE_DIR, rel_path)
    # Defensive: never read outside qc_files/, even though paths are app-generated.
    if not os.path.abspath(abs_path).startswith(_QC_FILES_DIR + os.sep):
        return None
    try:
        with open(abs_path, "rb") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return None


def delete_excel(rel_path: str) -> None:
    """Remove an Excel file given its DB-stored relative path. Silently ignores
    an empty path or an already-missing file — deletion is best-effort cleanup,
    never a hard failure that should block deleting the DB row."""
    if not rel_path:
        return
    abs_path = os.path.join(_BASE_DIR, rel_path)
    if not os.path.abspath(abs_path).startswith(_QC_FILES_DIR + os.sep):
        return
    try:
        os.remove(abs_path)
    except (FileNotFoundError, OSError):
        pass
