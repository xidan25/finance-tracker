"""Gmail IMAP client for fetching emails by label.

Provides:
  - connect(user, password): open IMAP4_SSL connection to Gmail
  - select_label(conn, label): select a Gmail label as current mailbox
  - search_uids_after(conn, last_uid): get list of UIDs > last_uid
  - fetch_email(conn, uid): fetch raw bytes for one UID
  - parse_email(...): parse raw bytes into ParsedEmail dataclass
  - get_last_uid(db, label) / set_last_uid(db, label, uid): UID state in meta table
  - fetch_new_emails(...): high-level generator that ties it all together
"""

import email
import imaplib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Iterator

import html2text

from finance_tracker.db import get_connection, transaction

GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993


@dataclass
class ParsedEmail:
    """Parsed Gmail email metadata + body content."""

    uid: int
    message_id: str
    subject: str
    sender: str
    received_at: datetime  # tz-aware UTC
    body_text: str          # plain text (HTML converted via html2text)
    body_html: str | None   # original HTML if present
    label: str              # the Gmail label this email was fetched under


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(user: str, password: str) -> imaplib.IMAP4_SSL:
    """Open IMAPS connection to Gmail and log in.

    Raises RuntimeError with a friendly message on common login failures.
    """
    conn = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "Application-specific password required" in msg:
            raise RuntimeError(
                "Gmail requires an App Password (not your regular password). "
                "Generate one at https://myaccount.google.com/apppasswords"
            ) from e
        if "Invalid credentials" in msg or "AUTHENTICATIONFAILED" in msg:
            raise RuntimeError(
                "Gmail login failed: invalid credentials. "
                "Check GMAIL_USER and GMAIL_APP_PASSWORD in .env."
            ) from e
        raise
    return conn


def select_label(conn: imaplib.IMAP4_SSL, label: str) -> int:
    """Select a Gmail label as the current mailbox. Returns total message count."""
    # Quote the label name; Gmail accepts spaces if quoted.
    status, data = conn.select(f'"{label}"', readonly=True)
    if status != "OK":
        raise RuntimeError(f"Failed to select label '{label}': {data}")
    try:
        return int(data[0])
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Searching & fetching
# ---------------------------------------------------------------------------


def search_uids_after(conn: imaplib.IMAP4_SSL, last_uid: int) -> list[int]:
    """Return list of UIDs strictly greater than last_uid (i.e. new emails)."""
    next_uid = last_uid + 1
    status, data = conn.uid("SEARCH", None, f"UID {next_uid}:*")
    if status != "OK":
        raise RuntimeError(f"UID search failed: {data}")
    raw = data[0].decode().split() if data and data[0] else []
    # Filter defensively: IMAP "N:*" returns max-UID even if max < N.
    return sorted(int(u) for u in raw if int(u) > last_uid)


def fetch_raw(conn: imaplib.IMAP4_SSL, uid: int) -> tuple[bytes, str | None]:
    """Fetch raw RFC822 bytes + INTERNALDATE string for one UID."""
    status, data = conn.uid("FETCH", str(uid), "(RFC822 INTERNALDATE)")
    if status != "OK":
        raise RuntimeError(f"Failed to fetch UID {uid}: {data}")

    raw_bytes: bytes | None = None
    internaldate: str | None = None
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            envelope_bytes, body_bytes = item
            raw_bytes = body_bytes
            envelope_str = envelope_bytes.decode("latin-1", errors="replace")
            m = re.search(r'INTERNALDATE "([^"]+)"', envelope_str)
            if m:
                internaldate = m.group(1)
            break

    if raw_bytes is None:
        raise RuntimeError(f"No body returned for UID {uid}")
    return raw_bytes, internaldate


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _decode_header(value: str | None) -> str:
    """Decode RFC 2047 encoded headers like '=?UTF-8?B?...?='."""
    if not value:
        return ""
    parts = decode_header(value)
    out: list[str] = []
    for content, charset in parts:
        if isinstance(content, bytes):
            try:
                out.append(content.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(content.decode("utf-8", errors="replace"))
        else:
            out.append(content)
    return "".join(out)


def _extract_bodies(msg: Message) -> tuple[str, str | None]:
    """Return (text_body, html_body_or_none).

    Prefers an existing text/plain part; falls back to converting HTML.
    """
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                text_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace") if payload else ""
        if msg.get_content_type() == "text/html":
            html_parts.append(text)
        else:
            text_parts.append(text)

    body_html = "\n".join(html_parts) if html_parts else None
    if text_parts:
        body_text = "\n".join(text_parts).strip()
    elif body_html:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0  # disable line wrapping
        body_text = h.handle(body_html).strip()
    else:
        body_text = ""

    return body_text, body_html


def parse_email(
    raw_bytes: bytes,
    uid: int,
    internaldate: str | None,
    label: str,
) -> ParsedEmail:
    """Parse RFC822 bytes into a ParsedEmail."""
    msg = email.message_from_bytes(raw_bytes)

    subject = _decode_header(msg.get("Subject", ""))
    sender = _decode_header(msg.get("From", ""))
    message_id = msg.get("Message-ID", f"<no-id-uid-{uid}>")

    received_at: datetime | None = None
    date_str = msg.get("Date")
    if date_str:
        try:
            received_at = parsedate_to_datetime(date_str)
        except (TypeError, ValueError):
            pass
    if received_at is None and internaldate:
        try:
            received_at = datetime.strptime(internaldate, "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            pass
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    received_at = received_at.astimezone(timezone.utc)

    body_text, body_html = _extract_bodies(msg)

    return ParsedEmail(
        uid=uid,
        message_id=message_id,
        subject=subject,
        sender=sender,
        received_at=received_at,
        body_text=body_text,
        body_html=body_html,
        label=label,
    )


# ---------------------------------------------------------------------------
# UID state in meta table
# ---------------------------------------------------------------------------


def _meta_key(label: str) -> str:
    """Convert label name to safe meta-table key."""
    return "last_uid_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def get_last_uid(db: sqlite3.Connection, label: str) -> int:
    """Read last processed UID for a label; 0 if never run."""
    row = db.execute(
        "SELECT value FROM meta WHERE key = ?", (_meta_key(label),)
    ).fetchone()
    return int(row["value"]) if row else 0


def set_last_uid(db: sqlite3.Connection, label: str, uid: int) -> None:
    """Persist last processed UID for a label."""
    with transaction(db):
        db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_meta_key(label), str(uid)),
        )


# ---------------------------------------------------------------------------
# High-level fetch
# ---------------------------------------------------------------------------


def fetch_new_emails(
    user: str,
    password: str,
    label: str,
    since_uid: int | None = None,
    max_emails: int | None = None,
) -> Iterator[ParsedEmail]:
    """Generator yielding parsed new emails from a Gmail label.

    Args:
        user: Gmail address.
        password: 16-char App Password.
        label: Gmail label to fetch.
        since_uid: Start fetching from UIDs > since_uid. If None, reads from meta.
        max_emails: Cap on number of emails to fetch (None = unlimited).
    """
    db = get_connection()
    try:
        if since_uid is None:
            since_uid = get_last_uid(db, label)
    finally:
        db.close()

    imap = connect(user, password)
    try:
        select_label(imap, label)
        uids = search_uids_after(imap, since_uid)
        if max_emails is not None:
            uids = uids[:max_emails]
        for uid in uids:
            raw_bytes, internaldate = fetch_raw(imap, uid)
            yield parse_email(raw_bytes, uid, internaldate, label)
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass
