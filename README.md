# BCC Email AI — Customer History Summarizer

A small pipeline that watches a dedicated "BCC archive" mailbox, saves every
email into SQLite, and lets you pull up an AI-generated summary of your full
history with any customer by searching their name or email address.

Matches the 5-step plan:
1. `fetch_emails.py` — IMAP ingestion (Sender Name, Sender Email, Subject, Body)
2. `database.py` — SQLite schema (`Customers`, `Email_Threads`)
3. `search.py` — look up a customer and combine their threads into one string
4. `summarize.py` — send that string to an LLM (Anthropic, OpenAI, or Gemini)
5. `cli.py` / `app.py` — command-line and Streamlit front ends

## 1. Set up the archive mailbox

Use a dedicated inbox (e.g. `archive@yourcompany.com`) that you BCC on every
customer email. For Gmail:

1. Turn on 2-Step Verification on that account.
2. Go to **Google Account → Security → App passwords** and generate one for
   "Mail". Gmail no longer accepts your normal password for IMAP.
3. Make sure IMAP is enabled: **Gmail Settings → Forwarding and POP/IMAP →
   Enable IMAP**.

For other providers, use their IMAP hostname (e.g. Outlook is
`outlook.office365.com`) and an app password if 2FA is on.

## 2. Install and configure

Requires Python 3.10+. Always run commands from inside the project folder.

**Windows (PowerShell):**
```powershell
cd path\to\bcc_email_ai
py -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # only if activation is blocked
.venv\Scripts\Activate.ps1          # Command Prompt (cmd): .venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

**Mac/Linux:**
```bash
cd path/to/bcc_email_ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

In `.env`, fill in:
- `BCC_EMAIL_ACCOUNT` / `BCC_EMAIL_PASSWORD`: the archive mailbox and its app password
- `MY_EMAIL_ADDRESSES`: the address(es) you send customer email from
- `AI_PROVIDER` (`anthropic`, `openai`, or `gemini`) and the API key for that
  provider. You only need the one key. A free Gemini key is available at
  https://aistudio.google.com/apikey

Your prompt shows `(.venv)` once the environment is active. To skip
activation entirely, call `.venv\Scripts\python.exe` (Windows) or
`.venv/bin/python` (Mac/Linux) in place of `python`.

## 3. Fetch emails

```bash
python fetch_emails.py
```

This logs in, pulls every **unread** email in batches of 50, writes the
sender/subject/body into `emails.db`, and marks each batch as read only
*after* it has been committed, so an interrupted run never loses mail. Emails
you sent yourself (From one of `MY_EMAIL_ADDRESSES`) are filed under the
recipient. Re-running is safe: duplicates are skipped by Message-ID. Schedule
it to run periodically:

- **Linux/Mac (cron)**: `*/10 * * * * cd /path/to/bcc_email_ai && .venv/bin/python fetch_emails.py`
- **Windows**: use Task Scheduler to run the same command every N minutes.

## 4. Search and summarize

CLI:
```bash
python cli.py
# Enter Customer Email: jane.doe@example.com
```

Web app:
```bash
streamlit run app.py
```

Both do the same thing: find every `Email_Threads` row tied to that
customer, combine the bodies chronologically, and send them to the LLM with
the prompt:

> "You are an AI assistant. Read the following email history with this
> customer and provide a short summary of the key topics discussed, pending
> tasks, and final conclusions: [combined text]"

## Sharing this project

Share the code only. Before zipping or copying the folder, **leave out**:

| Don't share | Why |
|---|---|
| `.env` | Contains your mailbox app password and API key |
| `emails.db` (and `emails.db-wal`, `emails.db-shm`) | Contains all your archived emails |
| `.venv/` | Tied to your machine's Python paths; won't work elsewhere |
| `__pycache__/` | Generated files |

Each person then follows steps 1–2 above with their own mailbox, app
password, and AI key. Each copy has its own separate database. Sharing
through GitHub handles this for you, because `.gitignore` already excludes
all of these files.

## 5. Version control

```bash
git init
git add .
git commit -m "Initial commit: email ingestion, storage, search, and AI summary"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

`.gitignore` already excludes `.env` and the `*.db` file so secrets and
customer data never get pushed to GitHub.

## Notes / things to harden later

- `fetch_emails.py` currently treats every unread email in the mailbox as a
  new record — fine for a dedicated BCC-only inbox, but don't point it at a
  personal inbox with other mail.
- Sqlite is fine for one user / low volume; move to Postgres if multiple
  people need concurrent write access.
- Models are set by `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL` in
  `.env`. Gemini defaults to the `gemini-flash-latest` alias so a model
  retirement doesn't break summaries.
- API calls retry with exponential backoff on 429/5xx errors, and the web app
  caches summaries for an hour so repeat searches don't cost another call.
- Run the offline test suite with `python test_pipeline.py`.
