"""ToolExecutor: executes tool calls from the LLM agentic loop.

Each handler method returns a JSON-serializable dict.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timezone

from src.validator import validate_contract
from src.glossary import check_ambiguity
from src.governance import ApprovalPolicy, ApprovalState, ApprovalVote, check_approval_policy
from src.lifecycle import set_status
from src.metrics_tree import (
    mark_contract_agreed, parse_tree, find_node_by_id, get_path_to_root,
    parse_linkage_path, ensure_path_in_tree,
)
from src.relationships import detect_mentions, upsert_relationships
from src.relationships_llm import (
    build_prompt as build_relationships_prompt,
    parse_and_validate as parse_relationships_llm,
)
from src.contract_summary import generate_summary
from src.config import BOT_USERNAME, BOT_DISPLAY_NAME

logger = logging.getLogger(__name__)

# Contract IDs that should never be created (bot identifiers, common words)
_RESERVED_CONTRACT_IDS = frozenset()


def _is_invalid_contract_id(contract_id: str) -> str | None:
    """Return error message if contract_id is invalid, None if OK."""
    if not contract_id or not contract_id.strip():
        return "contract_id пустой"
    cid = contract_id.strip().lower()
    # Must be ASCII snake_case
    if not re.match(r"^[a-z][a-z0-9_]{1,59}$", cid):
        return f"contract_id '{contract_id}' невалидный — допустимы только латиница, цифры, подчёркивания (2-60 символов)"
    # Must not match bot login/username/display name
    bot_names = {n.lower() for n in [BOT_USERNAME, BOT_DISPLAY_NAME] if n}
    # Also block the MATTERMOST_LOGIN value
    import os
    login = os.environ.get("MATTERMOST_LOGIN", "").lower()
    if login:
        bot_names.add(login)
    if cid in bot_names:
        return f"contract_id '{contract_id}' совпадает с именем бота — создание запрещено"
    return None


def _extract_section(markdown: str, section_name: str) -> str:
    """Extract content of a markdown ## section by name."""
    lines = (markdown or "").splitlines()
    in_section = False
    result: list[str] = []
    for line in lines:
        if line.strip().startswith("## ") and section_name.lower() in line.lower():
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("## "):
                break
            result.append(line)
    return "\n".join(result).strip()


def _extract_contract_name(markdown: str) -> str | None:
    for line in (markdown or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("# data contract:"):
            return line.split(":", 1)[1].strip() or None
        if line.startswith("#") and "Data Contract" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip() or None
    return None


def _merge_roles(memory) -> dict:
    """Merge read-only defaults (context/roles.json) with runtime state (tasks/roles.json)."""
    roles: dict[str, list[str]] = {}
    for path in ("context/roles.json", "tasks/roles.json"):
        src = memory.read_json(path) or {}
        if isinstance(src, dict) and isinstance(src.get("roles"), dict):
            for rk, users in src["roles"].items():
                if not isinstance(rk, str) or not isinstance(users, list):
                    continue
                cur = roles.get(rk, [])
                cur_lower = [x.lower() for x in cur]
                for u in users:
                    if isinstance(u, str) and u.lower() not in cur_lower:
                        cur.append(u.lower())
                        cur_lower.append(u.lower())
                roles[rk] = cur
    return roles


class ToolExecutor:
    """Dispatches tool calls to handler methods.

    Args:
        memory: Memory instance for file I/O
        mattermost_client: MattermostClient for user resolution (optional)
        llm_client: LLMClient for relationship detection (optional)
    """

    def __init__(self, memory, mattermost_client=None, llm_client=None, thread_root_id: str | None = None):
        self.memory = memory
        self.mm = mattermost_client
        self.llm = llm_client
        self.thread_root_id = thread_root_id

    def execute(self, tool_name: str, args: dict) -> dict:
        """Dispatch tool call to handler. Returns JSON-serializable result."""
        handler = getattr(self, f"_tool_{tool_name}", None)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return handler(**args)
        except Exception as e:
            logger.exception("Tool %s failed: %s", tool_name, e)
            return {"error": f"Tool {tool_name} failed: {e}"}

    # ── Read-only tools ──────────────────────────────────────────────────

    def _tool_read_contract(self, contract_id: str) -> dict:
        md = self.memory.get_contract(contract_id)
        if md is None:
            return {"error": f"Контракт {contract_id} не найден (contracts/{contract_id}.md)"}
        return {"contract_id": contract_id, "content": md}

    def _tool_read_draft(self, contract_id: str) -> dict:
        md = self.memory.get_draft(contract_id)
        if md is None:
            return {"error": f"Черновик {contract_id} не найден (drafts/{contract_id}.md)"}
        return {"contract_id": contract_id, "content": md}

    def _tool_read_discussion(self, contract_id: str) -> dict:
        data = self.memory.get_discussion(contract_id)
        if data is None:
            return {"error": f"Обсуждение {contract_id} не найдено"}
        return {"contract_id": contract_id, "discussion": data}

    def _tool_read_governance_policy(self, tier: str) -> dict:
        gov = self.memory.read_json("context/governance.json") or {}
        tiers = gov.get("tiers") if isinstance(gov, dict) else None
        if not isinstance(tiers, dict) or tier not in tiers:
            return {"error": f"Политика {tier} не найдена"}
        cfg = tiers[tier]
        req = cfg.get("approval_required", [])
        thr = cfg.get("consensus_threshold")
        desc = cfg.get("description", "")

        roles = _merge_roles(self.memory)
        role_assignments = {}
        for role in req:
            role_assignments[role] = roles.get(role, [])

        return {
            "tier": tier,
            "description": desc,
            "approval_required": req,
            "consensus_threshold": thr,
            "current_assignments": role_assignments,
        }

    def _tool_read_roles(self) -> dict:
        roles = _merge_roles(self.memory)
        return {"roles": roles}

    def _tool_validate_contract(self, contract_md: str) -> dict:
        report = validate_contract(contract_md)
        issues = []
        warnings = []
        for i in report.issues:
            entry = {"code": i.code, "message": i.message}
            if i.code == "missing_optional_section" or i.code.startswith("formula_missing"):
                warnings.append(entry)
            else:
                issues.append(entry)
        return {"ok": report.ok, "issues": issues, "warnings": warnings}

    def _tool_check_approval(self, contract_id: str, contract_md: str) -> dict:
        gov = self.memory.read_json("context/governance.json") or {}
        tiers = (gov.get("tiers") or {}) if isinstance(gov, dict) else {}
        tier_key = "tier_2"  # default

        for c in (self.memory.list_contracts() or []):
            if isinstance(c, dict) and str(c.get("id") or "").lower() == contract_id.lower() and c.get("tier"):
                tier_key = str(c["tier"])
                break

        tier_cfg = tiers.get(tier_key) if isinstance(tiers, dict) else None
        if not isinstance(tier_cfg, dict):
            return {"ok": True, "missing_roles": [], "glossary_issues": [], "note": f"Tier {tier_key} не найден, пропускаю governance"}

        policy = ApprovalPolicy(
            tier=tier_key,
            approval_required=list(tier_cfg.get("approval_required") or []),
            consensus_threshold=float(tier_cfg.get("consensus_threshold") or 1.0),
        )

        roles = _merge_roles(self.memory)
        role_map = {}
        for role, users in roles.items():
            for u in users:
                role_map[u.lower()] = role

        check = check_approval_policy(contract_md=contract_md, policy=policy, role_map=role_map)

        glossary = self.memory.read_json("context/glossary.json")
        glossary_issues = []
        try:
            gi = check_ambiguity(contract_md, glossary)
            glossary_issues = [{"canonical": g.canonical, "message": g.message} for g in gi]
        except Exception:
            pass

        return {
            "ok": check.ok and len(glossary_issues) == 0,
            "tier": tier_key,
            "missing_roles": list(check.missing_roles),
            "glossary_issues": glossary_issues,
        }

    def _tool_diff_contract(self, contract_id: str) -> dict:
        """Show diff between current and previous version of a contract."""
        history = self.memory.get_contract_history(contract_id)
        if not history:
            return {"error": f"История версий для {contract_id} не найдена."}

        # Find the last two "current" entries, or last "previous" + last "current"
        current_entries = [h for h in history if h.get("kind") == "current"]
        prev_entries = [h for h in history if h.get("kind") == "previous"]

        if not current_entries:
            return {"error": f"Нет текущих версий для {contract_id}."}

        latest_ts = current_entries[-1].get("ts")
        latest_md = self.memory.get_contract_version(contract_id, latest_ts) if latest_ts else None

        # Best previous: use the last "previous" entry, or the second-to-last "current"
        prev_md = None
        prev_ts = None
        if prev_entries:
            prev_ts = prev_entries[-1].get("ts")
            prev_md = self.memory.get_contract_version(contract_id, prev_ts) if prev_ts else None
        elif len(current_entries) >= 2:
            prev_ts = current_entries[-2].get("ts")
            prev_md = self.memory.get_contract_version(contract_id, prev_ts) if prev_ts else None

        if not prev_md:
            return {"error": f"Только одна версия контракта {contract_id}, сравнивать не с чем."}

        # Generate unified diff
        diff_lines = list(difflib.unified_diff(
            (prev_md or "").splitlines(keepends=True),
            (latest_md or "").splitlines(keepends=True),
            fromfile=f"{contract_id} ({prev_ts})",
            tofile=f"{contract_id} ({latest_ts})",
            lineterm="",
        ))

        if not diff_lines:
            return {"contract_id": contract_id, "diff": "(нет изменений)", "prev_ts": prev_ts, "current_ts": latest_ts}

        return {
            "contract_id": contract_id,
            "diff": "\n".join(diff_lines),
            "prev_ts": prev_ts,
            "current_ts": latest_ts,
        }

    def _tool_generate_contract_template(self, contract_id: str) -> dict:
        """Generate a pre-filled contract template from metrics tree + circles + governance."""
        tree_md = self.memory.read_file("context/metrics_tree.md") or ""
        root = parse_tree(tree_md)

        metric_name = contract_id
        tree_path = ""
        if root:
            node = find_node_by_id(root, contract_id)
            if node:
                metric_name = node.short_name or node.name
                tree_path = get_path_to_root(node)

        # Resolve stakeholders
        stakeholders = []
        try:
            from src.suggestion_engine import _resolve_stakeholders
            circles_md = self.memory.read_file("context/circles.md") or ""
            stakeholders = _resolve_stakeholders(metric_name, circles_md)
        except Exception:
            pass

        # Determine tier
        tier_key = "tier_2"
        for c in (self.memory.list_contracts() or []):
            if isinstance(c, dict) and str(c.get("id") or "").lower() == contract_id.lower() and c.get("tier"):
                tier_key = str(c["tier"])
                break

        # Governance info
        gov = self.memory.read_json("context/governance.json") or {}
        tiers = (gov.get("tiers") or {}) if isinstance(gov, dict) else {}
        tier_cfg = tiers.get(tier_key, {}) if isinstance(tiers, dict) else {}
        required_roles = tier_cfg.get("approval_required", []) if isinstance(tier_cfg, dict) else []

        stakeholders_str = ", ".join([f"@{s}" for s in stakeholders]) if stakeholders else "(определить)"
        roles_str = ", ".join(required_roles) if required_roles else "(определить)"

        template = f"""# Data Contract: {metric_name}

## Определение
(Описание метрики — что она измеряет, в каких единицах.)

## Формула
(Точная формула расчёта.)

## Источник данных
(Система, таблица, поле.)

## Связь с Extra Time
{tree_path or f"{metric_name} → ... → Extra Time"}

## Гранулярность
- Временная: месяц
- Организационная: (компания / отдел / клиент)

## Ответственные
- Владелец метрики: {stakeholders_str}
- Согласование ({tier_key}): {roles_str}

## Связанные контракты
(Перечислить ID связанных контрактов.)

## Согласовано
(Здесь будут подписи согласующих.)
"""
        return {
            "contract_id": contract_id,
            "metric_name": metric_name,
            "tree_path": tree_path,
            "tier": tier_key,
            "stakeholders": stakeholders,
            "template": template,
        }

    def _tool_participant_stats(self, username: str = "") -> dict:
        """Compute participant analytics from audit log and discussions."""
        audit = self.memory.read_jsonl("memory/audit.jsonl")
        contracts = self.memory.list_contracts() or []

        # Build per-user stats
        stats: dict[str, dict] = {}

        for entry in audit:
            action = entry.get("action", "")
            user = entry.get("username", "")
            if not user:
                continue
            if username and user.lower() != username.lower():
                continue
            if user not in stats:
                stats[user] = {"approvals": 0, "role_assignments": 0, "status_changes": 0, "contracts_saved": 0}
            if action == "approve_contract":
                stats[user]["approvals"] += 1
            elif action == "assign_role":
                stats[user]["role_assignments"] += 1
            elif action == "set_contract_status":
                stats[user]["status_changes"] += 1
            elif action == "save_contract":
                stats[user]["contracts_saved"] += 1

        # Count contracts per participant from index
        for c in contracts:
            if not isinstance(c, dict):
                continue

        if username:
            return {"username": username, "stats": stats.get(username.lower(), stats.get(username, {}))}

        return {"participants": stats, "total_contracts": len(contracts)}

    def _tool_list_contracts(self) -> dict:
        contracts = self.memory.list_contracts() or []
        items = []
        for c in contracts:
            if isinstance(c, dict):
                items.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "tier": c.get("tier"),
                })
        return {"contracts": items}

    # ── Write tools ──────────────────────────────────────────────────────

    def _tool_save_contract(self, contract_id: str, content: str, force: bool = False) -> dict:
        """Validate + governance + glossary + save. Returns structured result."""
        err = _is_invalid_contract_id(contract_id)
        if err:
            return {"success": False, "contract_id": contract_id, "errors": [err], "warnings": []}
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Validation
        report = validate_contract(content)
        if not report.ok:
            for i in report.issues:
                if i.code == "missing_optional_section" or i.code.startswith("formula_missing"):
                    warnings.append(i.message)
                else:
                    errors.append(f"Валидация: {i.message}")

        # 2. Governance
        try:
            gov = self.memory.read_json("context/governance.json") or {}
            tiers = (gov.get("tiers") or {}) if isinstance(gov, dict) else {}
            tier_key = "tier_2"

            for c in (self.memory.list_contracts() or []):
                if isinstance(c, dict) and str(c.get("id") or "").lower() == contract_id.lower() and c.get("tier"):
                    tier_key = str(c["tier"])
                    break

            tier_cfg = tiers.get(tier_key) if isinstance(tiers, dict) else None
            if isinstance(tier_cfg, dict):
                policy = ApprovalPolicy(
                    tier=tier_key,
                    approval_required=list(tier_cfg.get("approval_required") or []),
                    consensus_threshold=float(tier_cfg.get("consensus_threshold") or 1.0),
                )

                roles = _merge_roles(self.memory)
                role_map = {}
                for role, users in roles.items():
                    for u in users:
                        role_map[u.lower()] = role

                check = check_approval_policy(contract_md=content, policy=policy, role_map=role_map)
                if not check.ok:
                    missing = ", ".join(check.missing_roles) or "(неизвестно)"
                    errors.append(f"Governance ({tier_key}): не хватает ролей: {missing}")
        except Exception as e:
            logger.warning("Governance check failed: %s", e)

        # 3. Glossary (force=True downgrades to warnings)
        try:
            glossary = self.memory.read_json("context/glossary.json")
            glossary_issues = check_ambiguity(content, glossary)
            for gi in glossary_issues:
                if force:
                    warnings.append(f"Глоссарий: {gi.message}")
                else:
                    errors.append(f"Глоссарий: {gi.message}")
        except Exception:
            pass

        # 4. If errors — do NOT save
        if errors:
            return {
                "success": False,
                "contract_id": contract_id,
                "errors": errors,
                "warnings": warnings,
            }

        # 5. Save
        self.memory.save_contract(contract_id, content)
        name = _extract_contract_name(content) or contract_id
        now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self.memory.update_contract_index(contract_id, {
            "name": name,
            "status": "agreed",
            "file": f"contracts/{contract_id}.md",
            "agreed_date": now_date,
            "status_updated_at": now_date,
        })

        # Best-effort: relationships
        try:
            known_contracts = self.memory.list_contracts() or []
            known_ids = [c.get("id") for c in known_contracts if isinstance(c, dict) and c.get("id")]
            rels = detect_mentions(contract_id=contract_id, contract_md=content, known_contract_ids=known_ids)

            if self.llm:
                try:
                    system, user = build_relationships_prompt(
                        contract_id=contract_id, contract_md=content, known_contracts=known_contracts,
                    )
                    raw = self.llm.call_heavy(system, user)
                    parsed = parse_relationships_llm(
                        raw, contract_id=contract_id, known_ids=set(x for x in known_ids if isinstance(x, str)),
                    )
                    rels.extend(parsed)
                except Exception as e:
                    logger.info("Relationships LLM skipped: %s", e)

            if rels:
                idx = self.memory.read_json("contracts/relationships.json") or {"relationships": []}
                normalized = [r for r in rels if hasattr(r, "from_id")]
                idx2, added = upsert_relationships(idx, normalized)
                if added:
                    self.memory.write_json("contracts/relationships.json", idx2)
                    logger.info("Relationships updated: +%d", added)
        except Exception as e:
            logger.warning("Failed to update relationships: %s", e)

        # Best-effort: metrics tree
        try:
            tree_text = self.memory.read_file("context/metrics_tree.md") or ""

            # Grow tree from "Связь с Extra Time" path
            try:
                from src.validator import _extract_sections
                sections = _extract_sections(content)
                linkage = sections.get("Связь с Extra Time", "")
                if linkage:
                    path_parts = parse_linkage_path(linkage)
                    if len(path_parts) >= 2:
                        grow = ensure_path_in_tree(tree_text, path_parts)
                        if grow.ok and grow.changed:
                            self.memory.write_file("context/metrics_tree.md", grow.new_text)
                            tree_text = grow.new_text
            except Exception as e:
                logger.warning("Tree growth failed: %s", e)

            patch = mark_contract_agreed(tree_text, name)
            if not patch.ok:
                patch = mark_contract_agreed(tree_text, contract_id)
            if patch.ok and patch.changed:
                self.memory.write_file("context/metrics_tree.md", patch.new_text)
        except Exception:
            pass

        # Best-effort: suggest next contract (proactive)
        try:
            from src.suggestion_engine import SuggestionEngine
            engine = SuggestionEngine(self.memory, self.llm)
            if engine.can_suggest_today():
                candidates = engine.suggest_after_agreement(contract_id)
                candidates = engine.filter_already_suggested(candidates)
                if candidates:
                    msg = engine.format_suggestion_message(candidates[:2], f"agreed:{contract_id}")
                    if self.mm and msg:
                        resp = self.mm.send_to_channel(msg)
                        engine.record_suggestion(candidates[:2], f"agreed:{contract_id}", resp.get("id"))
        except Exception as e:
            logger.warning("Post-agreement suggestion failed: %s", e)

        self.memory.audit_log("save_contract", contract_id=contract_id, name=name)

        try:
            summary = generate_summary(contract_id, content, "agreed")
            self.memory.update_summary(contract_id, summary)
        except Exception as e:
            logger.warning("Failed to update summary for %s: %s", contract_id, e)

        logger.info("Saved contract: %s", contract_id)
        return {
            "success": True,
            "contract_id": contract_id,
            "warnings": warnings,
        }

    def _tool_save_draft(self, contract_id: str, content: str) -> dict:
        err = _is_invalid_contract_id(contract_id)
        if err:
            return {"success": False, "error": err}
        self.memory.save_draft(contract_id, content)
        name = _extract_contract_name(content) or contract_id
        self.memory.update_contract_index(contract_id, {
            "name": name,
            "status": "draft",
            "file": f"drafts/{contract_id}.md",
        })
        try:
            summary = generate_summary(contract_id, content, "draft")
            self.memory.update_summary(contract_id, summary)
        except Exception as e:
            logger.warning("Failed to update draft summary for %s: %s", contract_id, e)

        logger.info("Saved draft: %s", contract_id)
        result: dict = {"success": True, "contract_id": contract_id, "name": name}

        # Auto-check data availability when "Источник данных" section is filled
        data_source = _extract_section(content, "Источник данных")
        if data_source and data_source.strip() not in ("", "TBD", "—", "-", "нет"):
            try:
                avail = self._tool_check_data_availability(contract_id, data_source)
                if avail.get("error") is None:
                    result["data_availability"] = avail
            except Exception as e:
                logger.warning("Auto MCP check failed for %s: %s", contract_id, e)

        return result

    def _tool_update_discussion(self, contract_id: str, discussion: dict) -> dict:
        if not isinstance(discussion, dict):
            return {"error": "discussion must be a JSON object"}
        self.memory.update_discussion(contract_id, discussion)
        logger.info("Updated discussion: %s", contract_id)
        return {"success": True, "contract_id": contract_id}

    def _tool_add_reminder(self, reminder: dict) -> dict:
        if not isinstance(reminder, dict):
            return {"error": "reminder must be a JSON object"}
        reminders = self.memory.get_reminders()
        reminders.append(reminder)
        self.memory.save_reminders(reminders)
        logger.info("Added reminder for %s", reminder.get("contract_id"))
        return {"success": True, "reminder_id": reminder.get("id")}

    def _tool_update_participant(self, username: str, content: str) -> dict:
        self.memory.update_participant(username, content)
        logger.info("Updated participant: %s", username)
        return {"success": True, "username": username}

    def _tool_save_decision(self, decision: dict) -> dict:
        if not isinstance(decision, dict):
            return {"error": "decision must be a JSON object"}
        self.memory.save_decision(decision)
        logger.info("Saved decision for %s", decision.get("contract"))
        return {"success": True}

    def _tool_assign_role(self, role: str, username: str) -> dict:
        username = username.strip().lstrip("@").lower()
        role = role.strip().lower()

        if not role or not username:
            return {"error": "role and username are required"}

        # Resolve display name if possible
        resolved = None
        try:
            if self.mm and hasattr(self.mm, "resolve_username"):
                resolved = self.mm.resolve_username(username)
        except Exception:
            pass
        if resolved:
            username = str(resolved).lower()

        idx = self.memory.read_json("tasks/roles.json")
        if not isinstance(idx, dict):
            idx = {"roles": {}}
        roles = idx.get("roles")
        if not isinstance(roles, dict):
            roles = {}
            idx["roles"] = roles

        users = roles.get(role, [])
        if not isinstance(users, list):
            users = []
        users_lower = [str(x).lower() for x in users if isinstance(x, str)]
        if username not in users_lower:
            users_lower.append(username)
        roles[role] = users_lower

        self.memory.write_json("tasks/roles.json", idx)
        self.memory.audit_log("assign_role", role=role, username=username)
        logger.info("Assigned role %s to %s", role, username)
        return {"success": True, "role": role, "username": username}

    def _tool_ask_question(self, question: str, options: list, target_roles: list = None) -> dict:
        if not self.mm:
            return {"error": "Mattermost client not available"}
        if not isinstance(options, list) or len(options) < 2:
            return {"error": "options must be a list with at least 2 items"}

        mentions = self._resolve_mentions(target_roles)

        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        lines = [f":question: **{question}**\n"]
        for i, opt in enumerate(options[:5]):
            lines.append(f"{emojis[i]} {opt}")
        lines.append("")
        if mentions:
            lines.append(f"{mentions} — ваше мнение важно")

        self.mm.send_to_channel("\n".join(lines), root_id=self.thread_root_id)
        return {"success": True, "mentions": mentions}

    def _resolve_mentions(self, target_roles: list = None) -> str:
        """Resolve circle names to @mentions of leads."""
        circles_md = self.memory.read_file("context/circles.md") or ""
        from src.suggestion_engine import _parse_circles
        circle_leads = _parse_circles(circles_md)

        if target_roles:
            users: list[str] = []
            for role in target_roles:
                for circle_name, username in circle_leads.items():
                    if role.lower() in circle_name.lower() and username not in users:
                        users.append(username)
            if users:
                return " ".join(f"@{u}" for u in users)

        # Fallback: active participants (max 10)
        active = self.memory.list_participants(active_only=True)
        if active:
            return " ".join(f"@{u}" for u in active[:10])
        return ""

    def _tool_request_approval(self, contract_id: str) -> dict:
        """Start approval workflow: determine required roles, notify, track state."""
        content = self.memory.get_draft(contract_id) or self.memory.get_contract(contract_id)
        if not content:
            return {"error": f"Контракт или черновик {contract_id} не найден"}

        # Determine tier
        gov = self.memory.read_json("context/governance.json") or {}
        tiers = (gov.get("tiers") or {}) if isinstance(gov, dict) else {}
        tier_key = "tier_2"
        for c in (self.memory.list_contracts() or []):
            if isinstance(c, dict) and str(c.get("id") or "").lower() == contract_id.lower() and c.get("tier"):
                tier_key = str(c["tier"])
                break

        tier_cfg = tiers.get(tier_key) if isinstance(tiers, dict) else None
        if not isinstance(tier_cfg, dict):
            return {"error": f"Политика {tier_key} не найдена в governance.json"}

        required_roles = list(tier_cfg.get("approval_required") or [])
        threshold = float(tier_cfg.get("consensus_threshold") or 1.0)

        # Build role → users map
        roles = _merge_roles(self.memory)
        role_users = {}
        for role in required_roles:
            role_users[role] = roles.get(role, [])

        # Load or create approval state
        discussion = self.memory.get_discussion(contract_id) or {}
        existing_state = discussion.get("approval_state")

        now = datetime.now(timezone.utc).isoformat()
        state = ApprovalState(
            tier=tier_key,
            required_roles=required_roles,
            threshold=threshold,
            requested_at=now,
            approvals=ApprovalState.from_dict(existing_state).approvals if existing_state else [],
        )

        # Save state
        discussion["approval_state"] = state.to_dict()
        self.memory.update_discussion(contract_id, discussion)

        # Notify via Mattermost
        mentions = []
        for role, users in role_users.items():
            for u in users:
                mentions.append(f"@{u} ({role})")

        if self.mm and mentions:
            msg = (
                f"📋 **Запрос на согласование контракта `{contract_id}`**\n\n"
                f"Tier: {tier_key} (порог: {threshold * 100:.0f}%)\n"
                f"Требуются: {', '.join(mentions)}\n\n"
                f"Для согласования напишите: `согласую контракт {contract_id}`"
            )
            self.mm.send_to_channel(msg)

        return {
            "success": True,
            "contract_id": contract_id,
            "tier": tier_key,
            "threshold": threshold,
            "required_roles": required_roles,
            "role_users": role_users,
            "existing_approvals": [a.username for a in state.approvals],
            "quorum_met": state.is_quorum_met(),
        }

    def _tool_approve_contract(self, contract_id: str, username: str) -> dict:
        """Record an approval vote. Returns quorum status."""
        username = username.strip().lstrip("@").lower()
        if not username:
            return {"error": "username is required"}

        # Load approval state
        discussion = self.memory.get_discussion(contract_id) or {}
        state_data = discussion.get("approval_state")
        if not state_data:
            return {"error": f"Согласование для {contract_id} не запущено. Используй request_approval."}

        state = ApprovalState.from_dict(state_data)

        # Check user's role
        roles = _merge_roles(self.memory)
        role_map = {}
        for role, users in roles.items():
            for u in users:
                role_map[u.lower()] = role

        user_role = role_map.get(username)
        if not user_role or user_role not in state.required_roles:
            return {
                "error": f"@{username} не имеет необходимой роли для согласования. "
                         f"Требуются: {', '.join(state.required_roles)}",
                "username": username,
                "user_role": user_role,
            }

        # Check if already approved
        if any(a.username == username for a in state.approvals):
            return {
                "success": True,
                "already_approved": True,
                "contract_id": contract_id,
                "quorum_met": state.is_quorum_met(),
                "missing_roles": state.missing_roles(),
            }

        # Record approval
        now = datetime.now(timezone.utc).isoformat()
        state.approvals.append(ApprovalVote(
            username=username,
            role=user_role,
            approved_at=now,
        ))

        # Save
        discussion["approval_state"] = state.to_dict()
        self.memory.update_discussion(contract_id, discussion)

        self.memory.audit_log("approve_contract", contract_id=contract_id, username=username, role=user_role)

        quorum = state.is_quorum_met()
        missing = state.missing_roles()

        result = {
            "success": True,
            "contract_id": contract_id,
            "approved_by": username,
            "role": user_role,
            "quorum_met": quorum,
            "missing_roles": missing,
            "total_approvals": len(state.approvals),
        }

        if quorum:
            result["message"] = "Кворум достигнут! Контракт можно финализировать через save_contract."
        else:
            result["message"] = f"Осталось получить согласование: {', '.join(missing)}"

        return result

    # ── Data query tools (MCP) ───────────────────────────────────────────

    def _tool_explore_schema(self) -> dict:
        """List tables and columns from ai_bi schema via MCP."""
        from src.mcp_client import MCPClient, MCPError
        try:
            client = MCPClient()
            try:
                client.initialize()
                objects = client.list_objects(schema="ai_bi")
                # objects is a list — each item may be a table name string or dict
                tables = []
                for obj in objects:
                    if isinstance(obj, str):
                        table_name = obj
                        description = ""
                        columns = []
                    elif isinstance(obj, dict):
                        table_name = (obj.get("table") or obj.get("name")
                                      or obj.get("object_name") or "")
                        description = obj.get("description") or obj.get("comment") or ""
                        columns = obj.get("columns") or []
                    else:
                        continue
                    if not table_name:
                        continue
                    # If no columns yet, fetch details
                    if not columns and table_name:
                        try:
                            details = client.get_object_details("ai_bi", table_name)
                            columns = details.get("columns") or []
                            if not description:
                                description = details.get("description") or details.get("comment") or ""
                        except Exception:
                            pass
                    tables.append({
                        "table": table_name,
                        "description": description,
                        "columns": columns,
                    })
            finally:
                client.close()
        except MCPError as e:
            return {"error": f"MCP недоступен: {e}"}

        return {"schema": "ai_bi", "tables": tables, "table_count": len(tables)}

    def _tool_query_data(self, sql: str, description: str = "") -> dict:
        """Execute a SELECT query via MCP. Enforces safety rules."""
        sql_upper = sql.upper().strip()

        # Safety checks
        forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "CREATE", "ALTER", "GRANT", "REVOKE"]
        for kw in forbidden:
            if re.search(rf"\b{kw}\b", sql_upper):
                return {"error": f"Запрещённая операция: {kw}. Только SELECT."}

        if "CROSS JOIN" in sql_upper:
            return {"error": "CROSS JOIN запрещён."}

        if "LIMIT" not in sql_upper:
            return {"error": "Запрос должен содержать LIMIT (максимум 500)."}

        # Check all table references are in ai_bi
        # Warn if schema not specified (not a hard block — server enforces it)
        from src.mcp_client import MCPClient, MCPError
        try:
            client = MCPClient()
            try:
                client.initialize()
                rows = client.execute_sql(sql)
            finally:
                client.close()
        except MCPError as e:
            return {"error": f"MCP ошибка: {e}"}

        if not isinstance(rows, list):
            return {"error": "Неожиданный формат ответа от MCP", "raw": str(rows)[:500]}

        columns = list(rows[0].keys()) if rows else []
        logger.info("query_data: %s rows, sql=%s", len(rows), (description or sql)[:80])
        return {
            "rows": rows[:500],
            "columns": columns,
            "row_count": len(rows),
        }

    def _tool_check_data_availability(self, contract_id: str, data_source_description: str) -> dict:
        """Check if tables mentioned in data_source_description exist in ai_bi schema."""
        from src.mcp_client import MCPClient, MCPError

        # Step 1: get schema objects
        try:
            client = MCPClient()
            try:
                client.initialize()
                objects = client.list_objects(schema="ai_bi")
            finally:
                client.close()
        except MCPError as e:
            logger.warning("MCP unavailable: %s", e)
            return {"available": None, "error": f"MCP недоступен: {e}"}

        # Build set of known table names
        known_tables = {
            (obj.get("table") or obj.get("name") or "").lower()
            for obj in objects
            if isinstance(obj, dict)
        }

        # Step 2: use LLM to extract table names from description (if available)
        # Fall back to simple keyword matching
        candidate_tables: list[str] = []
        if self.llm:
            try:
                prompt = (
                    f"Из описания источника данных извлеки имена таблиц или представлений PostgreSQL "
                    f"(только имена, без схемы, по одному на строку).\n\n"
                    f"Описание: {data_source_description}\n\n"
                    f"Известные таблицы в схеме ai_bi: {', '.join(sorted(known_tables)) or 'нет данных'}\n\n"
                    f"Ответь только списком имён таблиц (одно имя на строку). "
                    f"Если таблицы не упомянуты, напиши «нет»."
                )
                resp = self.llm.call_cheap(
                    "Извлеки имена таблиц PostgreSQL из описания источника данных.",
                    prompt,
                )
                for line in resp.splitlines():
                    name = line.strip().strip("-").strip().lower()
                    if name and name != "нет" and len(name) < 80:
                        candidate_tables.append(name)
            except Exception as e:
                logger.warning("LLM extraction for MCP check failed: %s", e)

        # If LLM not available or returned nothing, do simple word matching
        if not candidate_tables:
            words = re.findall(r"\b[a-z_][a-z0-9_]{2,}\b", data_source_description.lower())
            candidate_tables = [w for w in words if w in known_tables]

        found = [t for t in candidate_tables if t in known_tables]
        missing = [t for t in candidate_tables if t not in known_tables]

        available = len(missing) == 0 if candidate_tables else None

        result = {
            "available": available,
            "tables_found": found,
            "tables_missing": missing,
            "total_tables_in_schema": len(known_tables),
        }

        # Auto-suggest similar tables when some are missing
        suggested_tables: list[str] = []
        if missing and known_tables:
            # Extract meaningful keywords from missing table names
            keywords = set()
            for m_tbl in missing:
                keywords.update(re.findall(r"[a-z]{3,}", m_tbl.lower()))
            suggested_tables = sorted([
                t for t in known_tables
                if any(kw in t for kw in keywords)
            ])[:5]

        if not candidate_tables:
            result["message"] = (
                "Не удалось определить конкретные таблицы из описания источника данных. "
                "Проверь вручную или уточни источник."
            )
        elif missing:
            msg = (
                f"Таблицы не найдены в ai_bi: {', '.join(missing)}. "
                f"Возможно, данные ещё не загружены."
            )
            if suggested_tables:
                msg += f" Похожие таблицы в ai_bi: {', '.join(suggested_tables)}."
                result["suggested_tables"] = suggested_tables
            result["message"] = msg
            # Post warning to Mattermost thread if connected
            if self.mm and self.thread_root_id:
                warning = (
                    f"⚠️ Проверка данных для контракта `{contract_id}`:\n"
                    f"Таблицы не найдены в схеме `ai_bi`: `{'`, `'.join(missing)}`\n"
                    f"Данные необходимо подготовить до финализации контракта."
                )
                try:
                    self.mm.send_to_channel(warning, root_id=self.thread_root_id)
                except Exception as e:
                    logger.warning("Failed to post MCP warning: %s", e)
        else:
            result["message"] = f"✅ Все таблицы найдены в ai_bi: {', '.join(found)}."

        logger.info("MCP availability check for %s: found=%s missing=%s", contract_id, found, missing)
        return result

    def _tool_generate_datamart_spec(self, contract_id: str) -> dict:
        """Generate a datamart specification from a contract."""
        # 1. Read contract content
        content = self.memory.read_file(f"contracts/{contract_id}.md")
        if not content:
            content = self.memory.read_file(f"drafts/{contract_id}.md")
        if not content:
            return {"error": f"Контракт {contract_id} не найден"}

        # 2. Extract key sections
        definition = _extract_section(content, "Определение")
        formula = _extract_section(content, "Формула")
        granularity = _extract_section(content, "Гранулярность")
        includes = _extract_section(content, "Включает")
        excludes = _extract_section(content, "Исключения")
        data_source = _extract_section(content, "Источник данных")
        data_owner = _extract_section(content, "Ответственный за данные")
        calc_owner = _extract_section(content, "Ответственный за расчёт")
        related = _extract_section(content, "Связанные контракты")
        name = _extract_contract_name(content) or contract_id

        # 3. Query MCP for existing tables (best-effort)
        tables_info = ""
        try:
            from src.mcp_client import MCPClient
            client = MCPClient()
            try:
                client.initialize()
                objects = client.list_objects(schema="ai_bi")
                if objects:
                    table_names = sorted(set(
                        (obj.get("table") or obj.get("name") or "").lower()
                        for obj in objects if isinstance(obj, dict)
                    ))
                    tables_info = f"Таблицы в ai_bi: {', '.join(table_names)}"
            finally:
                client.close()
        except Exception as e:
            logger.debug("MCP unavailable for datamart spec: %s", e)
            tables_info = "MCP недоступен — таблицы ai_bi не удалось получить."

        # 4. Build LLM prompt
        spec_prompt = self.memory.read_file("prompts/datamart_spec.md") or ""
        if not spec_prompt:
            return {"error": "Шаблон prompts/datamart_spec.md не найден"}

        contract_context = (
            f"Contract ID: {contract_id}\n"
            f"Название: {name}\n\n"
            f"Определение:\n{definition or '(не указано)'}\n\n"
            f"Формула:\n{formula or '(не указана)'}\n\n"
            f"Гранулярность:\n{granularity or '(не указана)'}\n\n"
            f"Включает:\n{includes or '(не указано)'}\n\n"
            f"Исключения:\n{excludes or '(не указано)'}\n\n"
            f"Источник данных:\n{data_source or '(не указан)'}\n\n"
            f"Ответственный за данные: {data_owner or '(не указан)'}\n"
            f"Ответственный за расчёт: {calc_owner or '(не указан)'}\n\n"
            f"Связанные контракты:\n{related or '(нет)'}\n\n"
            f"--- Данные DWH ---\n{tables_info}\n"
        )

        if not self.llm:
            return {"error": "LLM клиент недоступен"}

        spec = self.llm.call_heavy(spec_prompt, contract_context, max_tokens=4000)

        if not spec or not spec.strip():
            return {"error": "LLM вернул пустой результат"}

        # 5. Save spec to file
        self.memory.write_file(f"specs/{contract_id}_datamart.md", spec)

        logger.info("Generated datamart spec for %s", contract_id)
        return {
            "success": True,
            "contract_id": contract_id,
            "spec_file": f"specs/{contract_id}_datamart.md",
            "spec": spec,
        }

    def _tool_set_contract_status(self, contract_id: str, status: str) -> dict:
        index = self.memory.read_json("contracts/index.json") or {"contracts": []}
        result = set_status(index, contract_id, status)
        if not result.ok:
            return {"success": False, "error": result.message}
        self.memory.write_json("contracts/index.json", index)
        self.memory.audit_log("set_contract_status", contract_id=contract_id, status=status)
        return {"success": True, "contract_id": contract_id, "status": status, "message": result.message}


def backfill_summaries(memory) -> int:
    """Generate summaries for all existing contracts. Returns count of summaries created."""
    index = memory.list_contracts() or []
    summaries = {}
    for entry in index:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        if not cid:
            continue
        status = entry.get("status", "draft")
        md = memory.get_contract(cid) or memory.get_draft(cid) or ""
        if not md:
            continue
        summaries[cid] = generate_summary(cid, md, status)
    if summaries:
        memory.save_summaries(summaries)
    return len(summaries)
