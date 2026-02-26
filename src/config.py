"""Centralized configuration — all tunable constants in one place.

Each setting reads from an env variable with a default.
Import from here instead of hardcoding values.
"""

from __future__ import annotations

import os


def _int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


# ── Thread context ───────────────────────────────────────────────────────────
THREAD_MAX_MESSAGES = _int("THREAD_MAX_MESSAGES", 15)
THREAD_MAX_CHARS = _int("THREAD_MAX_CHARS", 4000)
THREAD_TTL_DAYS = _int("THREAD_TTL_DAYS", 7)

# ── Listener dedup ───────────────────────────────────────────────────────────
DEDUP_TTL_SECONDS = _int("DEDUP_TTL_SECONDS", 86400)  # 24 hours
DEDUP_MAX_ENTRIES = _int("DEDUP_MAX_ENTRIES", 4000)

# ── Memory I/O ───────────────────────────────────────────────────────────────
WRITE_MAX_RETRIES = _int("WRITE_MAX_RETRIES", 3)
WRITE_BACKOFF_BASE = _float("WRITE_BACKOFF_BASE", 0.5)

# ── Scheduler / reminders ────────────────────────────────────────────────────
REMINDER_STEP_DAYS = {1: 2, 2: 4, 3: 6, 4: 8}
REMINDER_DEFAULT_INTERVAL_DAYS = _int("REMINDER_DEFAULT_INTERVAL_DAYS", 2)

# ── Governance ───────────────────────────────────────────────────────────────
GOVERNANCE_REVIEW_THRESHOLD_DAYS = _int("GOVERNANCE_REVIEW_THRESHOLD_DAYS", 180)

# ── Suggestion engine ────────────────────────────────────────────────────────
SUGGESTION_COOLDOWN_DAYS = _int("SUGGESTION_COOLDOWN_DAYS", 14)
SUGGESTION_DISMISS_COOLDOWN_DAYS = _int("SUGGESTION_DISMISS_COOLDOWN_DAYS", 30)
SUGGESTION_MAX_PER_DAY = _int("SUGGESTION_MAX_PER_DAY", 1)

# ── LLM ──────────────────────────────────────────────────────────────────────
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 120)
