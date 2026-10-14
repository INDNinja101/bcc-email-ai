"""
Step 3: The Search Function (Information Retrieval)

Given a name or email address, finds the matching customer(s) and every
Email_Threads row on file for them, then combines the message bodies into
one big string ready for summarization.
"""

from database import get_connection


def find_customer_emails(search_term):
    """
    Case-insensitive partial match against Name or Email_Address.
    Returns a list of dicts: {customer_id, name, email, threads: [...]}
    threads are ordered oldest -> newest so the summary reads chronologically.
    """
    term = search_term.lower().strip()
    # Escape LIKE wildcards so e.g. "john_doe" matches literally.
    term = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    search_pattern = f"%{term}%"

    with get_connection() as conn:
        # One query for customers + threads (instead of one query per customer).
        # datetime() converts mixed timezone offsets to UTC so ordering is truly chronological.
        rows = conn.execute(
            """
            SELECT c.Customer_ID, c.Name, c.Email_Address,
                   t.Thread_ID, t.Subject, t.Message_Body, t.Date
            FROM Customers c
            LEFT JOIN Email_Threads t ON t.Customer_ID = c.Customer_ID
            WHERE LOWER(c.Email_Address) LIKE ? ESCAPE '!' OR LOWER(c.Name) LIKE ? ESCAPE '!'
            ORDER BY c.Customer_ID, COALESCE(datetime(t.Date), t.Date) ASC
            """,
            (search_pattern, search_pattern),
        ).fetchall()

    results = {}
    for row in rows:
        record = results.setdefault(row["Customer_ID"], {
            "customer_id": row["Customer_ID"],
            "name": row["Name"],
            "email": row["Email_Address"],
            "threads": [],
        })
        if row["Thread_ID"] is not None:  # customer with no emails yet
            record["threads"].append({
                "Subject": row["Subject"],
                "Message_Body": row["Message_Body"],
                "Date": row["Date"],
            })

    return list(results.values())


def combine_thread_text(customer_record):
    """Joins every message body for one customer into a single string for the LLM prompt."""
    parts = []
    for thread in customer_record["threads"]:
        parts.append(
            f"Date: {thread['Date']}\nSubject: {thread['Subject']}\n\n{thread['Message_Body']}"
        )
    return "\n\n---\n\n".join(parts)
