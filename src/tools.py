"""ToolExecutor: executes tool calls from the LLM agentic loop.

Each handler method returns a JSON-serializable dict.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from src.validator import validate_contract
from src.glossary import check_ambiguity
from src.governance import ApprovalPolicy, check_approval_policy
from src.lifecycle import set_status
from src.metrics_tree import mark_contract_agreed
from src.relationships import detect_mentions, upsert_relationships
from src.relationships_llm import (
    build_prompt as build_relationships_prompt,
    parse_and_validate as parse_relationships_llm,
)

logger = logging.getLogger(__name__)


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

    def __init__(self, memory, mattermost_client=None, llm_client=None):
        self.memory = memory
        self.mm = mattermost_client
        self.llm = llm_client

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
                    use_poll = len(candidates) > 1
                    msg = engine.format_suggestion_message(candidates[:2], f"agreed:{contract_id}", use_poll=use_poll)
                    if self.mm and msg:
                        resp = self.mm.send_to_channel(msg)
                        engine.record_suggestion(candidates[:2], f"agreed:{contract_id}", resp.get("id"))
        except Exception as e:
            logger.warning("Post-agreement suggestion failed: %s", e)

        logger.info("Saved contract: %s", contract_id)
        return {
            "success": True,
            "contract_id": contract_id,
            "warnings": warnings,
        }

    def _tool_save_draft(self, contract_id: str, content: str) -> dict:
        self.memory.save_draft(contract_id, content)
        name = _extract_contract_name(content) or contract_id
        self.memory.update_contract_index(contract_id, {
            "name": name,
            "status": "draft",
            "file": f"drafts/{contract_id}.md",
        })
        logger.info("Saved draft: %s", contract_id)
        return {"success": True, "contract_id": contract_id, "name": name}

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
        logger.info("Assigned role %s to %s", role, username)
        return {"success": True, "role": role, "username": username}

    def _tool_create_poll(self, question: str, options: list, channel_id: str = "") -> dict:
        if not self.mm:
            return {"error": "Mattermost client not available"}
        if not isinstance(options, list) or len(options) < 2:
            return {"error": "options must be a list with at least 2 items"}
        cid = channel_id or (self.mm.channel_id if hasattr(self.mm, "channel_id") else "")
        if not cid:
            return {"error": "channel_id is required"}
        result = self.mm.create_poll(cid, question, options)
        if isinstance(result, dict) and result.get("error"):
            return {"success": False, "error": result["error"]}
        return {"success": True}

    def _tool_set_contract_status(self, contract_id: str, status: str) -> dict:
        index = self.memory.read_json("contracts/index.json") or {"contracts": []}
        result = set_status(index, contract_id, status)
        if not result.ok:
            return {"success": False, "error": result.message}
        self.memory.write_json("contracts/index.json", index)
        return {"success": True, "contract_id": contract_id, "status": status, "message": result.message}
