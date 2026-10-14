"""
Central configuration for the BCC Email AI tool.
All values are read from environment variables (or a local .env file via python-dotenv)
so no secrets are ever hard-coded into the scripts.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional; if it's not installed, real environment
    # variables set some other way (shell, Docker, CI, etc.) still work.
    pass

# --- Email account (the dedicated BCC archive mailbox) ---
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
EMAIL_ACCOUNT = os.getenv("BCC_EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("BCC_EMAIL_PASSWORD")  # use an app password, never your real login password
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

# Your own sending addresses (comma-separated). When an archived email is FROM
# one of these, it's an outgoing email you BCC'd, so the customer is the
# recipient (To/Cc) rather than you. The archive account is always included.
MY_EMAIL_ADDRESSES = {
    a.strip().lower()
    for a in (os.getenv("MY_EMAIL_ADDRESSES", "") + "," + (EMAIL_ACCOUNT or "")).split(",")
    if a.strip()
}

# --- Database ---
# Relative paths are resolved against the project folder, so running from a
# different working directory (e.g. Task Scheduler) still uses the same database.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", "emails.db")
if DB_PATH != ":memory:" and not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(PROJECT_DIR, DB_PATH)

# --- AI summarization provider ---
# One of: "anthropic", "openai", "gemini"
AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").strip().lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Model names are overridable so a provider retiring a model doesn't require a
# code change. The Gemini default is an alias that always points at the
# current Flash model.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
SUMMARY_MAX_TOKENS = int(os.getenv("SUMMARY_MAX_TOKENS", "1024"))
