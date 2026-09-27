"""Local email drafts and a guarded Outlook web adapter."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlencode

import browser

ADDRESS = re.compile(r"^[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+$")
OUTLOOK_INBOX = "https://outlook.office.com/mail/"
OUTLOOK_COMPOSE = "https://outlook.office.com/mail/deeplink/compose"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS email_drafts (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        recipients TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.commit()


def validate(recipients: str, subject: str, body: str) -> str:
    if not all(isinstance(value, str) for value in (recipients, subject, body)):
        raise ValueError("Email recipient, subject, and body must be text")
    addresses = [part.strip() for part in recipients.split(",")]
    if not 1 <= len(addresses) <= 10 or any(not ADDRESS.fullmatch(address) for address in addresses):
        raise ValueError("Provide 1–10 complete email addresses")
    if not subject.strip() or len(subject) > 300:
        raise ValueError("Email subject must be 1–300 characters")
    if not body.strip() or len(body) > 4000:
        raise ValueError("Email body must be 1–4000 characters")
    return ", ".join(addresses)


def compose_url(provider: str, recipients: str, subject: str, body: str) -> str:
    if provider != "outlook":
        raise ValueError("This email provider is not configured yet")
    return OUTLOOK_COMPOSE + "?" + urlencode({"to": recipients, "subject": subject, "body": body})


def prepare(db: sqlite3.Connection, provider: str, recipients: str, subject: str, body: str) -> str:
    recipients = validate(recipients, subject, body)
    url = compose_url(provider, recipients, subject, body)
    timestamp = now()
    cursor = db.execute(
        "INSERT INTO email_drafts(provider,recipients,subject,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (provider, recipients, subject, body, "prepared", timestamp, timestamp),
    )
    db.commit()
    try:
        browser.open_url(url)
    except Exception:
        db.execute("UPDATE email_drafts SET status=?,updated_at=? WHERE id=?", ("needs_review", now(), cursor.lastrowid))
        db.commit()
        raise
    return f"Draft #{cursor.lastrowid} opened in {provider}. Review it before asking Omi to send."


def draft(db: sqlite3.Connection, identifier: str) -> dict:
    if identifier == "latest":
        row = db.execute("SELECT id,provider,recipients,subject,body,status FROM email_drafts ORDER BY id DESC LIMIT 1").fetchone()
    elif identifier.isdigit():
        row = db.execute("SELECT id,provider,recipients,subject,body,status FROM email_drafts WHERE id=?", (int(identifier),)).fetchone()
    else:
        row = None
    if row is None:
        raise ValueError("Email draft not found")
    return dict(zip(("id", "provider", "recipients", "subject", "body", "status"), row))


def preview(db: sqlite3.Connection, identifier: str) -> dict:
    value = draft(db, identifier)
    if value["status"] != "prepared":
        raise ValueError(f"Draft #{value['id']} is {value['status']}; inspect it before retrying")
    return value


def send(db: sqlite3.Connection, identifier: str) -> str:
    value = preview(db, identifier)
    if value["provider"] != "outlook":
        raise ValueError("This email provider is not configured yet")
    page = browser.snapshot()
    first_line = page.splitlines()[0] if page else ""
    if "outlook.office.com" not in first_line:
        raise RuntimeError("Open the prepared draft in Omi's Outlook browser first")
    required = (value["recipients"].split(",")[0].strip(), value["subject"], value["body"][:60])
    if any(piece not in page for piece in required):
        raise RuntimeError("Omi could not verify the recipient, subject, and body in the compose window; send it manually or reopen the draft")
    browser.resolve_target("Send")
    db.execute("UPDATE email_drafts SET status=?,updated_at=? WHERE id=?", ("sending", now(), value["id"]))
    db.commit()
    try:
        browser.click("Send")
    except Exception:
        db.execute("UPDATE email_drafts SET status=?,updated_at=? WHERE id=?", ("needs_review", now(), value["id"]))
        db.commit()
        raise
    db.execute("UPDATE email_drafts SET status=?,updated_at=? WHERE id=?", ("submitted", now(), value["id"]))
    db.commit()
    return f"Send was submitted for draft #{value['id']}. Check Sent Items to confirm delivery."


def inbox() -> str:
    browser.open_url(OUTLOOK_INBOX)
    return browser.read_page()
