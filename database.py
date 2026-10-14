"""
Step 2: Storing the Data (The Database)

Two tables, as specified:
  Customers      (Customer_ID, Name, Email_Address)
  Email_Threads  (Thread_ID, Customer_ID, Subject, Message_Body, Date, Message_ID)

Plus a schema_version table for safe future migrations.
"""

import logging
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

logger = logging.getLogger(__name__)

# Current schema version — bump this when adding migrations.
CURRENT_SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS Customers (
    Customer_ID   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name          TEXT,
    Email_Address TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS Email_Threads (
    Thread_ID     INTEGER PRIMARY KEY AUTOINCREMENT,
    Customer_ID   INTEGER NOT NULL,
    Subject       TEXT,
    Message_Body  TEXT,
    Date          TEXT,
    FOREIGN KEY (Customer_ID) REFERENCES Customers (Customer_ID)
);

CREATE INDEX IF NOT EXISTS idx_customers_email ON Customers (Email_Address);
CREATE INDEX IF NOT EXISTS idx_threads_customer ON Email_Threads (Customer_ID);
"""

# Migrations keyed by target version number.
MIGRATIONS = {
    2: [
        # Add Message_ID column for duplicate detection
        "ALTER TABLE Email_Threads ADD COLUMN Message_ID TEXT;",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_message_id ON Email_Threads (Message_ID);",
    ],
}


@contextmanager
def get_connection():
    """Yields a sqlite3 connection with dict-like row access, committing on success."""
    # timeout: wait for a concurrent writer (e.g. a scheduled fetch) instead of failing immediately.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _get_schema_version(conn):
    """Returns the current schema version, or 0 if the version table doesn't exist."""
    try:
        row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn, version):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (version,))


def init_db():
    """
    Creates the tables if they don't already exist. Applies any pending
    migrations to bring the schema up to CURRENT_SCHEMA_VERSION.
    Safe to call every run.
    """
    with get_connection() as conn:
        # WAL lets the web app read while a fetch is writing, and survives crashes cleanly.
        if DB_PATH != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
        current = _get_schema_version(conn)

        if current == 0:
            # Fresh database — create everything from scratch.
            conn.executescript(SCHEMA_V1)
            # Apply structural additions from all migrations
            for ver in sorted(MIGRATIONS.keys()):
                for stmt in MIGRATIONS[ver]:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # column/index already exists
            _set_schema_version(conn, CURRENT_SCHEMA_VERSION)
            logger.info("Database created at schema version %d.", CURRENT_SCHEMA_VERSION)
        elif current < CURRENT_SCHEMA_VERSION:
            # Existing database — apply only the migrations it hasn't seen yet.
            for ver in sorted(MIGRATIONS.keys()):
                if ver > current:
                    logger.info("Applying migration to schema version %d...", ver)
                    for stmt in MIGRATIONS[ver]:
                        try:
                            conn.execute(stmt)
                        except sqlite3.OperationalError as e:
                            logger.debug("Migration statement skipped (already applied): %s", e)
            _set_schema_version(conn, CURRENT_SCHEMA_VERSION)
            logger.info("Database migrated to schema version %d.", CURRENT_SCHEMA_VERSION)
        else:
            logger.debug("Database is at schema version %d (up to date).", current)


def get_or_create_customer(conn, name, email_address):
    """
    Looks up a customer by email address; inserts a new row if not found.
    Returns the Customer_ID either way.
    """
    email_address = email_address.lower().strip()
    cur = conn.execute(
        "SELECT Customer_ID, Name FROM Customers WHERE Email_Address = ?",
        (email_address,),
    )
    row = cur.fetchone()
    if row:
        # Backfill the name if we previously stored this customer without one.
        if name and not row["Name"]:
            conn.execute(
                "UPDATE Customers SET Name = ? WHERE Customer_ID = ?",
                (name, row["Customer_ID"]),
            )
        return row["Customer_ID"]

    cur = conn.execute(
        "INSERT INTO Customers (Name, Email_Address) VALUES (?, ?)",
        (name, email_address),
    )
    return cur.lastrowid


def email_exists(conn, message_id):
    """Check whether an email with the given Message-ID is already stored."""
    if not message_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM Email_Threads WHERE Message_ID = ? LIMIT 1",
        (message_id,),
    ).fetchone()
    return row is not None


def insert_email_thread(conn, customer_id, subject, body, date, message_id=None):
    """
    Inserts an email thread. If message_id is provided and already exists,
    the insert is skipped (duplicate protection).
    Returns True if inserted, False if skipped as duplicate.
    """
    # The UNIQUE index on Message_ID does the duplicate check in the same statement.
    cur = conn.execute(
        """INSERT OR IGNORE INTO Email_Threads (Customer_ID, Subject, Message_Body, Date, Message_ID)
           VALUES (?, ?, ?, ?, ?)""",
        (customer_id, subject, body, date, message_id),
    )
    if cur.rowcount == 0:
        logger.debug("Skipping duplicate email (Message-ID: %s)", message_id)
        return False
    return True


def get_stats(conn=None):
    """Returns a dict of database statistics for the UI sidebar."""
    def _fetch(c):
        customer_count = c.execute("SELECT COUNT(*) FROM Customers").fetchone()[0]
        email_count = c.execute("SELECT COUNT(*) FROM Email_Threads").fetchone()[0]
        # datetime() normalizes mixed timezone offsets to UTC so MAX compares correctly.
        latest = c.execute("SELECT MAX(COALESCE(datetime(Date), Date)) FROM Email_Threads").fetchone()[0]
        return {
            "customers": customer_count,
            "emails": email_count,
            "latest_email_date": latest,
        }

    if conn is not None:
        return _fetch(conn)

    with get_connection() as c:
        return _fetch(c)
