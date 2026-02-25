import json
import logging
import re
from datetime import datetime, timezone

from src.router import route
from src.validator import validate_contract
from src.metrics_tree import mark_contract_agreed
from src.analyzer import MetricsAnalyzer, render_conflicts
from src.glossary import check_ambiguity
from src.relationships import detect_mentions, upsert_relationships
from src.relationships_llm import build_prompt as build_relationships_prompt, parse_and_validate as parse_relationships_llm
from src.governance import (
    find_contracts_requiring_review,
    render_review_report,
    ApprovalPolicy,
    check_approval_policy,
)
from src.lifecycle import set_status, ensure_in_review


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
# Side-effect block patterns in LLM output.
# We are tolerant to missing closing tags by stopping at the next side-effect marker or end of text.
_SIDE_EFFECT_STOP = r"(?=\n\[(?:SAVE_CONTRACT|SAVE_DRAFT|UPDATE_DISCUSSION|ADD_REMINDER|UPDATE_PARTICIPANT|SAVE_DECISION)(?::|\])|\Z)"

SIDE_EFFECT_PATTERNS = {
    "SAVE_CONTRACT": re.compile(
        rf"\[SAVE_CONTRACT:(\w+)\]\n(.*?)(?:\[/SAVE_CONTRACT\]|{_SIDE_EFFECT_STOP})",
        re.DOTALL,
    ),
    "SAVE_DRAFT": re.compile(
        rf"\[SAVE_DRAFT:(\w+)\]\n(.*?)(?:\[/SAVE_DRAFT\]|{_SIDE_EFFECT_STOP})",
        re.DOTALL,
    ),
    "UPDATE_DISCUSSION": re.compile(
        rf"\[UPDATE_DISCUSSION:(\w+)\]\n(.*?)(?:\[/UPDATE_DISCUSSION\]|{_SIDE_EFFECT_STOP})",
        re.DOTALL,
    ),
    "ADD_REMINDER": re.compile(
        rf"\[ADD_REMINDER\]\n(.*?)(?:\[/ADD_REMINDER\]|{_SIDE_EFFECT_STOP})",
        re.DOTALL,
    ),
    "UPDATE_PARTICIPANT": re.compile(
        rf"\[UPDATE_PARTICIPANT:(\w+)\]\n(.*?)(?:\[/UPDATE_PARTICIPANT\]|{_SIDE_EFFECT_STOP})",
        re.DOTALL,
    ),
    "SAVE_DECISION": re.compile(
        rf"\[SAVE_DECISION\]\n(.*?)(?:\[/SAVE_DECISION\]|{_SIDE_EFFECT_STOP})",
        re.DOTALL,
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

        # Lifecycle MVP: when a contract enters discussion/init, auto move draft->in_review
        try:
            if route_data.get("type") in {"new_contract_init", "contract_discussion", "problem_report"}:
                cid = (route_data.get("entity") or "").strip().lower()
                if cid:
                    index = self.memory.read_json("contracts/index.json") or {"contracts": []}
                    res = ensure_in_review(index, cid)
                    if res.ok and res.changed:
                        self.memory.write_json("contracts/index.json", index)
        except Exception:
            pass

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

        if route_data.get("type") == "conflicts_audit":
            analyzer = MetricsAnalyzer(self.memory)
            conflicts = analyzer.detect_conflicts()
            return render_conflicts(conflicts)

        if route_data.get("type") == "relationships_show":
            cid = (route_data.get("entity") or "").strip().lower()
            idx = self.memory.read_json("contracts/relationships.json") or {"relationships": []}
            items = idx.get("relationships") if isinstance(idx, dict) else []
            if not isinstance(items, list):
                items = []

            # Build id->name map
            name_map = {}
            for c in (self.memory.list_contracts() or []):
                if isinstance(c, dict) and c.get("id"):
                    name_map[str(c.get("id")).lower()] = c.get("name") or c.get("id")

            rels = [r for r in items if isinstance(r, dict) and (str(r.get("from") or "").lower()==cid or str(r.get("to") or "").lower()==cid)]
            if not rels:
                return f"Связей для `{cid}` не найдено."

            title = name_map.get(cid, cid)
            lines = [f"🔗 Связи для `{cid}` ({title}):", ""]
            for r in rels[:30]:
                f = str(r.get("from") or "").lower()
                t = str(r.get("to") or "").lower()
                ty = str(r.get("type") or "")
                desc = (r.get("description") or "").strip()

                arrow = "→"
                if ty == "inverse":
                    arrow = "↔"
                lines.append(f"- `{f}` {arrow} `{t}` — **{ty}**" + (f" — {desc}" if desc else ""))

            if len(rels) > 30:
                lines.append(f"…и ещё {len(rels)-30}")

            return "\n".join(lines)

        if route_data.get("type") == "governance_review_audit":
            items = find_contracts_requiring_review(self.memory.list_contracts())
            return render_review_report(items)

        if route_data.get("type") == "governance_policy_show":
            tier_key = (route_data.get("entity") or "").strip().lower()
            gov = self.memory.read_json("context/governance.json") or {}
            tiers = gov.get("tiers") if isinstance(gov, dict) else None
            if not isinstance(tiers, dict) or tier_key not in tiers:
                return f"Политика `{tier_key}` не найдена."
            cfg = tiers.get(tier_key) or {}
            req = cfg.get("approval_required") or []
            thr = cfg.get("consensus_threshold")
            desc = cfg.get("description") or ""

            roles = self.memory.read_json("context/roles.json") or {}
            roles_dict = roles.get("roles") if isinstance(roles, dict) else None

            lines = [f"📜 Политика согласования {tier_key}", ""]
            if desc:
                lines.append(desc)
                lines.append("")
            lines.append(f"Требуемые роли: {', '.join(req) if req else '(нет)'}")
            lines.append(f"Порог консенсуса: {thr}")
            lines.append("")
            if isinstance(roles_dict, dict):
                lines.append("Текущее назначение пользователей на роли:")
                for role in req:
                    users = roles_dict.get(role) or []
                    if isinstance(users, list):
                        u = ", ".join([f"@{x}" for x in users if isinstance(x, str)])
                        lines.append(f"- {role}: {u or '(не назначено)'}")
            return "\n".join(lines)

        if route_data.get("type") == "governance_requirements_for":
            cid = (route_data.get("entity") or "").strip().lower()
            tier_key = "tier_2"
            for c in (self.memory.list_contracts() or []):
                if isinstance(c, dict) and str(c.get("id") or "").lower() == cid and c.get("tier"):
                    tier_key = str(c.get("tier"))
                    break

            gov = self.memory.read_json("context/governance.json") or {}
            tiers = gov.get("tiers") if isinstance(gov, dict) else None
            cfg = tiers.get(tier_key) if isinstance(tiers, dict) else None
            if not isinstance(cfg, dict):
                return f"Не нашёл политику для `{cid}` (tier={tier_key})."

            req = cfg.get("approval_required") or []
            thr = cfg.get("consensus_threshold")
            desc = cfg.get("description") or ""
            lines = [f"✅ Требования согласования для `{cid}` (tier={tier_key})", ""]
            if desc:
                lines.append(desc)
                lines.append("")
            lines.append(f"Роли: {', '.join(req) if req else '(нет)'}")
            lines.append(f"Порог: {thr}")
            lines.append("\nПодсказка: добавь согласующих в секцию `## Согласовано` как `@username — дата`.")
            return "\n".join(lines)

        if route_data.get("type") == "lifecycle_get_status":
            cid = (route_data.get("entity") or "").strip().lower()
            status = None
            for c in (self.memory.list_contracts() or []):
                if isinstance(c, dict) and str(c.get("id") or "").lower() == cid:
                    status = c.get("status")
                    break
            if not status:
                return f"Статус для `{cid}` не найден."
            return f"Статус `{cid}`: **{status}**"

        if route_data.get("type") == "lifecycle_set_status":
            ent = (route_data.get("entity") or "")
            if ":" not in ent:
                return "Неверный формат. Используй: `поставь статус <id> <draft|in_review|approved|active|deprecated|archived>`"
            cid, st = ent.split(":", 1)
            index = self.memory.read_json("contracts/index.json") or {"contracts": []}
            res = set_status(index, cid, st)
            if not res.ok:
                return f"Не получилось: {res.message}"
            self.memory.write_json("contracts/index.json", index)
            return f"✅ {cid}: статус теперь **{st}**"

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
        reply_text, _info = self._handle_side_effects(raw_response, route_data, user_message=message)

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
            self.memory.set_participant_onboarded(username, True)
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

    def _handle_side_effects(self, raw_response: str, route_data: dict, user_message: str = "") -> tuple[str, dict]:
        """Parse side-effect blocks from LLM output, execute them, return clean text.

        Returns: (reply_text, info)
        info keys:
          - can_write: bool
          - saved_contracts: list[str]
          - saved_drafts: list[str]
          - saved_decisions: int

        Safety rule: SAVE_CONTRACT/SAVE_DRAFT/SAVE_DECISION must happen only when the user explicitly asks
        to save/fix/update/create a contract (to avoid accidental writes during Q&A in threads).
        """
        reply = raw_response
        info = {
            "can_write": False,
            "saved_contracts": [],
            "saved_drafts": [],
            "saved_decisions": 0,
        }

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
        info["can_write"] = can_write

        # SAVE_CONTRACT
        for match in SIDE_EFFECT_PATTERNS["SAVE_CONTRACT"].finditer(raw_response):
            if not can_write:
                # Strip the side-effect block but do not execute
                reply = reply.replace(match.group(0), "")
                continue

            contract_id, content = match.group(1), match.group(2).strip()

            report = validate_contract(content)
            if not report.ok:
                # Do not save; return actionable feedback
                reply = reply.replace(match.group(0), "")
                bullets = "\n".join([f"- {i.message}" for i in report.issues[:12]])
                more = "" if len(report.issues) <= 12 else f"\n- …и ещё {len(report.issues)-12}"
                return (
                    "⚠️ Контракт не сохраняю: он не проходит валидацию.\n\n"
                    "Что поправить:\n"
                    f"{bullets}{more}\n\n"
                    "После правок напиши: «сохрани финальную версию» или «зафиксируй контракт»."
                ).strip(), info

            # Governance tier approvals (MVP): if governance.json declares required roles for this tier,
            # enforce that the approvers listed in "## Согласовано" include all required roles for tier_1.
            try:
                gov = self.memory.read_json("context/governance.json") or {}
                tiers = (gov.get("tiers") or {}) if isinstance(gov, dict) else {}
                tier_key = "tier_2"  # default

                # allow explicit tier in index.json record if present
                idx_items = self.memory.list_contracts() or []
                for c in idx_items:
                    if isinstance(c, dict) and str(c.get("id") or "").lower() == contract_id.lower() and c.get("tier"):
                        tier_key = str(c.get("tier"))
                        break

                tier_cfg = tiers.get(tier_key) if isinstance(tiers, dict) else None
                if isinstance(tier_cfg, dict):
                    policy = ApprovalPolicy(
                        tier=tier_key,
                        approval_required=list(tier_cfg.get("approval_required") or []),
                        consensus_threshold=float(tier_cfg.get("consensus_threshold") or 1.0),
                    )

                    roles = self.memory.read_json("context/roles.json") or {}
                    role_map = {}
                    roles_dict = roles.get("roles") if isinstance(roles, dict) else None
                    if isinstance(roles_dict, dict):
                        for role, users in roles_dict.items():
                            if isinstance(users, list):
                                for u in users:
                                    if isinstance(u, str):
                                        role_map[u.lower()] = str(role)

                    check = check_approval_policy(contract_md=content, policy=policy, role_map=role_map)
                    if not check.ok:
                        missing = ", ".join(check.missing_roles) or "(неизвестно)"
                        reply = reply.replace(match.group(0), "")
                        return (
                            f"⚠️ Контракт не сохраняю: не выполнена политика согласования ({tier_key}).\n\n"
                            f"Не хватает ролей: {missing}.\n"
                            "Добавь нужных согласующих в секцию «## Согласовано», затем повтори: «зафиксируй контракт»."
                        ).strip(), info
            except Exception:
                pass

            # Glossary ambiguity check (best-effort): block save until clarified
            try:
                glossary = self.memory.read_json("context/glossary.json")
                issues = check_ambiguity(content, glossary)
                if issues:
                    reply = reply.replace(match.group(0), "")
                    bullets = "\n".join([f"- {i.message}" for i in issues])
                    return (
                        "⚠️ Контракт не сохраняю: нужно уточнение терминов по глоссарию.\n\n"
                        f"{bullets}\n\n"
                        "Ответь в треде, и я обновлю текст (или ты обновишь вручную, затем: «зафиксируй контракт»)."
                    ).strip(), info
            except Exception:
                # If glossary missing/invalid, do not block
                pass

            self.memory.save_contract(contract_id, content)
            info["saved_contracts"].append(contract_id)
            name = _extract_contract_name(content) or contract_id

            # Best-effort: detect and store relationships
            try:
                known_contracts = self.memory.list_contracts() or []
                known_ids = [c.get("id") for c in known_contracts if isinstance(c, dict) and c.get("id")]

                # (a) deterministic mentions by id
                rels = detect_mentions(contract_id=contract_id, contract_md=content, known_contract_ids=known_ids)

                # (b) LLM-assisted semantic relationships
                try:
                    system, user = build_relationships_prompt(contract_id=contract_id, contract_md=content, known_contracts=known_contracts)
                    raw = self.llm.call_heavy(system, user)
                    parsed = parse_relationships_llm(raw, contract_id=contract_id, known_ids=set([x for x in known_ids if isinstance(x, str)]))
                    for p in parsed:
                        rels.append(p)  # type: ignore
                except Exception as e:
                    logger.info("Relationships LLM skipped/failed: %s", e)

                if rels:
                    idx = self.memory.read_json("contracts/relationships.json") or {"relationships": []}
                    # rels may contain both Relationship and ProposedRelationship; normalize
                    normalized = []
                    for r in rels:
                        if hasattr(r, "from_id"):
                            normalized.append(r)
                        else:
                            # unknown type
                            pass

                    idx2, added = upsert_relationships(idx, normalized)  # type: ignore
                    if added:
                        self.memory.write_json("contracts/relationships.json", idx2)
                        logger.info("Relationships updated: +%d", added)
            except Exception as e:
                logger.warning("Failed to update relationships.json: %s", e)
            now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.memory.update_contract_index(contract_id, {
                "name": name,
                "status": "agreed",
                "file": f"contracts/{contract_id}.md",
                "agreed_date": now_date,
                "status_updated_at": now_date,
            })

            # Best-effort: mark the corresponding node in metrics_tree.md as agreed
            try:
                tree_text = self.memory.read_file("context/metrics_tree.md") or ""
                patch = mark_contract_agreed(tree_text, name)
                if not patch.ok:
                    patch = mark_contract_agreed(tree_text, contract_id)
                if patch.ok and patch.changed:
                    self.memory.write_file("context/metrics_tree.md", patch.new_text)
                    logger.info("Metrics tree updated: %s", patch.message)
            except Exception as e:
                logger.warning("Failed to update metrics_tree.md: %s", e)

            logger.info("Saved contract: %s", contract_id)
            reply = reply.replace(match.group(0), "")

        # SAVE_DRAFT
        for match in SIDE_EFFECT_PATTERNS["SAVE_DRAFT"].finditer(raw_response):
            if not can_write:
                reply = reply.replace(match.group(0), "")
                continue
            contract_id, content = match.group(1), match.group(2).strip()
            self.memory.save_draft(contract_id, content)
            info["saved_drafts"].append(contract_id)
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
                info["saved_decisions"] += 1
                logger.info("Saved decision for %s", decision.get("contract"))
            except json.JSONDecodeError:
                logger.error("Invalid JSON in SAVE_DECISION")
            reply = reply.replace(match.group(0), "")

        # If the user explicitly asked to save/finalize, but the model didn't emit SAVE_CONTRACT,
        # do a retry call that *must* output a SAVE_CONTRACT block based on the latest draft + discussion.
        try:
            explicit_save = can_write
            entity = (route_data.get("entity") or "").strip().lower()
            needs_contract = route_data.get("type") in {"contract_discussion", "new_contract_init", "problem_report"}
            if explicit_save and needs_contract and entity and not info["saved_contracts"]:
                draft = self.memory.get_draft(entity) or ""
                discussion = self.memory.get_discussion(entity) or {}
                system = (
                    "Ты помощник по Data Contracts. Тебе нужно строго выполнить сохранение контракта. "
                    "Ответь ТОЛЬКО блоком SAVE_CONTRACT в формате:\n"
                    "[SAVE_CONTRACT:<id>]\n<markdown контракта>\n[/SAVE_CONTRACT]\n\n"
                    "Без дополнительного текста. Контракт должен пройти детерминированную валидацию. "
                    "Обязательные секции (каждая непустая):\n"
                    "- ## Статус\n- ## Определение\n- ## Формула\n- ## Источник данных\n- ## Включает\n- ## Исключения\n- ## Гранулярность\n"
                    "- ## Ответственный за данные\n- ## Ответственный за расчёт\n- ## Связь с Extra Time\n- ## Потребители\n- ## Состояние данных\n- ## Известные проблемы\n"
                    "- ## Связанные контракты\n- ## Согласовано\n- ## История изменений\n\n"
                    "Требования к секции «Формула»: обязательно добавь строку 'Человеческая: ...' и блок 'Псевдо‑SQL: ...'.\n"
                    "Требования к «Связь с Extra Time»: обязательно путь вида 'X → ... → Extra Time' (с символом стрелки →)."
                )
                user = (
                    f"Contract id: {entity}\n\n"
                    f"Последний черновик (drafts/{entity}.md):\n{draft}\n\n"
                    f"Сводка обсуждения (drafts/{entity}_discussion.json):\n{json.dumps(discussion, ensure_ascii=False, indent=2)}\n\n"
                    "Сгенерируй финальную версию и сохрани её через SAVE_CONTRACT."
                )
                retry_raw = self.llm.call_heavy(system, user)
                # recurse once: parse retry output and execute
                retry_reply, retry_info = self._handle_side_effects(retry_raw, route_data, user_message=user_message)
                # merge info
                info["saved_contracts"].extend(retry_info.get("saved_contracts") or [])
                info["saved_decisions"] += int(retry_info.get("saved_decisions") or 0)
                # Prefer retry reply if it contains any user-visible content (normally empty)
                reply = retry_reply or reply
        except Exception as e:
            logger.warning("SAVE_CONTRACT retry failed: %s", e)

        return reply.strip(), info
