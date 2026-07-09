"""
utils/mailer.py — SMTP email sender using stdlib smtplib.

Works with any standard SMTP provider (Gmail, Outlook/Office365, etc.) —
just point SMTP_HOST/PORT/USER/PASSWORD at the provider you're using.

Configure via .env:
  SMTP_HOST     e.g. smtp.gmail.com (Gmail) or smtp.office365.com (Outlook/365)
  SMTP_PORT     e.g. 587
  SMTP_USER     sender email address
  SMTP_PASSWORD app password / SMTP password
  SMTP_FROM     optional display name + address, defaults to SMTP_USER

See .env.example for provider-specific setup notes (app passwords, etc.).
"""
import os
import re
import smtplib
import logging
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid, parseaddr
from email import encoders

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _cfg():
    """Read SMTP config fresh each call so .env changes take effect at runtime."""
    host     = os.environ.get("SMTP_HOST", "").strip()
    port     = int(os.environ.get("SMTP_PORT", "587") or "587")
    user     = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_    = os.environ.get("SMTP_FROM", "").strip() or user
    return host, port, user, password, from_


def _is_configured():
    h, _, u, p, _ = _cfg()
    return bool(h and u and p)


class _SmtpConfiguredProxy:
    def __bool__(self):
        return _is_configured()
    def __repr__(self):
        return str(_is_configured())


SMTP_CONFIGURED = _SmtpConfiguredProxy()


def _plaintext_fallback(html_body: str) -> str:
    """Strip tags for a plaintext alternative part — improves spam scoring
    (multipart/alternative with only HTML is a common spam-filter signal,
    especially on stricter Google Workspace / business mail policies)."""
    text = _TAG_RE.sub("", html_body)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def send_email(to: str, subject: str, html_body: str, attachment_path: str = None) -> bool:
    """Send an HTML email, optionally with one file attached. Returns True
    on success, False on failure. `attachment_path`, if given, is attached
    as a generic binary file (its basename is used as the filename)."""
    host, port, user, password, from_ = _cfg()
    if not (host and user and password):
        logger.warning("SMTP not configured — email not sent.")
        return False
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"]    = subject
        msg["From"]       = from_
        msg["To"]         = to
        msg["Date"]       = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=(parseaddr(from_)[1].split("@")[-1] or None))

        body = MIMEMultipart("alternative")
        # Plaintext part first, HTML second — email clients render the last
        # (most-preferred) part, and having both reduces spam-filter scoring.
        body.attach(MIMEText(_plaintext_fallback(html_body), "plain"))
        body.attach(MIMEText(html_body, "html"))
        msg.attach(body)

        if attachment_path:
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{os.path.basename(attachment_path)}"',
            )
            msg.attach(part)

        with smtplib.SMTP(host, port, timeout=60) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.sendmail(user, [to], msg.as_string())
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to, e)
        return False
