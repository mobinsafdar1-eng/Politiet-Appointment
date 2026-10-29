"""Email helpers: send notices and read the verification code from the inbox."""
import email
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import unescape

GMAIL = os.environ.get("GMAIL_ADDRESS", "")
APP_PW = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO") or GMAIL
IMAP_HOST = os.environ.get("IMAP_HOST") or "imap.gmail.com"
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"


def send_mail(subject, body, attachments=()):
    msg = EmailMessage()
    msg["From"] = GMAIL
    msg["To"] = NOTIFY_TO
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments:
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(str(path)))
    with smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context()) as s:
        s.login(GMAIL, APP_PW)
        s.send_message(msg)


def _text_of(msg):
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            if part.get_content_type() == "text/html":
                text = unescape(re.sub(r"<[^>]+>", " ", text))
            parts.append(text)
    return "\n".join(parts)


def _find_code(text):
    m = re.search(r"(?:kode|code|verifi\w*)\D{0,40}(\d{4,8})", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{6})\b", text)
    return m.group(1) if m else None


def fetch_code(since, timeout=150, poll=8):
    """Wait for a verification-code email that arrived after `since` (UTC datetime)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST)
            imap.login(GMAIL, APP_PW)
            imap.select("INBOX")
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            _, data = imap.search(None, f'(SINCE "{day}")')
            ids = data[0].split()[-15:]
            for num in reversed(ids):
                _, raw = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])
                try:
                    sent = parsedate_to_datetime(msg["Date"])
                    if sent.tzinfo is None:
                        sent = sent.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if sent < since - timedelta(seconds=60):
                    continue
                code = _find_code(_text_of(msg))
                if code:
                    imap.logout()
                    return code
            imap.logout()
        except Exception as e:  # keep polling on transient errors
            print(f"[mail] IMAP poll error: {e}")
        time.sleep(poll)
    return None


def _test():
    send_mail("Appointment checker: test email",
              "If you can read this, sending works. Reading the inbox is checked next.")
    print("Test email sent.")
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(GMAIL, APP_PW)
    imap.select("INBOX")
    imap.logout()
    print("Inbox login OK.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
