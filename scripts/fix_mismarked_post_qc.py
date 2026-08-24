"""
One-off data correction: qc_versions rows saved through the STC (Post-QC)
template but stored with kind='pre' due to the variable-shadowing bug in
upload_zip (fixed in routers/qc_checks.py, commit 3561e20).

Only touches rows whose template_name exactly matches a template row whose
OWN kind is 'post' (currently just "STC") and whose stored kind is anything
other than 'post'. Prints every row it's about to change before writing,
and everything runs inside one transaction.

Run this on the server, from the app's own directory, with its venv:
    cd /var/www/adsagent
    venv/bin/python3 scripts/fix_mismarked_post_qc.py            # dry run, no writes
    venv/bin/python3 scripts/fix_mismarked_post_qc.py --apply    # actually applies the fix

Always run the dry run first and read the printed list before --apply —
back up the database (deploy/backup_db.sh, or just copy the .db file) before
applying if you want an easy way back.
"""
import sys
sys.path.insert(0, ".")

from db.connection import get_db

APPLY = "--apply" in sys.argv

with get_db() as conn:
    post_template_names = [
        r["name"] for r in conn.execute(
            "SELECT name FROM templates WHERE kind = 'post'"
        ).fetchall()
    ]
    if not post_template_names:
        print("No Post-QC templates found — nothing to do.")
        sys.exit(0)

    placeholders = ",".join("?" * len(post_template_names))
    rows = conn.execute(
        f"""
        SELECT id, quote_id, kind, status, template_name, zip_filename,
               saved_by_user_id, confirmed_by_user_id, saved_at
        FROM qc_versions
        WHERE template_name IN ({placeholders})
          AND COALESCE(kind, 'pre') != 'post'
        ORDER BY id
        """,
        post_template_names,
    ).fetchall()

    print(f"Post-QC template names: {post_template_names}")
    print(f"Found {len(rows)} mismarked row(s):\n")
    for r in rows:
        print(f"  id={r['id']:>4}  quote_id={r['quote_id']:>4}  "
              f"kind={r['kind']!r:>6} -> 'post'  status={r['status']:<9}  "
              f"template={r['template_name']!r}  zip={r['zip_filename']!r}")

    if not rows:
        print("Nothing to fix.")
        sys.exit(0)

    if not APPLY:
        print(f"\nDry run only — {len(rows)} row(s) would be updated. "
              "Re-run with --apply to write the change.")
        sys.exit(0)

    ids = [r["id"] for r in rows]
    id_placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE qc_versions SET kind = 'post' WHERE id IN ({id_placeholders})",
        ids,
    )
    print(f"\nUpdated {len(ids)} row(s) to kind='post'.")
