from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from src.router import route, HEAVY_TYPES
from src.analyzer import MetricsAnalyzer, render_conflicts
from src.governance import find_contracts_requiring_review, render_review_report
from src.lifecycle import set_status, ensure_in_review
from src.tool_definitions import get_tools_for_route
from src.tools import ToolExecutor


logger = logging.getLogger(__name__)


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


@dataclass
class ProcessResult:
    reply: str
    thread_root_id: str | None = None


# Route types that should be tracked to active threads
THREAD_TRACKING_TYPES = {"contract_discussion", "new_contract_init", "problem_report"}


class Agent:
    def __init__(self, llm_client, memory, mattermost_client):
        self.llm = llm_client
        self.memory = memory
        self.mm = mattermost_client

    def _build_thread_context(self, thread_posts: list[dict], exclude_post_id: str | None = None) -> str | None:
        """Build thread context string from a list of thread posts."""
        _THREAD_MAX_MESSAGES = 15
        _THREAD_MAX_CHARS = 4000

        context_parts = []
        for tp in thread_posts:
            if exclude_post_id and tp.get("id") == exclude_post_id:
                continue
            tp_user_id = tp.get("user_id", "")
            if tp_user_id == self.mm.bot_user_id:
                tp_name = "AI-архитектор"
            else:
                try:
                    tp_info = self.mm.get_user_info(tp_user_id)
                    tp_name = f"@{tp_info['username']}"
                except Exception:
                    tp_name = "unknown"
            context_parts.append(f"{tp_name}: {tp['message']}")

        # Keep last N messages
        if len(context_parts) > _THREAD_MAX_MESSAGES:
            context_parts = context_parts[-_THREAD_MAX_MESSAGES:]

        result = "\n".join(context_parts)

        # Truncate by chars, keep tail
        if len(result) > _THREAD_MAX_CHARS:
            result = "…(начало треда обрезано)\n" + result[-_THREAD_MAX_CHARS:]

        return result if result else None

    def _handle_role_assignments_inline(self, message: str) -> str | None:
        """Handle role assignment messages without router/LLM.

        Accepts lines like:
          Data Lead — @pavelpetrin
          Circle Lead — @Никита Корабовцев

        Persists canonical usernames to tasks/roles.json (runtime state).
        Returns a reply string if handled, else None.
        """
        import re

        lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
        if not lines:
            return None

        role_key_by_label = {
            "data lead": "data_lead",
            "circle lead": "circle_lead",
        }

        pairs: list[tuple[str, str]] = []

        for ln in lines:
            m = re.match(r"^(data\s*lead|circle\s*lead)\s*[—\-:]\s*(.+)$", ln, flags=re.IGNORECASE)
            if not m:
                continue
            label = re.sub(r"\s+", " ", m.group(1).strip().lower())
            role = role_key_by_label.get(label)
            rhs = m.group(2).strip()
            if not role or not rhs:
                continue

            # 1) Prefer explicit @username in latin
            m_user = re.search(r"@([a-z0-9_.\-]{3,})", rhs, flags=re.IGNORECASE)
            if m_user:
                pairs.append((role, m_user.group(1).lower()))
                continue

            # 2) Otherwise, try to resolve whatever comes after '@' (may contain spaces/cyrillic)
            raw = rhs
            if "@" in raw:
                raw = raw.split("@", 1)[1].strip()
            # Trim possible markdown/link artifacts
            raw = re.sub(r"[\]\[\(\)<>]", " ", raw).strip()

            resolved = None
            try:
                if hasattr(self.mm, "resolve_username"):
                    resolved = self.mm.resolve_username(raw)
            except Exception:
                resolved = None

            # If first attempt failed and raw contains a space, try the full multi-word string
            if not resolved and " " in raw:
                try:
                    if hasattr(self.mm, "resolve_username"):
                        resolved = self.mm.resolve_username(raw)
                except Exception:
                    resolved = None

            # If still unresolved, try just the first word (e.g. "Никита" from "Никита Корабовцев")
            if not resolved and " " in raw:
                try:
                    if hasattr(self.mm, "resolve_username"):
                        resolved = self.mm.resolve_username(raw.split()[0])
                except Exception:
                    resolved = None

            if resolved:
                pairs.append((role, str(resolved).lower()))
            else:
                # If we detected the role label but can't resolve the user, we should respond with guidance.
                return (
                    f"⚠️ Не смог распознать username пользователя «{raw}» для назначения роли.\n\n"
                    "Пожалуйста, напиши так (латиницей, как в mention):\n"
                    "- Circle Lead — @korabovtsev\n"
                    "- Data Lead — @pavelpetrin"
                )

        if not pairs:
            return None

        # Merge defaults (context/roles.json) + runtime (tasks/roles.json)
        base = self.memory.read_json("context/roles.json") or {}
        state = self.memory.read_json("tasks/roles.json") or {}

        def _roles_dict(d):
            if isinstance(d, dict) and isinstance(d.get("roles"), dict):
                return d.get("roles")
            return {}

        merged = {"roles": {}}
        roles = merged["roles"]

        for src in (_roles_dict(base), _roles_dict(state)):
            for rk, users in (src or {}).items():
                if not isinstance(rk, str) or not isinstance(users, list):
                    continue
                cur = roles.get(rk) or []
                cur_l = [str(x).lower() for x in cur if isinstance(x, str)]
                for u in users:
                    if isinstance(u, str) and u.lower() not in cur_l:
                        cur_l.append(u.lower())
                roles[rk] = cur_l

        updated: list[tuple[str, str]] = []
        for role, user in pairs:
            cur_l = roles.get(role) or []
            cur_l = [str(x).lower() for x in cur_l if isinstance(x, str)]
            if user.lower() not in cur_l:
                cur_l.append(user.lower())
            roles[role] = cur_l
            updated.append((role, user))

        self.memory.write_json("tasks/roles.json", merged)

        lines_out = ["✅ Роли обновлены (tasks/roles.json):", ""]
        for role, user in updated:
            lines_out.append(f"- {role}: @{user}")
        lines_out.append("\nТеперь можно повторить: `зафиксируй контракт <id>`." )
        return "\n".join(lines_out)

    def process_message(
        self,
        username: str,
        message: str,
        channel_type: str,
        thread_context: str | None,
        post_id: str | None = None,
        root_id: str | None = None,
    ) -> ProcessResult:
        """Process an incoming message and return ProcessResult."""
        # 0. Role assignment messages (fast-path, no LLM)
        try:
            fast = self._handle_role_assignments_inline(message)
            if fast:
                return ProcessResult(reply=fast)
        except Exception:
            # best-effort; fall back to normal flow
            pass

        # 1. Route
        route_data = route(self.llm, self.memory, username, message, channel_type, thread_context)
        # keep channel type for side-effect policy
        route_data["channel_type"] = channel_type

        # 2. Active thread lookup for top-level messages in channel
        entity = (route_data.get("entity") or "").strip().lower()
        resolved_thread_root: str | None = None

        if not root_id and entity and channel_type == "channel":
            try:
                existing_root = self.memory.get_active_thread(entity)
                if existing_root:
                    # Load thread context from the existing thread
                    thread_posts = self.mm.get_thread(existing_root)
                    thread_context = self._build_thread_context(thread_posts, exclude_post_id=post_id)
                    resolved_thread_root = existing_root
            except Exception as e:
                logger.warning("Failed to load active thread for %s: %s", entity, e)

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

        # Helper to wrap reply and register thread before returning
        def _result(reply: str) -> ProcessResult:
            # Register active thread for discussion-related types
            if entity and route_data.get("type") in THREAD_TRACKING_TYPES:
                try:
                    # If user wrote in a thread (root_id set), track that thread.
                    # If we resolved an existing thread, track that.
                    # Otherwise, use post_id — same value Listener uses as thread root.
                    track_root = root_id or resolved_thread_root or post_id
                    if track_root:
                        self.memory.set_active_thread(entity, track_root)
                except Exception as e:
                    logger.warning("Failed to register active thread for %s: %s", entity, e)
            return ProcessResult(reply=reply, thread_root_id=resolved_thread_root)

        # Fast-path: contract history/version rendering without LLM
        if route_data.get("type") == "contract_history":
            cid = route_data.get("entity")
            items = self.memory.get_contract_history(cid) if cid else []
            if not items:
                return _result(f"История версий для контракта `{cid}` не найдена. (Нет history.jsonl)")
            # newest last in our history.jsonl; show tail
            tail = items[-10:]
            lines = [f"История версий `{cid}` (последние {len(tail)}):", ""]
            for it in tail:
                sha = (it.get("sha256") or "")[:12]
                lines.append(f"- `{it.get('ts')}` — {it.get('kind')} — sha {sha} — {it.get('bytes')} bytes")
            lines.append("\nЧтобы посмотреть конкретную версию: `покажи версию <contract_id> <ts>`")
            return _result("\n".join(lines))

        if route_data.get("type") == "contract_version":
            ent = route_data.get("entity") or ""
            if ":" not in ent:
                return _result("Неверный формат. Используй: `покажи версию <contract_id> <ts>`")
            cid, ts = ent.split(":", 1)
            md = self.memory.get_contract_version(cid, ts)
            if not md:
                return _result(f"Версия не найдена: `{cid}` `{ts}`")
            return _result(f"Версия `{cid}` `{ts}`:\n\n```markdown\n{md}\n```")

        if route_data.get("type") == "show_contract":
            cid = (route_data.get("entity") or "").strip().lower()
            md = self.memory.read_file(f"contracts/{cid}.md")
            if not md:
                return _result(f"Контракт `{cid}` не найден на диске (contracts/{cid}.md).")
            return _result(f"📋 Контракт `{cid}`:\n\n```markdown\n{md}\n```")

        if route_data.get("type") == "show_draft":
            cid = (route_data.get("entity") or "").strip().lower()
            md = self.memory.read_file(f"drafts/{cid}.md")
            if not md:
                return _result(f"Черновик `{cid}` не найден на диске (drafts/{cid}.md).")
            return _result(f"📝 Черновик `{cid}`:\n\n```markdown\n{md}\n```")

        if route_data.get("type") == "contract_diff":
            cid = (route_data.get("entity") or "").strip().lower()
            executor = ToolExecutor(self.memory, self.mm, self.llm)
            result = executor.execute("diff_contract", {"contract_id": cid})
            if "error" in result:
                return _result(result["error"])
            diff_text = result.get("diff", "")
            prev_ts = result.get("prev_ts", "?")
            cur_ts = result.get("current_ts", "?")
            return _result(f"📊 Diff `{cid}` ({prev_ts} → {cur_ts}):\n\n```diff\n{diff_text}\n```")

        if route_data.get("type") == "conflicts_audit":
            analyzer = MetricsAnalyzer(self.memory)
            conflicts = analyzer.detect_conflicts()
            return _result(render_conflicts(conflicts))

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
                return _result(f"Связей для `{cid}` не найдено.")

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

            return _result("\n".join(lines))

        if route_data.get("type") == "governance_review_audit":
            items = find_contracts_requiring_review(self.memory.list_contracts())
            return _result(render_review_report(items))

        if route_data.get("type") == "governance_policy_show":
            tier_key = (route_data.get("entity") or "").strip().lower()
            gov = self.memory.read_json("context/governance.json") or {}
            tiers = gov.get("tiers") if isinstance(gov, dict) else None
            if not isinstance(tiers, dict) or tier_key not in tiers:
                return _result(f"Политика `{tier_key}` не найдена.")
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
            return _result("\n".join(lines))

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
                return _result(f"Не нашёл политику для `{cid}` (tier={tier_key}).")

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
            return _result("\n".join(lines))

        if route_data.get("type") == "lifecycle_get_status":
            cid = (route_data.get("entity") or "").strip().lower()
            status = None
            for c in (self.memory.list_contracts() or []):
                if isinstance(c, dict) and str(c.get("id") or "").lower() == cid:
                    status = c.get("status")
                    break
            if not status:
                return _result(f"Статус для `{cid}` не найден.")
            return _result(f"Статус `{cid}`: **{status}**")

        if route_data.get("type") == "lifecycle_set_status":
            ent = (route_data.get("entity") or "")
            if ":" not in ent:
                return _result("Неверный формат. Используй: `поставь статус <id> <draft|in_review|agreed|approved|active|deprecated|archived>`")
            cid, st = ent.split(":", 1)
            index = self.memory.read_json("contracts/index.json") or {"contracts": []}
            res = set_status(index, cid, st)
            if not res.ok:
                return _result(f"Не получилось: {res.message}")
            self.memory.write_json("contracts/index.json", index)
            return _result(f"✅ {cid}: статус теперь **{st}**")

        if route_data.get("type") == "roles_assign":
            ent = (route_data.get("entity") or "")
            pairs = []
            for part in ent.split(","):
                part = part.strip()
                if not part or ":" not in part:
                    continue
                role, user = part.split(":", 1)
                role = role.strip().lower()
                user_raw = user.strip().lstrip("@")
                # Resolve display name fragments to canonical username when possible.
                user_resolved = None
                try:
                    if hasattr(self.mm, "resolve_username"):
                        user_resolved = self.mm.resolve_username(user_raw)
                except Exception:
                    user_resolved = None
                user = (user_resolved or user_raw).strip().lower()
                if role and user:
                    pairs.append((role, user))

            if not pairs:
                return _result("Не понял назначения ролей. Формат: `Data Lead — @username` / `Circle Lead — @username`.")

            # Read runtime roles state from tasks/roles.json (writable). Fallback to context/roles.json defaults.
            idx = self.memory.read_json("tasks/roles.json")
            if idx is None:
                idx = self.memory.read_json("context/roles.json")
            if not isinstance(idx, dict):
                idx = {"roles": {}}
            roles = idx.get("roles")
            if not isinstance(roles, dict):
                roles = {}
                idx["roles"] = roles

            updated = []
            for role, user in pairs:
                users = roles.get(role)
                if not isinstance(users, list):
                    users = []
                # de-dup, lower-case
                users_l = [str(x).lower() for x in users if isinstance(x, str)]
                if user.lower() not in users_l:
                    users_l.append(user.lower())
                roles[role] = users_l
                updated.append((role, user))

            # Persist ONLY to tasks/roles.json (runtime writable state)
            self.memory.write_json("tasks/roles.json", idx)

            lines = ["✅ Роли обновлены (tasks/roles.json):", ""]
            for role, user in updated:
                lines.append(f"- {role}: @{user}")
            lines.append("\nТеперь можно повторить: `зафиксируй контракт <id>`.")
            return _result("\n".join(lines))

        # ── Tool-use path ────────────────────────────────────────────────
        reply = self._process_with_tools(username, message, channel_type, thread_context, route_data)
        return _result(reply)

    def _process_with_tools(
        self,
        username: str,
        message: str,
        channel_type: str,
        thread_context: str | None,
        route_data: dict,
    ) -> str:
        """Process message using tool-use / function-calling path."""
        # Route-specific system prompt: full prompt only for heavy contract operations
        _FULL_PROMPT_TYPES = {"contract_discussion", "new_contract_init", "problem_report"}

        if route_data.get("type") in _FULL_PROMPT_TYPES:
            system_prompt = self.memory.read_file("prompts/system_full.md") or ""
        else:
            system_prompt = self.memory.read_file("prompts/system_short.md") or ""

        # Load context files
        load_files = route_data.get("load_files", [])
        context_files = self.memory.load_files(load_files) if load_files else ""

        # Load participant profile only for routes that need it
        _PROFILE_ROUTES = {"contract_discussion", "new_contract_init", "problem_report", "profile_intro"}

        if route_data.get("type") in _PROFILE_ROUTES:
            participant_profile = self.memory.get_participant(username) or ""
        else:
            participant_profile = ""
        if participant_profile:
            context_files += f"\n\n--- participants/{username}.md ---\n{participant_profile}"

        full_system = system_prompt

        # Entity anchoring: tell LLM which contract it's working on
        entity = route_data.get("entity")
        route_type = route_data.get("type", "")
        if entity:
            full_system += f"\n\n# Текущий контракт\n\nТы сейчас работаешь над контрактом: `{entity}`\n"
            full_system += f"Тип задачи: {route_type}\n"
            full_system += "НЕ переключайся на другие контракты, если пользователь не попросил об этом явно.\n"

        if context_files:
            full_system += "\n\n# Загруженный контекст\n\n" + context_files

        # Build user message
        user_msg = f"@{username}: {message}"
        if thread_context:
            user_msg = f"Контекст треда:\n{thread_context}\n\nНовое сообщение:\n{user_msg}"

        # Determine available tools based on route type
        tools = get_tools_for_route(route_data.get("type", ""), channel_type != "dm")

        executor = ToolExecutor(self.memory, self.mm, self.llm)

        return self.llm.call_with_tools(
            system_prompt=full_system,
            user_message=user_msg,
            tools=tools,
            tool_executor=executor.execute,
        )

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

