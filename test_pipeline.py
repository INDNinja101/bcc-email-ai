"""
End-to-end smoke test for the BCC Email AI pipeline.
Uses an in-memory SQLite database and mocked API calls so no real
IMAP server or AI provider keys are needed.
"""

import os
import sys
import io
import sqlite3
import json

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# --- Force test config before importing project modules ---
os.environ["DB_PATH"] = ":memory:"
os.environ["AI_PROVIDER"] = "gemini"
os.environ["GEMINI_API_KEY"] = "test-key"

# Patch config module values directly after import
import config
config.DB_PATH = ":memory:"

# Since database.py uses DB_PATH at module level via get_connection,
# we need to patch get_connection to use an in-memory db with shared cache
# so that all connections see the same data.
_shared_conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
_shared_conn.row_factory = sqlite3.Row
_shared_conn.execute("PRAGMA foreign_keys = ON;")

import database

# Override init_db and get_connection to use our shared connection
from contextlib import contextmanager

@contextmanager
def _patched_get_connection():
    yield _shared_conn

database.get_connection = _patched_get_connection

# Now import modules that depend on database
from search import find_customer_emails, combine_thread_text
import search
search.get_connection = _patched_get_connection  # patch search module's reference too

from summarize import summarize_text, SUMMARY_PROMPT_TEMPLATE, _truncate_if_needed
import summarize

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0

def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  -- {detail}")


# ──────────────────────────────────────────────────────────────
# 1. Database layer
# ──────────────────────────────────────────────────────────────
print("\n=== 1. Database Tests ===")

# Run schema creation directly on shared connection
database._set_schema_version(_shared_conn, 0)
_shared_conn.executescript(database.SCHEMA_V1)
for ver in sorted(database.MIGRATIONS.keys()):
    for stmt in database.MIGRATIONS[ver]:
        try:
            _shared_conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
database._set_schema_version(_shared_conn, database.CURRENT_SCHEMA_VERSION)
_shared_conn.commit()

# Verify tables exist
tables = [r[0] for r in _shared_conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()]
check("Tables created", "Customers" in tables and "Email_Threads" in tables,
      f"got {tables}")

# Verify schema_version table
check("Schema version table exists", "schema_version" in tables)
ver = _shared_conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
check("Schema version is current", ver["version"] == database.CURRENT_SCHEMA_VERSION,
      f"got {ver['version']}")

# Verify Message_ID column exists
cols = [r[1] for r in _shared_conn.execute("PRAGMA table_info(Email_Threads)").fetchall()]
check("Message_ID column exists", "Message_ID" in cols, f"columns: {cols}")

# Insert a customer
cid = database.get_or_create_customer(_shared_conn, "Jane Doe", "jane@example.com")
_shared_conn.commit()
check("Customer inserted", cid is not None and cid > 0, f"cid={cid}")

# Same email -> same id (dedup)
cid2 = database.get_or_create_customer(_shared_conn, "Jane Doe", "jane@example.com")
_shared_conn.commit()
check("Duplicate email returns same ID", cid == cid2)

# Name backfill: insert customer with no name, then with a name
cid3 = database.get_or_create_customer(_shared_conn, None, "noname@example.com")
_shared_conn.commit()
cid3b = database.get_or_create_customer(_shared_conn, "Got Name", "noname@example.com")
_shared_conn.commit()
check("Name backfill works", cid3 == cid3b)
row = _shared_conn.execute("SELECT Name FROM Customers WHERE Customer_ID = ?", (cid3,)).fetchone()
check("Name was updated", row["Name"] == "Got Name", f"got {row['Name']}")

# Insert email threads
database.insert_email_thread(_shared_conn, cid, "Hello", "Hi Jane, how are you?", "2024-01-01T10:00:00", "<msg001@test.com>")
database.insert_email_thread(_shared_conn, cid, "Follow up", "Just checking in.", "2024-01-02T10:00:00", "<msg002@test.com>")
database.insert_email_thread(_shared_conn, cid, "Invoice", "Here is your invoice.", "2024-01-03T10:00:00", "<msg003@test.com>")
_shared_conn.commit()

thread_count = _shared_conn.execute(
    "SELECT COUNT(*) as cnt FROM Email_Threads WHERE Customer_ID = ?", (cid,)
).fetchone()["cnt"]
check("Email threads inserted", thread_count == 3, f"got {thread_count}")

# ──────────────────────────────────────────────────────────────
# 2. Duplicate email protection (Message_ID)
# ──────────────────────────────────────────────────────────────
print("\n=== 2. Duplicate Email Protection ===")

# email_exists checks
check("email_exists returns True for existing ID", database.email_exists(_shared_conn, "<msg001@test.com>"))
check("email_exists returns False for unknown ID", not database.email_exists(_shared_conn, "<unknown@test.com>"))
check("email_exists returns False for None", not database.email_exists(_shared_conn, None))
check("email_exists returns False for empty string", not database.email_exists(_shared_conn, ""))

# Try inserting a duplicate
result = database.insert_email_thread(_shared_conn, cid, "Duplicate", "Should not insert", "2024-01-04T10:00:00", "<msg001@test.com>")
_shared_conn.commit()
check("Duplicate insert returns False", result is False)

new_count = _shared_conn.execute(
    "SELECT COUNT(*) as cnt FROM Email_Threads WHERE Customer_ID = ?", (cid,)
).fetchone()["cnt"]
check("Duplicate was NOT inserted", new_count == 3, f"got {new_count}")

# Insert without Message_ID (should always work)
result2 = database.insert_email_thread(_shared_conn, cid, "No ID", "No message ID", "2024-01-05T10:00:00", None)
_shared_conn.commit()
check("Insert without Message_ID succeeds", result2 is True)

# ──────────────────────────────────────────────────────────────
# 3. Database stats
# ──────────────────────────────────────────────────────────────
print("\n=== 3. Database Stats ===")

stats = database.get_stats(_shared_conn)
check("Stats returns customer count", stats["customers"] >= 1, f"got {stats['customers']}")
check("Stats returns email count", stats["emails"] >= 3, f"got {stats['emails']}")
check("Stats returns latest email date", stats["latest_email_date"] is not None)

# ──────────────────────────────────────────────────────────────
# 4. Search & combine
# ──────────────────────────────────────────────────────────────
print("\n=== 4. Search & Combine Tests ===")

results = find_customer_emails("jane")
check("Search by name finds customer", len(results) == 1)
check("Correct email returned", results[0]["email"] == "jane@example.com")
check("All threads returned", len(results[0]["threads"]) >= 3)

# Search by email
results2 = find_customer_emails("jane@example.com")
check("Search by email works", len(results2) == 1)

# Search with no match
results3 = find_customer_emails("nonexistent@nowhere.com")
check("No-match returns empty list", len(results3) == 0)

# Combine thread text
combined = combine_thread_text(results[0])
check("Combined text contains all subjects",
      "Hello" in combined and "Follow up" in combined and "Invoice" in combined)
check("Threads separated by divider", "---" in combined)
check("Threads ordered chronologically",
      combined.index("2024-01-01") < combined.index("2024-01-02") < combined.index("2024-01-03"))

# ──────────────────────────────────────────────────────────────
# 5. Summarize module
# ──────────────────────────────────────────────────────────────
print("\n=== 5. Summarize Tests ===")

# Empty input
empty_result = summarize_text("")
check("Empty text returns fallback message", "No email history" in empty_result)

empty_result2 = summarize_text("   ")
check("Whitespace-only returns fallback message", "No email history" in empty_result2)

# Invalid provider
try:
    summarize_text("some text", provider="invalid_provider")
    check("Invalid provider raises error", False, "no exception raised")
except ValueError as e:
    check("Invalid provider raises ValueError", "Unknown AI provider" in str(e))

# Prompt template formatting
prompt = SUMMARY_PROMPT_TEMPLATE.format(email_text="Test email content")
check("Prompt template formats correctly", "Test email content" in prompt)
check("Prompt contains instruction", "summary" in prompt.lower())

# Mock API call test
_api_called = False
_api_prompt = None

def _mock_gemini(prompt):
    global _api_called, _api_prompt
    _api_called = True
    _api_prompt = prompt
    return "Summary: Customer discussed invoicing and follow-ups."

# Temporarily replace the gemini caller
original_caller = summarize.PROVIDERS["gemini"]
summarize.PROVIDERS["gemini"] = _mock_gemini

try:
    result = summarize_text("Test email body", provider="gemini")
    check("Summarize calls the correct provider", _api_called)
    check("Provider receives the prompt", "Test email body" in _api_prompt)
    check("Summary returned correctly", "invoicing" in result)
finally:
    summarize.PROVIDERS["gemini"] = original_caller

# ──────────────────────────────────────────────────────────────
# 6. Token limit / truncation
# ──────────────────────────────────────────────────────────────
print("\n=== 6. Token Limit Guard ===")

# Short text — no truncation
short_text, was_truncated = _truncate_if_needed("Short text", "openai")
check("Short text not truncated", not was_truncated)
check("Short text unchanged", short_text == "Short text")

# Long text — should truncate
long_text = "A" * 200_000
truncated_text, was_truncated = _truncate_if_needed(long_text, "openai")
check("Long text gets truncated", was_truncated)
check("Truncated text is shorter", len(truncated_text) < len(long_text))
check("Truncation notice appended", "truncated" in truncated_text.lower())

# Test with separator-based truncation
separator_text = ("x" * 60_000) + "\n\n---\n\n" + ("y" * 60_000)
trunc, was_trunc = _truncate_if_needed(separator_text, "openai")
check("Separator-based truncation works for openai", was_trunc)

# Test that summary adds truncation warning
summarize.PROVIDERS["gemini"] = _mock_gemini
try:
    huge_text = "B" * 1_000_000
    result = summarize_text(huge_text, provider="gemini")
    check("Truncated summary includes warning note", "truncated" in result.lower())
finally:
    summarize.PROVIDERS["gemini"] = original_caller

# ──────────────────────────────────────────────────────────────
# 7. fetch_emails.py functions (non-network)
# ──────────────────────────────────────────────────────────────
print("\n=== 7. Fetch Emails (Unit Tests) ===")

from fetch_emails import decode_mime_words, get_email_body
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# decode_mime_words
check("Plain text header decoded", decode_mime_words("Hello World") == "Hello World")
check("Empty header returns empty string", decode_mime_words("") == "")
check("None header returns empty string", decode_mime_words(None) == "")

# MIME-encoded header
encoded = "=?UTF-8?B?SGVsbG8gV29ybGQ=?="
check("MIME-encoded header decoded", decode_mime_words(encoded) == "Hello World")

# get_email_body -- plain text
plain_msg = MIMEText("This is a test email body.", "plain", "utf-8")
check("Plain text body extracted", get_email_body(plain_msg) == "This is a test email body.")

# get_email_body -- multipart
multi_msg = MIMEMultipart("alternative")
html_part = MIMEText("<p>HTML body</p>", "html", "utf-8")
text_part = MIMEText("Plain body", "plain", "utf-8")
multi_msg.attach(html_part)
multi_msg.attach(text_part)
body = get_email_body(multi_msg)
check("Multipart: plain text preferred over HTML", "Plain body" in body)

# get_email_body -- HTML-only multipart
html_only = MIMEMultipart()
html_only.attach(MIMEText("<p>Only HTML</p>", "html", "utf-8"))
body2 = get_email_body(html_only)
check("HTML fallback when no plain text", "Only HTML" in body2)

# fetch_unread_emails -- missing credentials
from fetch_emails import fetch_unread_emails
import fetch_emails

orig_account = fetch_emails.EMAIL_ACCOUNT
orig_password = fetch_emails.EMAIL_PASSWORD
fetch_emails.EMAIL_ACCOUNT = None
fetch_emails.EMAIL_PASSWORD = None

try:
    fetch_unread_emails()
    check("Missing credentials raises error", False, "no exception raised")
except RuntimeError as e:
    check("Missing credentials raises RuntimeError", "BCC_EMAIL_ACCOUNT" in str(e))
finally:
    fetch_emails.EMAIL_ACCOUNT = orig_account
    fetch_emails.EMAIL_PASSWORD = orig_password

# HTML-only bodies are converted to text
html_rich = MIMEText("<html><head><style>p{}</style></head><body><p>Hi&nbsp;there</p><p>Line two</p></body></html>", "html", "utf-8")
body3 = get_email_body(html_rich)
check("HTML converted to plain text", "<" not in body3 and "Hi there" in body3 and "Line two" in body3, repr(body3))

# Dates normalized to UTC
from fetch_emails import _normalize_date, pick_customer, _store_message
check("Date normalized to UTC", _normalize_date("Thu, 09 Jul 2026 04:57:09 -0700") == "2026-07-09T11:57:09+00:00",
      _normalize_date("Thu, 09 Jul 2026 04:57:09 -0700"))
check("Bad date kept as-is", _normalize_date("garbage") == "garbage")

# Outgoing BCC'd mail is filed under the recipient, not you
fetch_emails.MY_EMAIL_ADDRESSES = {"me@mycompany.com"}
out_msg = MIMEText("Quote attached.")
out_msg["From"] = "Me <me@mycompany.com>"
out_msg["To"] = "Bob Buyer <Bob@Customer.com>"
check("Outgoing mail -> recipient is customer", pick_customer(out_msg) == ("Bob Buyer", "bob@customer.com"),
      pick_customer(out_msg))
in_msg = MIMEText("Thanks!")
in_msg["From"] = "Bob Buyer <bob@customer.com>"
in_msg["To"] = "me@mycompany.com"
check("Incoming mail -> sender is customer", pick_customer(in_msg) == ("Bob Buyer", "bob@customer.com"))

# Messages without Message-ID are still deduplicated (content hash)
raw = b"From: nomsgid@example.com\r\nSubject: No ID\r\n\r\nbody"
check("First store of ID-less message inserts", _store_message(_shared_conn, raw) is True)
check("Second store of ID-less message is a duplicate", _store_message(_shared_conn, raw) is False)
_shared_conn.commit()

# Mixed timezone ordering: 23:00-05:00 (=04:00Z next day) must sort after 01:00Z that day
tzcid = database.get_or_create_customer(_shared_conn, "Tz Person", "tz_person@example.com")
database.insert_email_thread(_shared_conn, tzcid, "Later", "b", "2024-03-01T23:00:00-05:00", "<tz1>")
database.insert_email_thread(_shared_conn, tzcid, "Earlier", "a", "2024-03-02T01:00:00+00:00", "<tz2>")
_shared_conn.commit()
tz_threads = find_customer_emails("tz_person")[0]["threads"]
check("Mixed-timezone dates sort chronologically", [t["Subject"] for t in tz_threads] == ["Earlier", "Later"],
      [t["Subject"] for t in tz_threads])

# LIKE wildcards are matched literally
check("Underscore in search is literal", len(find_customer_emails("e_d")) == 0)  # would match "Jane Doe" as a wildcard
check("Percent in search is literal", len(find_customer_emails("%")) == 0)

# ──────────────────────────────────────────────────────────────
# 8. CLI module
# ──────────────────────────────────────────────────────────────
print("\n=== 8. CLI Module ===")

import cli
check("CLI module imports successfully", hasattr(cli, "main"))
check("CLI main is callable", callable(cli.main))

# ──────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────
print(f"\n{'=' * 40}")
print(f"Results: {PASS} passed, {FAIL} failed")
print(f"{'=' * 40}")

# Cleanup
_shared_conn.close()

sys.exit(1 if FAIL > 0 else 0)
