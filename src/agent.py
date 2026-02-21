import json
import logging
import re
from datetime import datetime, timezone

from src.router import route

logger = logging.getLogger(__name__)


def _extract_contract_name(markdown: str) -> str | None:
    """Best-effort extract human name from contract markdown.

    Expected heading: '# Data Contract: WIN NI'
    """
    for line in (markdown or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("# data contract:"):
            return line.split(":", 1)[1].strip() or None
        if line.startswith("#") and "Data Contract" in line:
            # fallback for variants
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip() or None
    return None

# Side-effect block patterns in LLM output
SIDE_EFFECT_PATTERNS = {
    "SAVE_CONTRACT": re.compile(
        r"\[SAVE_CONTRACT:(\w+)\]\n(.*?)\[/SAVE_CONTRACT\]", re.DOTALL
    ),
    "SAVE_DRAFT": re.compile(
        r"\[SAVE_DRAFT:(\w+)\]\n(.*?)\[/SAVE_DRAFT\]", re.DOTALL
    ),
    "UPDATE_DISCUSSION": re.compile(
        r"\[UPDATE_DISCUSSION:(\w+)\]\n(.*?)\[/UPDATE_DISCUSSION\]", re.DOTALL
    ),
    "ADD_REMINDER": re.compile(
        r"\[ADD_REMINDER\]\n(.*?)\[/ADD_REMINDER\]", re.DOTALL
    ),
    "UPDATE_PARTICIPANT": re.compile(
        r"\[UPDATE_PARTICIPANT:(\w+)\]\n(.*?)\[/UPDATE_PARTICIPANT\]", re.DOTALL
    ),
    "SAVE_DECISION": re.compile(
        r"\[SAVE_DECISION\]\n(.*?)\[/SAVE_DECISION\]", re.DOTALL
    ),
}

ONBOARD_TEMPLATE = """Привет, {display_name}! Я AI-архитектор метрик в канале Data Contracts.
Помогаю команде согласовывать определения данных и метрик.

Расскажи коротко:
1. Какая у тебя роль? За какой круг/домен отвечаешь?
2. Какие данные и метрики используешь чаще всего?
3. Есть ли боли с данными, которые хотелось бы решить?"""

PARTICIPANT_TEMPLATE = """# {display_name} (@{username})

## Базовое
- В канале с: {date}

## Домен и данные
- Метрики: (не заполнено)

## Профиль коммуникации
- Скорость ответа: неизвестно

## Позиции по контрактам
(нет данных)
"""


class Agent:
    def __init__(self, llm_client, memory, mattermost_client):
        self.llm = llm_client
        self.memory = memory
        self.mm = mattermost_client

    def process_message(
        self,
        username: str,
        message: str,
        channel_type: str,
        thread_context: str | None,
        post_id: str | None = None,
    ) -> str:
        """Process an incoming message and return reply text."""
        # 1. Route
        route_data = route(self.llm, self.memory, username, message, channel_type, thread_context)
        # keep channel type for side-effect policy
        route_data["channel_type"] = channel_type

        # Fast-path: contract history/version rendering without LLM
        if route_data.get("type") == "contract_history":
            cid = route_data.get("entity")
            items = self.memory.get_contract_history(cid) if cid else []
            if not items:
                return f"История версий для контракта `{cid}` не найдена. (Нет history.jsonl)"
            # newest last in our history.jsonl; show tail
            tail = items[-10:]
            lines = [f"История версий `{cid}` (последние {len(tail)}):", ""]
            for it in tail:
                sha = (it.get("sha256") or "")[:12]
                lines.append(f"- `{it.get('ts')}` — {it.get('kind')} — sha {sha} — {it.get('bytes')} bytes")
            lines.append("\nЧтобы посмотреть конкретную версию: `покажи версию <contract_id> <ts>`")
            return "\n".join(lines)

        if route_data.get("type") == "contract_version":
            ent = route_data.get("entity") or ""
            if ":" not in ent:
                return "Неверный формат. Используй: `покажи версию <contract_id> <ts>`"
            cid, ts = ent.split(":", 1)
            md = self.memory.get_contract_version(cid, ts)
            if not md:
                return f"Версия не найдена: `{cid}` `{ts}`"
            return f"Версия `{cid}` `{ts}`:\n\n```markdown\n{md}\n```"

        # 2. Load system prompt
        if route_data["model"] == "cheap":
            system_prompt = self.memory.read_file("prompts/system_short.md") or ""
        else:
            system_prompt = self.memory.read_file("prompts/system_full.md") or ""

        # 3. Load context files
        load_files = route_data.get("load_files", [])
        context_files = self.memory.load_files(load_files) if load_files else ""

        # Always load participant profile if available
        participant_profile = self.memory.get_participant(username) or ""
        if participant_profile:
            context_files += f"\n\n--- participants/{username}.md ---\n{participant_profile}"

        # Build full system prompt
        full_system = system_prompt
        if context_files:
            full_system += "\n\n# Загруженный контекст\n\n" + context_files

        # 4. Build user message
        user_msg = f"@{username}: {message}"
        if thread_context:
            user_msg = f"Контекст треда:\n{thread_context}\n\nНовое сообщение:\n{user_msg}"

        # 5. Call LLM
        if route_data["model"] == "cheap":
            raw_response = self.llm.call_cheap(full_system, user_msg)
        else:
            raw_response = self.llm.call_heavy(full_system, user_msg)

        # 6. Parse side effects and clean reply
        reply_text = self._handle_side_effects(raw_response, route_data, user_message=message)

        return reply_text

    def onboard_participant(self, user_id: str, username: str, display_name: str) -> None:
        """Create basic profile and send welcome DM."""
        # Check if profile already exists
        existing = self.memory.get_participant(username)
        if existing:
            logger.info("Participant %s already has a profile, skipping onboard", username)
            return

        # Create basic profile
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            self.memory.set_participant_active(username, True)
        except Exception:
            # best-effort
            pass
        profile = PARTICIPANT_TEMPLATE.format(
            display_name=display_name or username,
            username=username,
            date=now,
        )
        self.memory.update_participant(username, profile)
        logger.info("Created profile for %s", username)

        # Send welcome DM
        welcome = ONBOARD_TEMPLATE.format(
            display_name=display_name or username,
        )
        try:
            self.mm.send_dm(user_id, welcome)
            logger.info("Sent onboard DM to %s", username)
        except Exception as e:
            logger.error("Failed to send onboard DM to %s: %s", username, e)

    def _handle_side_effects(self, raw_response: str, route_data: dict, user_message: str = "") -> str:
        """Parse side-effect blocks from LLM output, execute them, return clean text.

        Safety rule: SAVE_CONTRACT/SAVE_DRAFT/SAVE_DECISION must happen only when the user explicitly asks
        to save/fix/update/create a contract (to avoid accidental writes during Q&A in threads).
        """
        reply = raw_response

        def allow_contract_write() -> bool:
            m = (user_message or "").lower()
            # explicit verbs/commands meaning "persist/change state"
            keywords = [
                "сохрани",
                "сохранить",
                "зафиксируй",
                "зафиксировать",
                "обнови",
                "обновить",
                "создай контракт",
                "создать контракт",
                "финальная версия",
                "согласован",
                "согласовать",
                "опубликуй финальную",
                "опубликовать финальную",
            ]
            explicit = any(k in m for k in keywords)

            # Only allow writes for contract lifecycle events
            allowed_types = {"new_contract_init", "contract_discussion", "problem_report"}
            type_ok = route_data.get("type") in allowed_types

            # In DM, never allow contract writes (profiles/reminders only)
            dm_block = route_data.get("channel_type") == "dm" or route_data.get("channel") == "dm"
            if dm_block:
                return False

            return explicit and type_ok

        can_write = allow_contract_write()

        # SAVE_CONTRACT
        for match in SIDE_EFFECT_PATTERNS["SAVE_CONTRACT"].finditer(raw_response):
            if not can_write:
                # Strip the side-effect block but do not execute
                reply = reply.replace(match.group(0), "")
                continue
            contract_id, content = match.group(1), match.group(2).strip()
            self.memory.save_contract(contract_id, content)
            name = _extract_contract_name(content) or contract_id
            self.memory.update_contract_index(contract_id, {
                "name": name,
                "status": "agreed",
                "file": f"contracts/{contract_id}.md",
                "agreed_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })
            logger.info("Saved contract: %s", contract_id)
            reply = reply.replace(match.group(0), "")

        # SAVE_DRAFT
        for match in SIDE_EFFECT_PATTERNS["SAVE_DRAFT"].finditer(raw_response):
            if not can_write:
                reply = reply.replace(match.group(0), "")
                continue
            contract_id, content = match.group(1), match.group(2).strip()
            self.memory.save_draft(contract_id, content)
            self.memory.update_contract_index(contract_id, {
                "name": contract_id,
                "status": "draft",
                "file": f"drafts/{contract_id}.md",
            })
            logger.info("Saved draft: %s", contract_id)
            reply = reply.replace(match.group(0), "")

        # UPDATE_DISCUSSION
        for match in SIDE_EFFECT_PATTERNS["UPDATE_DISCUSSION"].finditer(raw_response):
            contract_id, content = match.group(1), match.group(2).strip()
            try:
                discussion = json.loads(content)
                self.memory.update_discussion(contract_id, discussion)
                logger.info("Updated discussion: %s", contract_id)
            except json.JSONDecodeError:
                logger.error("Invalid JSON in UPDATE_DISCUSSION for %s", contract_id)
            reply = reply.replace(match.group(0), "")

        # ADD_REMINDER
        for match in SIDE_EFFECT_PATTERNS["ADD_REMINDER"].finditer(raw_response):
            content = match.group(1).strip()
            try:
                reminder = json.loads(content)
                reminders = self.memory.get_reminders()
                reminders.append(reminder)
                self.memory.save_reminders(reminders)
                logger.info("Added reminder for %s", reminder.get("contract_id"))
            except json.JSONDecodeError:
                logger.error("Invalid JSON in ADD_REMINDER")
            reply = reply.replace(match.group(0), "")

        # UPDATE_PARTICIPANT
        for match in SIDE_EFFECT_PATTERNS["UPDATE_PARTICIPANT"].finditer(raw_response):
            username, content = match.group(1), match.group(2).strip()
            self.memory.update_participant(username, content)
            logger.info("Updated participant: %s", username)
            reply = reply.replace(match.group(0), "")

        # SAVE_DECISION
        for match in SIDE_EFFECT_PATTERNS["SAVE_DECISION"].finditer(raw_response):
            if not can_write:
                reply = reply.replace(match.group(0), "")
                continue
            content = match.group(1).strip()
            try:
                decision = json.loads(content)
                self.memory.save_decision(decision)
                logger.info("Saved decision for %s", decision.get("contract"))
            except json.JSONDecodeError:
                logger.error("Invalid JSON in SAVE_DECISION")
            reply = reply.replace(match.group(0), "")

        return reply.strip()
