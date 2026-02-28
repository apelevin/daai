"""ActionDispatcher: execute planner actions via Mattermost.

Each action type has a handler that sends a message and returns metadata.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ActionDispatcher:
    def __init__(self, memory, mattermost_client, llm_client):
        self.memory = memory
        self.mm = mattermost_client
        self.llm = llm_client

    def execute(self, action: dict, initiative: dict) -> dict | None:
        """Execute an action and return result metadata, or None on failure.

        Args:
            action: {"type": str, "contract_id": str, "reason": str,
                     "message_hint": str, "target_user": str | None}
            initiative: current initiative dict from planner_state

        Returns:
            {"action": str, "at": str, "post_id": str, ...} or None
        """
        action_type = action.get("type", "")
        handler = {
            "start_thread": self._start_thread,
            "ask_question": self._ask_question,
            "propose_resolution": self._propose_resolution,
            "partial_fix": self._partial_fix,
            "follow_up": self._follow_up,
            "escalate": self._escalate,
        }.get(action_type)

        if not handler:
            logger.warning("Unknown planner action type: %s", action_type)
            return None

        try:
            return handler(action, initiative)
        except Exception as e:
            logger.error("Failed to execute planner action %s: %s", action_type, e, exc_info=True)
            return None

    def _start_thread(self, action: dict, initiative: dict) -> dict | None:
        """Create a new thread in the channel, tagging stakeholders."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        stakeholders = initiative.get("stakeholders", [])

        mentions = " ".join(f"@{s}" for s in stakeholders) if stakeholders else ""
        message = (
            f":dart: **{hint or f'Обсуждение контракта {contract_id}'}**\n\n"
            f"Контракт: `{contract_id}`\n"
            f"Причина: {action.get('reason', '')}\n"
        )
        if mentions:
            message += f"\n{mentions} — прошу вашего участия в обсуждении."

        resp = self.mm.send_to_channel(message)
        post_id = resp.get("id", "")

        if post_id:
            self.memory.set_active_thread(contract_id, post_id)

        return {
            "action": "start_thread",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": post_id,
            "contract_id": contract_id,
        }

    def _ask_question(self, action: dict, initiative: dict) -> dict | None:
        """Ask a specific question to a specific person in a thread."""
        contract_id = action.get("contract_id", "")
        target_user = action.get("target_user", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")

        if target_user and target_user.startswith("@"):
            target_user = target_user[1:]

        message = f"@{target_user}, {hint}" if target_user else hint

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "ask_question",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "target": target_user,
            "contract_id": contract_id,
        }

    def _propose_resolution(self, action: dict, initiative: dict) -> dict | None:
        """Propose a resolution to a conflict in the thread."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")

        message = (
            f":bulb: **Предложение по разрешению конфликта** (`{contract_id}`)\n\n"
            f"{hint}\n\n"
            f"Что думаете? Напишите в этом треде."
        )

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "propose_resolution",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "contract_id": contract_id,
        }

    def _partial_fix(self, action: dict, initiative: dict) -> dict | None:
        """Propose a fix for a specific section of a contract."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")

        message = (
            f":wrench: **Предложение по исправлению** (`{contract_id}`)\n\n"
            f"{hint}\n\n"
            f"Согласны с исправлением?"
        )

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "partial_fix",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "contract_id": contract_id,
        }

    def _follow_up(self, action: dict, initiative: dict) -> dict | None:
        """Follow up on a stale discussion."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")
        stakeholders = initiative.get("stakeholders", [])
        waiting_for = initiative.get("waiting_for", [])

        targets = waiting_for or stakeholders
        mentions = " ".join(f"@{s}" for s in targets) if targets else ""

        message = hint or f"Напоминаю об обсуждении контракта `{contract_id}`."
        if mentions:
            message = f"{mentions}, {message}"

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "follow_up",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "contract_id": contract_id,
        }

    def _escalate(self, action: dict, initiative: dict) -> dict | None:
        """Escalate to a manager."""
        import os

        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")
        escalation_user = os.environ.get("ESCALATION_USER", "alexey")

        message = (
            f"@{escalation_user}, нужна помощь с контрактом `{contract_id}`.\n\n"
            f"{hint}"
        )

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "escalate",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "contract_id": contract_id,
            "escalated_to": escalation_user,
        }
