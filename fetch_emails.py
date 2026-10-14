"""
Step 1: Fetching the Emails (Data Ingestion)

Logs into the dedicated BCC-archive mailbox via IMAP, reads unread emails,
extracts Sender Name / Sender Email / Subject / Body, and writes each one
straight into the SQLite database from database.py.

Run directly:  python fetch_emails.py
Or schedule it (cron, Task Scheduler, etc.) to run every few minutes.
"""

import email
import hashlib
import html
import imaplib
import logging
import re
import time
from datetime import timezone
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from config import IMAP_SERVER, IMAP_PORT, IMAP_FOLDER, EMAIL_ACCOUNT, EMAIL_PASSWORD, MY_EMAIL_ADDRESSES
from database import init_db, get_connection, get_or_create_customer, insert_email_thread

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5  # doubles on each retry

# Messages fetched per IMAP round-trip; each batch is committed before it is marked read.
FETCH_BATCH_SIZE = 50


def decode_mime_words(raw):
    """Decodes MIME-encoded header text (e.g. '=?UTF-8?B?...?=') into a plain string."""
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except LookupError:  # unknown charset name
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


class _HTMLToText(HTMLParser):
    """Minimal HTML -> text converter so HTML-only emails don't waste LLM tokens on markup."""

    _BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup):
    parser = _HTMLToText()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:  # unknown charset name
        return payload.decode("utf-8", errors="replace")


def get_email_body(msg):
    """Extracts the best available text body from a (possibly multipart) email.message.Message."""
    html_body = None
    for part in msg.walk():
        if part.is_multipart() or "attachment" in str(part.get("Content-Disposition", "")):
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            text = _decode_part(part)
            if text is not None:
                return text  # plain text preferred
        elif content_type == "text/html" and html_body is None:
            html_body = _decode_part(part)
    if html_body is not None:
        return html_to_text(html_body)
    if not msg.is_multipart():
        payload = msg.get_payload()
        return payload if isinstance(payload, str) else ""
    return ""


def _normalize_date(date_header):
    """Parses an email Date header into a UTC ISO-8601 string so dates sort correctly."""
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return date_header or ""


def pick_customer(msg):
    """
    Returns (name, email) of the customer this message belongs to.
    Incoming mail -> the sender. Outgoing mail you BCC'd to the archive (sent
    from one of MY_EMAIL_ADDRESSES) -> the first To/Cc recipient who isn't you.
    """
    sender_name, sender_email = parseaddr(msg.get("From", ""))
    sender_email = (sender_email or "").lower().strip()
    if sender_email and sender_email not in MY_EMAIL_ADDRESSES:
        return decode_mime_words(sender_name), sender_email

    recipients = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    for name, addr in recipients:
        addr = (addr or "").lower().strip()
        if addr and "@" in addr and addr not in MY_EMAIL_ADDRESSES:
            return decode_mime_words(name), addr

    # Note-to-self or no usable recipient: fall back to the sender.
    return decode_mime_words(sender_name), sender_email


def _connect_imap(retries=MAX_RETRIES, backoff=RETRY_BACKOFF_SECONDS):
    """Connects to the IMAP server with retry + exponential backoff."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            logger.info("Connecting to %s:%d (attempt %d/%d)...", IMAP_SERVER, IMAP_PORT, attempt, retries)
            imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            imap.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            logger.info("IMAP login successful.")
            return imap
        except (imaplib.IMAP4.error, OSError, TimeoutError) as e:
            last_error = e
            if attempt < retries:
                wait = backoff * (2 ** (attempt - 1))
                logger.warning("IMAP connection failed: %s. Retrying in %ds...", e, wait)
                time.sleep(wait)
            else:
                logger.error("IMAP connection failed after %d attempts.", retries)
    raise RuntimeError(f"Could not connect to IMAP server after {retries} attempts: {last_error}")


def fetch_unread_emails(mark_as_seen=True, limit=None):
    """
    Connects to the IMAP server, pulls every unread email, stores it, and
    returns the number of emails successfully fetched and saved.

    mark_as_seen: if False, uses BODY.PEEK[] so messages stay unread on the
                  server (useful while testing).
    limit:        cap on how many unread messages to process in one run.
    """
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise RuntimeError(
            "Set BCC_EMAIL_ACCOUNT and BCC_EMAIL_PASSWORD as environment variables "
            "(see .env.example) before running this script. Use an app password, "
            "not your normal account password."
        )

    init_db()

    imap = _connect_imap()
    try:
        status, _ = imap.select(IMAP_FOLDER)
        if status != "OK":
            raise RuntimeError(f"Could not open IMAP folder '{IMAP_FOLDER}'.")

        # UIDs (not sequence numbers) stay valid even if the mailbox changes mid-run.
        status, data = imap.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("IMAP search for unread mail failed.")

        uids = data[0].split()
        if limit:
            uids = uids[:limit]

        logger.info("Found %d unread email(s) to process.", len(uids))

        fetched_count = 0
        skipped_count = 0

        with get_connection() as conn:
            for start in range(0, len(uids), FETCH_BATCH_SIZE):
                batch = uids[start:start + FETCH_BATCH_SIZE]
                # BODY.PEEK[] never sets \Seen; messages are marked read only after they're committed,
                # so a crash mid-run can't leave emails "read" on the server but missing from the DB.
                status, msg_data = imap.uid("FETCH", b",".join(batch), "(BODY.PEEK[])")
                if status != "OK":
                    logger.warning("Failed to fetch batch starting at UID %s, skipping.", batch[0])
                    continue

                stored_uids = []
                for item in msg_data:
                    if not isinstance(item, tuple):
                        continue  # closing b")" separators
                    uid_match = re.search(rb"UID (\d+)", item[0])
                    uid = uid_match.group(1) if uid_match else None
                    try:
                        inserted = _store_message(conn, item[1])
                    except Exception as e:
                        logger.error("Error processing message UID %s: %s", uid, e, exc_info=True)
                        continue
                    if inserted is None:
                        continue  # unusable message; leave it unread for a human to look at
                    if inserted:
                        fetched_count += 1
                    else:
                        skipped_count += 1
                    if uid:
                        stored_uids.append(uid)

                conn.commit()
                if mark_as_seen and stored_uids:
                    imap.uid("STORE", b",".join(stored_uids), "+FLAGS", r"(\Seen)")

                logger.info("Progress: processed %d/%d emails...", min(start + len(batch), len(uids)), len(uids))

        logger.info("Done: %d new email(s) stored, %d duplicate(s) skipped.", fetched_count, skipped_count)
        return fetched_count
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _store_message(conn, raw_email):
    """
    Parses one raw RFC822 message and stores it.
    Returns True if inserted, False if it was a duplicate, None if unusable.
    """
    msg = email.message_from_bytes(raw_email)

    customer_name, customer_email = pick_customer(msg)
    subject = decode_mime_words(msg.get("Subject", "(No Subject)"))
    if not customer_email:
        logger.warning("Skipping message with no usable address (subject: %s).", subject)
        return None

    # Messages without a Message-ID get a content hash so re-fetching never duplicates them.
    message_id = (msg.get("Message-ID") or "").strip() or f"<sha256:{hashlib.sha256(raw_email).hexdigest()}>"

    customer_id = get_or_create_customer(conn, customer_name, customer_email)
    return insert_email_thread(
        conn, customer_id, subject, get_email_body(msg), _normalize_date(msg.get("Date")), message_id,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    count = fetch_unread_emails()
    print(f"Fetched and stored {count} new email(s).")
