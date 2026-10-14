"""
Step 5: The User Interface (CLI)

Run:  python cli.py
Prompts for a customer email/name, looks up their full thread history,
and prints an AI-generated summary.
"""

import logging

from database import init_db
from search import find_customer_emails, combine_thread_text
from summarize import summarize_text


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    init_db()

    print("=== Customer Email History Summarizer ===")
    search_term = input("Enter Customer Email: ").strip()

    if not search_term:
        print("Please enter a search term.")
        return

    results = find_customer_emails(search_term)

    if not results:
        print(f"No customer found matching '{search_term}'.")
        return

    for customer in results:
        label = customer["name"] or "Unknown"
        print(f"\n--- {label} <{customer['email']}> ---")
        print(f"{len(customer['threads'])} email(s) on file.")

        combined_text = combine_thread_text(customer)
        if not combined_text:
            print("No email content to summarize.")
            continue

        print("Generating summary...")
        try:
            summary = summarize_text(combined_text)
        except Exception as e:
            print(f"Error generating summary: {e}")
            continue

        print("\nSummary:")
        print(summary)


if __name__ == "__main__":
    main()
