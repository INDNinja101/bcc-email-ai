"""
Step 5 (web version): Streamlit UI

Run:  streamlit run app.py
"""

import logging
import streamlit as st

from database import init_db, get_stats, get_connection
from search import find_customer_emails, combine_thread_text
from summarize import summarize_text
from config import AI_PROVIDER, EMAIL_ACCOUNT

# Set up logging for the app
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ──────────────────────────────────────────────────────────────
# Page config — must be first Streamlit call
# ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Customer Email Summarizer",
    page_icon="\U0001F4E7",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Ensure database exists (once per server process, not on every rerun)
st.cache_resource(init_db)()


@st.cache_data(show_spinner=False, ttl=3600, max_entries=200)
def cached_summary(combined_text, provider):
    """Same email history + provider -> reuse the summary instead of paying for another API call."""
    return summarize_text(combined_text, provider)

# ──────────────────────────────────────────────────────────────
# Custom CSS for a premium look
# ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* --- Global refinements --- */
    .stApp {
        background: linear-gradient(160deg, #0f0c29 0%, #1a1a3e 40%, #24243e 100%);
    }

    /* Main container */
    .main .block-container {
        padding-top: 2rem;
        max-width: 900px;
    }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 16px 20px;
        backdrop-filter: blur(10px);
    }
    [data-testid="stMetricValue"] {
        font-size: 1.8rem;
        font-weight: 700;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }

    /* Search input */
    .stTextInput > div > div > input {
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 10px;
        padding: 14px 18px;
        font-size: 1.05rem;
        color: #e0e0e0;
        transition: border-color 0.3s ease, box-shadow 0.3s ease;
    }
    .stTextInput > div > div > input:focus {
        border-color: #667eea;
        box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.25);
    }

    /* Primary button */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        border: none;
        border-radius: 10px;
        padding: 10px 32px;
        font-weight: 600;
        font-size: 1rem;
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
    }
    .stButton > button[kind="primary"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(102, 126, 234, 0.45);
    }

    /* Secondary button */
    .stButton > button[kind="secondary"] {
        background: rgba(255, 255, 255, 0.06);
        color: #c0c0c0;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 10px;
        transition: all 0.3s ease;
    }
    .stButton > button[kind="secondary"]:hover {
        background: rgba(255, 255, 255, 0.1);
        border-color: #667eea;
    }

    /* Expander */
    .streamlit-expanderHeader {
        background: rgba(255, 255, 255, 0.04);
        border-radius: 8px;
        font-weight: 500;
    }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #151530 0%, #1a1a3e 100%);
        border-right: 1px solid rgba(255, 255, 255, 0.06);
    }
    [data-testid="stSidebar"] .stMarkdown h2 {
        font-size: 1.1rem;
        text-transform: uppercase;
        letter-spacing: 1.5px;
        color: #888;
        margin-top: 1.5rem;
    }

    /* Alert / info boxes */
    .stAlert {
        border-radius: 10px;
    }

    /* Summary card styling */
    /* Divider */
    hr {
        border-color: rgba(255, 255, 255, 0.06);
    }

    /* Hide Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────
# Sidebar — stats & controls
# ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## \U0001F4CA Dashboard")

    stats = get_stats()
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Customers", stats["customers"])
    with col2:
        st.metric("Emails", stats["emails"])

    if stats["latest_email_date"]:
        date_display = stats["latest_email_date"][:10] if stats["latest_email_date"] else "—"
        st.caption(f"\U0001F4C5 Latest email: **{date_display}**")
    else:
        st.caption("\U0001F4ED No emails in database yet")

    st.divider()

    st.markdown("## \u2699\uFE0F Settings")
    st.caption(f"**AI Provider:** `{AI_PROVIDER}`")
    if EMAIL_ACCOUNT:
        masked = EMAIL_ACCOUNT[:3] + "***" + EMAIL_ACCOUNT[EMAIL_ACCOUNT.index("@"):] if "@" in EMAIL_ACCOUNT else "configured"
        st.caption(f"**Mailbox:** `{masked}`")
    else:
        st.caption("**Mailbox:** \u26A0\uFE0F Not configured")

    st.divider()

    st.markdown("## \U0001F4E5 Fetch Emails")
    st.caption("Pull new unread emails from the BCC archive mailbox.")

    fetch_col1, fetch_col2 = st.columns(2)
    with fetch_col1:
        fetch_btn = st.button("\U0001F504 Fetch Now", type="secondary", use_container_width=True)
    with fetch_col2:
        peek_mode = st.checkbox("Peek only", help="Don't mark emails as read on the server")

    if fetch_btn:
        if not EMAIL_ACCOUNT:
            st.error("Configure `BCC_EMAIL_ACCOUNT` and `BCC_EMAIL_PASSWORD` in your `.env` file first.")
        else:
            with st.spinner("Connecting to mailbox..."):
                try:
                    from fetch_emails import fetch_unread_emails
                    count = fetch_unread_emails(mark_as_seen=not peek_mode)
                    if count > 0:
                        st.success(f"\u2705 Fetched **{count}** new email(s)!")
                        st.rerun()
                    else:
                        st.info("No new unread emails found.")
                except Exception as e:
                    st.error(f"Fetch failed: {e}")

# ──────────────────────────────────────────────────────────────
# Main content
# ──────────────────────────────────────────────────────────────
st.markdown("""
# \U0001F4E7 Customer Email Summarizer
<p style="color: #999; font-size: 1.05rem; margin-top: -8px;">
Search by customer name or email to view their full history and an AI-powered summary.
</p>
""", unsafe_allow_html=True)

# Search bar
search_col, btn_col = st.columns([5, 1])
with search_col:
    search_term = st.text_input(
        "Search",
        placeholder="jane.doe@example.com or Jane Doe",
        label_visibility="collapsed",
    )
with btn_col:
    go = st.button("\U0001F50D Search", type="primary", use_container_width=True)

# ──────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────
if go:
    st.session_state["active_search"] = search_term.strip()
active_search = st.session_state.get("active_search", "")

if active_search:
    results = find_customer_emails(active_search)
    search_term = active_search

    if not results:
        st.warning(f"No customer found matching **'{search_term}'**.")
        st.caption("Try a partial name or email address.")

    for customer in results:
        label = customer["name"] or "Unknown"
        thread_count = len(customer["threads"])

        st.divider()

        # Customer header
        header_col, badge_col = st.columns([4, 1])
        with header_col:
            st.subheader(f"\U0001F464 {label}")
            st.caption(f"\u2709\uFE0F {customer['email']}")
        with badge_col:
            st.metric("Emails", thread_count)

        combined_text = combine_thread_text(customer)
        if not combined_text:
            st.info("No email content to summarize.")
            continue

        # AI Summary — rendered as plain markdown (never raw HTML), since it's
        # derived from untrusted email content.
        with st.spinner("Generating AI summary..."):
            try:
                summary = cached_summary(combined_text, AI_PROVIDER)
                with st.container(border=True):
                    st.markdown("#### \U0001F9E0 AI Summary")
                    st.markdown(summary)
            except Exception as e:
                st.error(f"Error generating summary: {e}")
                st.caption("Check your AI provider API key in the `.env` file.")

        # Raw email threads
        with st.expander(f"\U0001F4DC View {thread_count} raw email thread(s)"):
            for i, t in enumerate(customer["threads"]):
                if i > 0:
                    st.divider()
                date_display = t["Date"][:10] if t["Date"] and len(t["Date"]) >= 10 else t["Date"]
                st.markdown(f"**\U0001F4C5 {date_display}** — {t['Subject']}")
                st.text(t["Message_Body"])

elif go:
    st.warning("Enter a search term first.")
elif stats["emails"] == 0:
    # Show onboarding when database is empty
    st.markdown("---")
    st.markdown("""
    <div style="text-align: center; padding: 60px 20px;">
        <p style="font-size: 3rem; margin-bottom: 8px;">\U0001F4ED</p>
        <h3 style="color: #888;">No emails yet</h3>
        <p style="color: #666; max-width: 500px; margin: 0 auto;">
            Configure your BCC archive mailbox in <code>.env</code>, then click
            <strong>Fetch Now</strong> in the sidebar to pull your first batch of emails.
        </p>
    </div>
    """, unsafe_allow_html=True)
