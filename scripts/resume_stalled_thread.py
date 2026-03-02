#!/usr/bin/env python3
"""One-shot script: send a resume message to the stalled Recurring Income thread.

Run inside the container:
  docker exec daai-agent-1 python scripts/resume_stalled_thread.py

Or locally if .env is sourced.
"""

import json
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.mattermost_client import MattermostClient
from src.memory import Memory

THREAD_ROOT_ID = "m1q1s3j6ztbxu853yckhdqrihc"
CONTRACT_ID = "recurring_income"


def main():
    memory = Memory()
    mm = MattermostClient()

    # Load discussion state
    discussion = memory.get_discussion(CONTRACT_ID)
    draft_raw = memory.read_file(f"drafts/{CONTRACT_ID}.md")

    # Build message
    parts = [
        "Эвелина, спасибо за ответы! Вот что было зафиксировано:",
        "",
    ]

    if draft_raw:
        parts.append(f"- Создан черновик **{CONTRACT_ID}**")

    if discussion:
        if discussion.get("open_questions"):
            parts.append("")
            parts.append("**Открытые вопросы:**")
            for q in discussion["open_questions"]:
                if isinstance(q, str):
                    parts.append(f"- {q}")
                elif isinstance(q, dict):
                    parts.append(f"- {q.get('question', q)}")

        if discussion.get("decisions"):
            parts.append("")
            parts.append("**Принятые решения:**")
            for d in discussion["decisions"]:
                if isinstance(d, str):
                    parts.append(f"- {d}")
                elif isinstance(d, dict):
                    parts.append(f"- {d.get('decision', d)}")

    parts.append("")
    parts.append("Когда будет удобно обсудить детали — напишите, продолжим!")

    message = "\n".join(parts)

    print("--- Message to send ---")
    print(message)
    print("--- Sending to thread", THREAD_ROOT_ID, "---")

    resp = mm.send_to_channel(message, root_id=THREAD_ROOT_ID)
    print(f"Sent! post_id={resp['id']}")


if __name__ == "__main__":
    main()
