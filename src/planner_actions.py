"""ActionDispatcher: execute planner actions via Mattermost.

Each action type has a handler that sends a message and returns metadata.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ActionDispatcher:
    def __init__(self, memory, mattermost_client, llm_client):
        self.memory = memory
        self.mm = mattermost_client
        self.llm = llm_client

    def _thread_url(self, thread_id: str | None) -> str | None:
        """Build a permalink to a Mattermost thread, or None."""
        if not thread_id:
            return None
        mm_url = os.environ.get("MATTERMOST_URL", "")
        team = os.environ.get("MATTERMOST_TEAM_NAME", "")
        if mm_url and team:
            return f"{mm_url.rstrip('/')}/{team}/pl/{thread_id}"
        return None

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
            "generate_datamart_spec": self._generate_datamart_spec,
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
        """Ask a specific question to one or more people in a thread."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")

        # Support both target_users (list) and target_user (string, backward compat)
        target_users = action.get("target_users", [])
        if not target_users:
            single = action.get("target_user", "")
            if single:
                target_users = [single]

        # Clean @ prefixes
        target_users = [u.lstrip("@") for u in target_users]

        mentions = " ".join(f"@{u}" for u in target_users)
        message = f"{mentions}, {hint}" if mentions else hint

        # Add thread link
        url = self._thread_url(thread_id)
        if url:
            message += f"\n\n:point_right: [Перейти в тред]({url})"

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "ask_question",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "targets": target_users,
            "contract_id": contract_id,
        }

    def _propose_resolution(self, action: dict, initiative: dict) -> dict | None:
        """Generate 2-3 resolution options via LLM and post for stakeholder voting."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")

        try:
            options = self._generate_resolution_options(contract_id, hint)
        except Exception as e:
            logger.warning("LLM resolution generation failed for %s: %s", contract_id, e)
            options = None

        if not options:
            return self._propose_resolution_simple(action, initiative)

        # Format message with numbered options
        problem = options.get("problem_summary", hint)
        opts = options.get("options", [])

        number_emojis = ["1️⃣", "2️⃣", "3️⃣"]
        message_parts = [
            f":scales: **Разрешение конфликта** (`{contract_id}`)\n",
            f"**Проблема:** {problem}\n",
            "---\n",
        ]

        for i, opt in enumerate(opts[:3]):
            emoji = number_emojis[i]
            title = opt.get("title", f"Вариант {i + 1}")
            desc = opt.get("description", "")
            changes = opt.get("changes", "")
            pros = opt.get("pros", "")
            cons = opt.get("cons", "")

            message_parts.append(f"{emoji} **{title}**")
            if desc:
                message_parts.append(f"  {desc}")
            if changes:
                message_parts.append(f"  Изменения: {changes}")
            if pros:
                message_parts.append(f"  ✅ {pros}")
            if cons:
                message_parts.append(f"  ⚠️ {cons}")
            et_impact = opt.get("extra_time_impact")
            if et_impact:
                message_parts.append(f"  📊 Extra Time: {et_impact}")
            message_parts.append("")

        # Mention stakeholders
        stakeholders = initiative.get("stakeholders", [])
        mentions = " ".join(f"@{s}" for s in stakeholders) if stakeholders else ""
        message_parts.append("---\n")
        if mentions:
            message_parts.append(
                f"{mentions} — какой вариант предпочитаете? Напишите номер или предложите свой."
            )
        else:
            message_parts.append("Какой вариант предпочитаете? Напишите номер или предложите свой.")

        message = "\n".join(message_parts)

        resp = self.mm.send_to_channel(message, root_id=thread_id)

        # Save resolution options in discussion
        self._save_resolution_options(contract_id, opts, stakeholders)

        return {
            "action": "propose_resolution",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "contract_id": contract_id,
            "options_count": len(opts),
        }

    def _propose_resolution_simple(self, action: dict, initiative: dict) -> dict | None:
        """Fallback: simple resolution proposal without LLM-generated options."""
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

    def _generate_resolution_options(self, contract_id: str, hint: str) -> dict | None:
        """Call LLM to generate 2-3 resolution options based on contract contents."""
        from src.analyzer import MetricsAnalyzer
        from src.contract_summary import _extract_sections

        # Detect conflicts for this contract
        analyzer = MetricsAnalyzer(self.memory)
        conflicts = analyzer.detect_conflicts(only_contract_ids=[contract_id])
        if not conflicts:
            return None

        # Load involved contract contents
        involved_ids: set[str] = set()
        for c in conflicts:
            involved_ids.update(c.contracts)

        contract_snippets = {}
        for cid in involved_ids:
            md = self.memory.get_contract(cid) or self.memory.get_draft(cid) or ""
            if md:
                sections = _extract_sections(md)
                contract_snippets[cid] = {
                    "formula": sections.get("Формула", "")[:300],
                    "definition": sections.get("Определение", "")[:300],
                    "data_source": sections.get("Источник данных", "")[:200],
                    "extra_time": sections.get("Связь с Extra Time", "")[:200],
                }

        # Build conflict details
        conflict_details = []
        for c in conflicts:
            conflict_details.append({
                "type": c.type,
                "severity": c.severity,
                "title": c.title,
                "details": c.details[:300],
                "contracts": c.contracts,
            })

        # Load prompt and call LLM
        system_prompt = self.memory.read_file("prompts/conflict_resolution.md") or ""
        user_message = json.dumps({
            "contract_id": contract_id,
            "conflicts": conflict_details,
            "contract_contents": contract_snippets,
            "hint": hint,
        }, ensure_ascii=False, indent=2)

        response = self.llm.call_heavy(system_prompt, user_message, max_tokens=1500)

        # Parse response
        text = response.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            import re
            m = re.search(r'\{.*\}', response, re.DOTALL)
            if m:
                result = json.loads(m.group())
            else:
                return None

        options = result.get("options", [])
        if not options or not isinstance(options, list):
            return None

        return result

    def _save_resolution_options(self, contract_id: str, options: list, stakeholders: list):
        """Save generated resolution options into the discussion JSON."""
        try:
            discussion = self.memory.get_discussion(contract_id) or {}
            discussion["resolution_options"] = [
                {"index": i + 1, "title": opt.get("title", "")}
                for i, opt in enumerate(options[:3])
            ]
            discussion["stakeholder_votes"] = {s: None for s in stakeholders}
            discussion["resolution_status"] = "voting"
            self.memory.update_discussion(contract_id, discussion)
        except Exception as e:
            logger.warning("Failed to save resolution options for %s: %s", contract_id, e)

    def _partial_fix(self, action: dict, initiative: dict) -> dict | None:
        """Propose a fix for a specific section of a contract."""
        contract_id = action.get("contract_id", "")
        hint = action.get("message_hint", "")
        thread_id = initiative.get("thread_id")
        stakeholders = initiative.get("stakeholders", [])

        # Resolve human-readable contract name
        contract_name = contract_id
        for c in (self.memory.list_contracts() or []):
            if isinstance(c, dict) and str(c.get("id", "")).lower() == contract_id.lower():
                contract_name = c.get("name") or contract_id
                break

        mentions = " ".join(f"@{s}" for s in stakeholders) if stakeholders else ""

        message = f":wrench: **{contract_name}** — нужно доработать\n\n{hint}"
        if mentions:
            message += f"\n\n{mentions}, ваш ответ?"
        else:
            message += "\n\nСогласны с исправлением?"

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

        # Add thread link when sending to channel (not as reply in thread)
        url = self._thread_url(thread_id)
        if url:
            message += f"\n\n:point_right: [Перейти в тред]({url})"

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

    def _generate_datamart_spec(self, action: dict, initiative: dict) -> dict | None:
        """Generate and post a datamart specification for an agreed contract."""
        contract_id = action.get("contract_id", "")
        thread_id = initiative.get("thread_id")

        from src.tools import ToolExecutor
        executor = ToolExecutor(self.memory, self.mm, self.llm, thread_root_id=thread_id)
        result = executor._tool_generate_datamart_spec(contract_id)

        if result.get("error"):
            logger.warning("Datamart spec generation failed for %s: %s", contract_id, result["error"])
            return None

        spec = result.get("spec", "")
        if not spec:
            return None

        # Find data leads to mention
        data_leads = self._get_data_leads()
        if data_leads:
            mentions = ", ".join(f"@{u}" for u in data_leads)
            mention = f"\n\n{mentions}, прошу ознакомиться и взять в работу."
        else:
            mention = ""

        spec_preview = spec[:3000]
        message = (
            f"Техзадание на витрину данных для контракта `{contract_id}` "
            f"сгенерировано и сохранено в `specs/{contract_id}_datamart.md`.\n\n"
            f"```markdown\n{spec_preview}\n```{mention}"
        )

        resp = self.mm.send_to_channel(message, root_id=thread_id)
        return {
            "action": "generate_datamart_spec",
            "at": datetime.now(timezone.utc).isoformat(),
            "post_id": resp.get("id", ""),
            "contract_id": contract_id,
        }

    def _get_data_leads(self) -> list[str]:
        """Get all data lead usernames from roles."""
        for path in ("tasks/roles.json", "context/roles.json"):
            data = self.memory.read_json(path)
            if isinstance(data, dict):
                roles = data.get("roles", {})
                if isinstance(roles, dict):
                    leads = roles.get("data_lead", [])
                    if isinstance(leads, list) and leads:
                        return [str(u) for u in leads]
        return []
