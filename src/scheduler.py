from __future__ import annotations

import logging
import os
import re
import time
import threading
from datetime import datetime, timezone, timedelta

import schedule

from src.config import (
    REMINDER_STEP_DAYS,
    REMINDER_DEFAULT_INTERVAL_DAYS,
    OPEN_QUESTIONS_DIGEST_TIME,
    OPEN_QUESTIONS_DIGEST_WORKDAYS,
    MATTERMOST_TEAM_NAME,
)

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, agent, memory, mattermost_client, llm_client):
        self.agent = agent
        self.memory = memory
        self.mm = mattermost_client
        self.llm = llm_client
        self.escalation_user = os.environ.get("ESCALATION_USER", "alexey")
        self.reminder_hours = int(os.environ.get("REMINDER_CHECK_HOURS", "4"))

    def start(self):
        """Start the scheduler loop in the current thread."""
        schedule.every(self.reminder_hours).hours.do(self._check_reminders)
        schedule.every().friday.at("17:00").do(self._weekly_digest)
        schedule.every().tuesday.at("10:00").do(self._coverage_scan)
        schedule.every().day.at("03:00").do(self._cleanup_threads)
        schedule.every().day.at(OPEN_QUESTIONS_DIGEST_TIME).do(self._open_questions_digest)

        logger.info(
            "Scheduler started: reminders every %dh, digest Fri 17:00, coverage Tue 10:00, cleanup 03:00, open-questions %s",
            self.reminder_hours, OPEN_QUESTIONS_DIGEST_TIME,
        )

        while True:
            schedule.run_pending()
            time.sleep(60)

    def _check_reminders(self):
        """Check and send due reminders."""
        try:
            reminders = self.memory.get_reminders()
            if not reminders:
                return

            now = datetime.now(timezone.utc)
            updated = False

            for rem in reminders:
                next_str = rem.get("next_reminder")
                if not next_str:
                    continue

                next_dt = datetime.fromisoformat(next_str)
                if next_dt > now:
                    continue

                step = rem.get("escalation_step", 1)
                contract_id = rem.get("contract_id", "")
                target_user = rem.get("target_user", "")
                target_mm_user_id = rem.get("target_mm_user_id", "")
                thread_id = rem.get("thread_id")
                question = rem.get("question_summary", "")

                # Load reminder templates
                templates = self.memory.read_file("prompts/reminder_templates.md") or ""

                message = None

                def _render(text: str, ctx: dict[str, str]) -> str:
                    out = text
                    for k, v in (ctx or {}).items():
                        out = out.replace("{" + k + "}", v)
                    return out

                def apply(marker: str, base_text: str, ctx: dict[str, str]) -> str:
                    # Wrapper marker
                    if templates and marker in templates:
                        wrapped = templates.replace(marker, base_text)
                        return _render(wrapped, ctx).strip()
                    # No wrapper marker: still allow placeholders inside base_text
                    return _render(base_text, ctx).strip()

                if step == 1:
                    # Soft reminder — template substitution, no LLM (MVP)
                    base = (
                        f"@{target_user}, напоминаю — жду твоё мнение по {contract_id}. "
                        f"Можешь ответить коротко, даже одним предложением."
                    )
                    ctx = {
                        "TARGET_USER": f"@{target_user}",
                        "TARGET_USERNAME": target_user,
                        "CONTRACT_ID": contract_id,
                        "QUESTION": question,
                    }
                    message = apply("{SOFT_REMINDER}", base, ctx)

                    self._send_reminder_to_thread(thread_id, message)
                    rem["escalation_step"] = 2

                elif step == 2:
                    # Simplification — may need LLM for options
                    discussion = self.memory.get_discussion(contract_id)
                    if discussion and discussion.get("proposed_resolution"):
                        option_a = discussion["proposed_resolution"]
                        option_b = "Другой вариант (опиши)"
                        message = (
                            f"@{target_user}, упрощу. Два варианта:\n"
                            f"A — {option_a}\n"
                            f"B — {option_b}\n"
                            f"Напиши A или B, я дальше сам оформлю."
                        )
                    else:
                        # Use cheap model to generate options
                        prompt = (
                            f"Сформулируй два простых варианта для вопроса: {question}\n"
                            f"Контракт: {contract_id}\n"
                            f"Формат: A — ...\nB — ..."
                        )
                        options = self.llm.call_cheap(
                            "Ты помощник. Сформулируй кратко.", prompt
                        )
                        message = f"@{target_user}, упрощу.\n{options}\nНапиши A или B, я дальше сам оформлю."

                    # placeholders for A/B options (best-effort)
                    option_a = ""
                    option_b = ""
                    if "\nA" in message or "A —" in message:
                        # naive extract
                        for ln in message.splitlines():
                            if ln.strip().startswith("A"):
                                option_a = ln.split("—", 1)[-1].strip()
                            if ln.strip().startswith("B"):
                                option_b = ln.split("—", 1)[-1].strip()

                    ctx = {
                        "TARGET_USER": f"@{target_user}",
                        "TARGET_USERNAME": target_user,
                        "CONTRACT_ID": contract_id,
                        "QUESTION": question,
                        "OPTION_A": option_a,
                        "OPTION_B": option_b,
                    }
                    message = apply("{AB_REMINDER}", message, ctx)

                    self._send_reminder_to_thread(thread_id, message)
                    rem["escalation_step"] = 3

                elif step == 3:
                    # DM to the person
                    message = (
                        f"Привет. В канале Data Contracts жду твой ответ по {contract_id} — "
                        f"это блокирует согласование. Можешь ответить прямо здесь."
                    )
                    ctx = {
                        "TARGET_USER": f"@{target_user}",
                        "TARGET_USERNAME": target_user,
                        "CONTRACT_ID": contract_id,
                        "QUESTION": question,
                    }
                    message = apply("{DM_REMINDER}", message, ctx)
                    if target_mm_user_id:
                        try:
                            self.mm.send_dm(target_mm_user_id, message)
                        except Exception as e:
                            logger.error("Failed to send DM reminder to %s: %s", target_user, e)
                    rem["escalation_step"] = 4

                elif step >= 4:
                    # Escalation to controller
                    first_asked = rem.get("first_asked", "")
                    days = 0
                    if first_asked:
                        try:
                            first_dt = datetime.fromisoformat(first_asked)
                            days = (now - first_dt).days
                        except ValueError:
                            pass

                    message = (
                        f"@{self.escalation_user}, контракт {contract_id} заблокирован {days} дней. "
                        f"Жду ответа от @{target_user}. Нужна помощь."
                    )
                    ctx = {
                        "ESCALATION_USER": f"@{self.escalation_user}",
                        "TARGET_USER": f"@{target_user}",
                        "TARGET_USERNAME": target_user,
                        "CONTRACT_ID": contract_id,
                        "DAYS_BLOCKED": str(days),
                        "QUESTION": question,
                    }
                    message = apply("{ESCALATION_REMINDER}", message, ctx)
                    self._send_reminder_to_thread(thread_id, message)
                    # Keep step at 4, don't escalate further
                    rem["escalation_step"] = 5

                # Update next reminder (+2 days)
                rem["last_reminder"] = now.isoformat()
                rem["next_reminder"] = (now + timedelta(days=REMINDER_DEFAULT_INTERVAL_DAYS)).isoformat()
                updated = True

                logger.info(
                    "Sent reminder step %d for %s to @%s",
                    step, contract_id, target_user,
                )

            if updated:
                self.memory.save_reminders(reminders)

        except Exception as e:
            logger.error("Error in check_reminders: %s", e, exc_info=True)

    def _send_reminder_to_thread(self, thread_id: str | None, message: str):
        """Send a reminder to a thread or to the main channel."""
        try:
            if thread_id:
                self.mm.send_to_channel(message, root_id=thread_id)
            else:
                self.mm.send_to_channel(message)
        except Exception as e:
            logger.error("Failed to send reminder: %s", e)

    def _coverage_scan(self):
        """Periodic scan: find uncovered metrics and suggest contracts."""
        try:
            from src.suggestion_engine import SuggestionEngine

            engine = SuggestionEngine(self.memory, self.llm)
            if not engine.can_suggest_today():
                logger.info("Coverage scan: rate limit reached, skipping")
                return

            candidates = engine.coverage_scan()
            candidates = engine.filter_already_suggested(candidates)
            if not candidates:
                logger.info("Coverage scan: no new candidates")
                return

            msg = engine.format_coverage_message(candidates[:5])
            if msg:
                resp = self.mm.send_to_channel(msg)
                engine.record_suggestion(candidates[:2], "coverage_scan", resp.get("id"))
                logger.info("Coverage scan: suggested %d candidates", min(len(candidates), 5))

        except Exception as e:
            logger.error("Error in coverage_scan: %s", e, exc_info=True)

    def _cleanup_threads(self):
        """Remove expired entries from active_threads.json."""
        try:
            removed = self.memory.cleanup_expired_threads()
            if removed:
                logger.info("Thread cleanup: removed %d expired entries", removed)
        except Exception as e:
            logger.error("Error in cleanup_threads: %s", e, exc_info=True)

    def _weekly_digest(self):
        """Generate and publish weekly digest."""
        try:
            contracts = self.memory.list_contracts()
            queue = self.memory.get_queue()
            reminders = self.memory.get_reminders()

            template = self.memory.read_file("prompts/digest_template.md") or ""

            user_msg = template.format(
                contracts_index=_format_json(contracts),
                queue=_format_json(queue),
                reminders=_format_json(reminders),
            )

            system_prompt = self.memory.read_file("prompts/system_short.md") or ""
            digest = self.llm.call_heavy(system_prompt, user_msg, max_tokens=1500)

            self.mm.send_to_channel(digest)
            logger.info("Weekly digest published")

        except Exception as e:
            logger.error("Error in weekly_digest: %s", e, exc_info=True)

    def _open_questions_digest(self):
        """Post a daily digest of contracts with open questions."""
        try:
            # Workday guard
            today = datetime.now(timezone.utc).weekday()
            if today not in OPEN_QUESTIONS_DIGEST_WORKDAYS:
                logger.debug("Open questions digest: skipped (not a workday)")
                return

            contracts = self.memory.list_contracts()
            active_threads = self.memory.get_all_active_threads()
            mm_url = os.environ.get("MATTERMOST_URL", "")

            items: list[dict] = []
            for contract in contracts:
                contract_id = contract.get("id", "")
                status = contract.get("status", "")

                # Skip contracts without discussion
                discussion = self.memory.get_discussion(contract_id)
                if not discussion:
                    continue

                # Skip discussions marked as resolved/agreed
                disc_status = (discussion.get("status") or "").lower()
                if disc_status in ("resolved", "agreed", "consensus", "closed"):
                    continue

                blocker = discussion.get("blocker", "")
                # Treat "none", "нет", empty as no blocker
                if blocker and blocker.strip().lower() in ("none", "нет", "no", ""):
                    blocker = ""
                has_open = False
                waiting_on: set[str] = set()

                # Check blocker
                if blocker:
                    has_open = True
                    waiting_on |= _extract_mentions(blocker)

                # Check top-level open_questions
                open_questions = discussion.get("open_questions", [])
                if isinstance(open_questions, list) and open_questions:
                    has_open = True
                    for q in open_questions:
                        if isinstance(q, str):
                            waiting_on |= _extract_mentions(q)
                        elif isinstance(q, dict):
                            waiting_on |= _extract_mentions(q.get("text", ""))
                            waiting_on |= _extract_mentions(q.get("assigned_to", ""))

                # Check positions for per-user open_questions
                positions = discussion.get("positions", {})
                if isinstance(positions, dict):
                    for username, pos in positions.items():
                        if not isinstance(pos, dict):
                            continue
                        user_oq = pos.get("open_questions", [])
                        if isinstance(user_oq, list) and user_oq:
                            has_open = True
                            waiting_on.add(username)
                            for q in user_oq:
                                if isinstance(q, str):
                                    waiting_on |= _extract_mentions(q)

                # Check next_action
                next_action = discussion.get("next_action", "")
                if next_action and next_action.strip().lower() not in ("none", "нет", "no", ""):
                    action_mentions = _extract_mentions(next_action)
                    if action_mentions:
                        has_open = True
                        waiting_on |= action_mentions

                # Skip if agreed/approved and no open issues
                if status in ("agreed", "approved") and not has_open:
                    continue

                if not has_open:
                    continue

                # Build thread URL
                url = None
                root_post_id = active_threads.get(contract_id)
                if root_post_id and mm_url and MATTERMOST_TEAM_NAME:
                    url = f"{mm_url.rstrip('/')}/{MATTERMOST_TEAM_NAME}/pl/{root_post_id}"

                name = contract.get("name", contract_id)
                items.append({
                    "name": name,
                    "url": url,
                    "waiting_on": waiting_on,
                    "blocker": blocker if blocker else None,
                })

            if not items:
                logger.debug("Open questions digest: no items")
                return

            message = _format_open_questions_digest(items)
            self.mm.send_to_channel(message)
            logger.info("Open questions digest: posted %d items", len(items))

        except Exception as e:
            logger.error("Error in open_questions_digest: %s", e, exc_info=True)


def _extract_mentions(text: str) -> set[str]:
    """Extract @mentions from text."""
    if not text:
        return set()
    return set(re.findall(r"@([a-zA-Z0-9_.]+)", text))


def _format_open_questions_digest(items: list[dict]) -> str:
    """Format digest message from list of items.

    Each item: {name, url, waiting_on, blocker}
    """
    lines = ["### :clipboard: Дайджест открытых вопросов", ""]
    for item in items:
        name = item["name"]
        url = item.get("url")
        if url:
            lines.append(f"- **[{name}]({url})**")
        else:
            lines.append(f"- **{name}**")
        waiting = item.get("waiting_on")
        if waiting:
            mentions = ", ".join(f"@{u}" for u in sorted(waiting))
            lines.append(f"  Ожидаем: {mentions}")
        blocker = item.get("blocker")
        if blocker:
            lines.append(f"  Блокер: {blocker}")
        lines.append("")
    lines.append(f"_Всего контрактов с открытыми вопросами: {len(items)}_")
    return "\n".join(lines)


def _format_json(data) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, indent=2)
