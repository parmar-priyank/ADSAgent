"""
deploy/email_backup.py — emails a database backup file as an attachment.

Called by deploy/backup_db.sh after it writes a fresh local backup, so a
copy of the data also exists off-server (this machine's disk is not the
only place it lives). Reuses the SMTP config already set up for OTP
emails (SMTP_HOST/PORT/USER/PASSWORD in .env) — no separate credentials.

Usage: python3 email_backup.py <path-to-backup-file>
"""
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from utils.mailer import send_email, SMTP_CONFIGURED


def main():
    if len(sys.argv) != 2:
        print("Usage: email_backup.py <path-to-backup-file>", file=sys.stderr)
        sys.exit(1)

    backup_path = sys.argv[1]
    if not os.path.isfile(backup_path):
        print(f"Backup file not found: {backup_path}", file=sys.stderr)
        sys.exit(1)

    to_addr = os.environ.get("BACKUP_EMAIL_TO", "").strip()
    if not to_addr:
        print("BACKUP_EMAIL_TO not set in .env — skipping email delivery.", file=sys.stderr)
        sys.exit(0)  # not a failure — email delivery is optional

    if not SMTP_CONFIGURED:
        print("SMTP is not configured — skipping email delivery.", file=sys.stderr)
        sys.exit(0)

    size_mb = os.path.getsize(backup_path) / (1024 * 1024)
    filename = os.path.basename(backup_path)
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;
                background:#fafaf9;border-radius:16px;border:1px solid #e8e2db;">
      <h2 style="font-size:18px;font-weight:800;color:#1a1a1a;margin:0 0 12px;">
        Weekly database backup
      </h2>
      <p style="font-size:14px;color:#555;line-height:1.6;">
        Attached: <strong>{filename}</strong> ({size_mb:.1f} MB)
      </p>
      <p style="font-size:12px;color:#aaa;margin-top:16px;">
        Automated message from ADS Agent's weekly backup job.
      </p>
    </div>
    """
    ok = send_email(
        to_addr,
        f"ADS Agent — weekly DB backup ({filename})",
        html,
        attachment_path=backup_path,
    )
    if not ok:
        print("Failed to send backup email.", file=sys.stderr)
        sys.exit(1)
    print(f"Backup emailed to {to_addr}")


if __name__ == "__main__":
    main()
