"""
Step 4: AI Summarization (The Brain)

Sends the combined email text to an LLM with a strict summarization prompt.
Supports three interchangeable providers -- pick one with AI_PROVIDER in
your .env: "anthropic" (default), "openai", or "gemini".

Only the standard library (urllib) is used for the HTTP calls, so no extra
SDK installs are required.
"""

import json
import logging
import time
import urllib.request
import urllib.error

from config import (
    AI_PROVIDER, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY,
    ANTHROPIC_MODEL, OPENAI_MODEL, GEMINI_MODEL, SUMMARY_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

SUMMARY_PROMPT_TEMPLATE = (
    "You are an AI assistant. Read the following email history with this customer "
    "and provide a short summary of the key topics discussed, pending tasks, and "
    "final conclusions:\n\n{email_text}"
)

# Approximate character limits per provider to stay within context windows.
# These are conservative estimates (1 token ≈ 4 chars).
CHAR_LIMITS = {
    "anthropic": 380_000,  # Claude supports ~200K tokens
    "openai": 100_000,     # GPT-4o-mini supports ~128K tokens
    "gemini": 900_000,     # Gemini Flash supports ~1M tokens
}
DEFAULT_CHAR_LIMIT = 100_000

# Retry configuration for transient API errors (429, 500, 502, 503)
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}


def _post_json(url, payload, headers, timeout=120):
    """Makes an HTTP POST request with automatic retry on transient failures."""
    body = json.dumps(payload).encode("utf-8")

    for attempt in range(1, MAX_RETRIES + 1):
        # A Request can't be reliably re-sent after a failure, so build a fresh one each attempt.
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
                detail = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"API request failed ({e.code}): {detail}") from e
            error = f"API returned {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"API request failed after {MAX_RETRIES} attempts: {e}") from e
            error = f"Network error: {e}"

        wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
        logger.warning("%s. Retrying in %ds (attempt %d/%d)...", error, wait, attempt, MAX_RETRIES)
        time.sleep(wait)


def _truncate_if_needed(text, provider):
    """Truncates text to fit within the provider's context window, with a warning."""
    limit = CHAR_LIMITS.get(provider, DEFAULT_CHAR_LIMIT)
    if len(text) <= limit:
        return text, False

    logger.warning(
        "Email text is %d chars, which exceeds the %s limit of %d chars. Truncating.",
        len(text), provider, limit,
    )
    truncated = text[:limit]
    # Try to cut at a clean boundary (end of an email separator)
    last_sep = truncated.rfind("\n\n---\n\n")
    if last_sep > limit * 0.5:  # only if we keep at least half
        truncated = truncated[:last_sep]

    return truncated + "\n\n[... truncated due to length ...]", True


def _call_anthropic(prompt):
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": SUMMARY_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


def _call_openai(prompt):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": SUMMARY_MAX_TOKENS,
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
    )
    return data["choices"][0]["message"]["content"]


def _call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    # Key goes in a header, not the URL, so it can't leak into logs or error messages.
    data = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        {"contents": [{"parts": [{"text": prompt}]}]},
        {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
    )
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
        raise RuntimeError(f"Gemini returned no summary ({reason}).")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise RuntimeError(f"Gemini returned an empty summary (finishReason: {candidates[0].get('finishReason')}).")
    return text


PROVIDERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
}


def summarize_text(email_text, provider=None):
    """Sends email_text to the configured LLM and returns the summary string."""
    if not email_text or not email_text.strip():
        return "No email history found for this customer."

    provider = provider or AI_PROVIDER
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider '{provider}'. Choose from: {list(PROVIDERS)}")

    # Guard against exceeding context window
    email_text, was_truncated = _truncate_if_needed(email_text, provider)

    prompt = SUMMARY_PROMPT_TEMPLATE.format(email_text=email_text)
    summary = PROVIDERS[provider](prompt)

    if was_truncated:
        summary += "\n\n⚠️ Note: The email history was truncated to fit within the AI model's limits. Some older emails may not be reflected in this summary."

    return summary
