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

# ── Continuous Planner ────────────────────────────────────────────────────────
PLANNER_RUN_TIME = os.environ.get("PLANNER_RUN_TIME", "09:00")
PLANNER_WORKDAYS = [int(d) for d in os.environ.get("PLANNER_WORKDAYS", "0,1,2,3,4").split(",")]
PLANNER_MAX_ACTIVE_INITIATIVES = _int("PLANNER_MAX_ACTIVE_INITIATIVES", 3)
PLANNER_MAX_NEW_THREADS_PER_DAY = _int("PLANNER_MAX_NEW_THREADS_PER_DAY", 2)
PLANNER_MAX_MESSAGES_PER_DAY = _int("PLANNER_MAX_MESSAGES_PER_DAY", 8)
PLANNER_MAX_ACTIONS_PER_INITIATIVE_PER_DAY = _int("PLANNER_MAX_ACTIONS_PER_INITIATIVE_PER_DAY", 2)
PLANNER_COOLDOWN_HOURS = _int("PLANNER_COOLDOWN_HOURS", 48)
PLANNER_WAIT_BEFORE_FOLLOWUP_HOURS = _int("PLANNER_WAIT_BEFORE_FOLLOWUP_HOURS", 24)
PLANNER_STALE_INITIATIVE_DAYS = _int("PLANNER_STALE_INITIATIVE_DAYS", 14)

# ── Dashboard ───────────────────────────────────────────────────────────────
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8050)

# ── LLM ──────────────────────────────────────────────────────────────────────
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 120)
