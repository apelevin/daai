"""SuggestionEngine: proactively suggests next Data Contracts to agree on.

V1:  suggest_after_agreement — triggered after a contract is saved.
V1.5: coverage_scan — periodic scan for uncovered metrics.
V2:  extensible via SuggestionSource interface.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from src.metrics_tree import (
    parse_tree,
    get_uncovered_nodes,
    get_path_to_root,
    get_siblings,
    find_node_by_id,
    TreeNode,
)

logger = logging.getLogger(__name__)

# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SuggestionCandidate:
    contract_id: str         # "activation_rate"
    metric_name: str         # "Activation Rate"
    tree_path: str           # "Activation Rate → Activation → MAU → Extra Time"
    priority: int | None     # from queue.json priority table
    reason: str              # why we suggest this now
    stakeholders: list[str]  # @username from circles.md
    related_to: str | None   # which contract triggered this


# ── V2 hook: extensible sources ──────────────────────────────────────────────

class SuggestionSource:
    """Base class for suggestion sources (V2 extensibility)."""

    def get_candidates(self) -> list[SuggestionCandidate]:
        raise NotImplementedError


class MetricsTreeSource(SuggestionSource):
    """V1 source: metrics tree analysis."""

    def __init__(self, engine: SuggestionEngine):
        self._engine = engine

    def get_candidates(self) -> list[SuggestionCandidate]:
        return self._engine.coverage_scan()


# ── Stakeholder resolution ───────────────────────────────────────────────────

_CIRCLE_KEYWORDS: dict[str, list[str]] = {
    "Sales": ["WIN", "NI", "pipeline", "conversion", "sales", "acquisition", "новых клиентов"],
    "Product": ["MAU", "activation", "feature", "adoption", "product", "onboarding"],
    "Customer Success": ["Churn", "Retention", "NPS", "CSAT", "REC", "renewal"],
    "Analytics & Data": ["data", "quality", "metric", "analytics", "reporting"],
    "Engineering": ["uptime", "deployment", "infrastructure", "SLA", "error rate", "load time"],
}


def _parse_circles(circles_md: str) -> dict[str, str]:
    """Parse circles.md → {circle_name: @username}."""
    result: dict[str, str] = {}
    current_circle = None
    for line in (circles_md or "").splitlines():
        if line.startswith("## "):
            current_circle = line[3:].strip()
        elif current_circle and "Ответственный:" in line:
            m = re.search(r"@(\S+)", line)
            if m:
                result[current_circle] = m.group(1)
    return result


def _resolve_stakeholders(metric_name: str, circles_md: str) -> list[str]:
    """Match metric name against circle keywords → list of @usernames."""
    circle_leads = _parse_circles(circles_md)
    if not circle_leads:
        return []

    matched: list[str] = []
    name_lower = metric_name.lower()

    for circle, keywords in _CIRCLE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in name_lower:
                lead = circle_leads.get(circle)
                if lead and lead not in matched:
                    matched.append(lead)
                break

    return matched


def _slugify_metric(name: str) -> str:
    """Convert metric name to contract_id slug."""
    from src.router import _slugify
    return _slugify(name)


# ── Main engine ──────────────────────────────────────────────────────────────

_COOLDOWN_DAYS = 14       # Don't re-suggest same contract within 14 days
_DISMISS_COOLDOWN_DAYS = 30  # Don't re-suggest dismissed within 30 days
_MAX_PER_DAY = 1          # Max 1 suggestion per day


class SuggestionEngine:
    def __init__(self, memory, llm_client=None):
        self.memory = memory
        self.llm = llm_client

    def suggest_after_agreement(self, agreed_id: str) -> list[SuggestionCandidate]:
        """V1: Suggest next contract after one is agreed.

        Strategy:
        1. Parse tree → find agreed node
        2. Get siblings + children with ← DATA CONTRACT but no ✅
        3. Boost from queue.json priorities
        4. Return top 1-2 candidates
        """
        tree_md = self.memory.read_file("context/metrics_tree.md") or ""
        root = parse_tree(tree_md)
        if not root:
            return []

        node = find_node_by_id(root, agreed_id)
        if not node:
            return []

        # Collect nearby uncovered nodes: siblings first, then parent's siblings' children
        nearby: list[TreeNode] = []
        for sib in get_siblings(node):
            if sib.has_contract_marker and not sib.is_agreed:
                nearby.append(sib)
            for child in sib.children:
                if child.has_contract_marker and not child.is_agreed:
                    nearby.append(child)

        # Also check parent's siblings (cousins)
        if node.parent:
            for uncle in get_siblings(node.parent):
                for child in uncle.children:
                    if child.has_contract_marker and not child.is_agreed:
                        nearby.append(child)

        if not nearby:
            return []

        # Build candidates
        circles_md = self.memory.read_file("context/circles.md") or ""
        queue = self.memory.get_queue()
        queue_map = {item["id"]: item.get("priority") for item in queue if isinstance(item, dict)}

        candidates: list[SuggestionCandidate] = []
        seen_ids: set[str] = set()

        for tn in nearby:
            cid = _slugify_metric(tn.short_name)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            priority = queue_map.get(cid)
            stakeholders = _resolve_stakeholders(tn.short_name, circles_md)

            candidates.append(SuggestionCandidate(
                contract_id=cid,
                metric_name=tn.short_name,
                tree_path=get_path_to_root(tn),
                priority=priority,
                reason=f"Связан с только что согласованным контрактом {agreed_id}",
                stakeholders=stakeholders,
                related_to=agreed_id,
            ))

        # Sort: queue priority first (lower = higher), then tree depth (shallower first)
        def _sort_key(c: SuggestionCandidate) -> tuple:
            p = c.priority if c.priority is not None else 999
            # Find depth from tree_path (count arrows)
            depth = c.tree_path.count("→")
            return (p, depth)

        candidates.sort(key=_sort_key)
        return candidates[:2]

    def coverage_scan(self) -> list[SuggestionCandidate]:
        """V1.5: Scan for uncovered metrics in the tree.

        Strategy:
        1. get_uncovered_nodes() from tree
        2. Minus already covered in index (any active status)
        3. Sort by queue priority
        """
        tree_md = self.memory.read_file("context/metrics_tree.md") or ""
        root = parse_tree(tree_md)
        if not root:
            return []

        uncovered = get_uncovered_nodes(root)
        if not uncovered:
            return []

        # Exclude those already in index with active statuses
        index = self.memory.list_contracts() or []
        active_ids = set()
        for c in index:
            if isinstance(c, dict) and c.get("status") in ("draft", "in_review", "approved", "active", "agreed"):
                active_ids.add(str(c.get("id", "")).lower())

        circles_md = self.memory.read_file("context/circles.md") or ""
        queue = self.memory.get_queue()
        queue_map = {item["id"]: item.get("priority") for item in queue if isinstance(item, dict)}

        candidates: list[SuggestionCandidate] = []
        for tn in uncovered:
            cid = _slugify_metric(tn.short_name)
            if cid in active_ids:
                continue

            priority = queue_map.get(cid)
            stakeholders = _resolve_stakeholders(tn.short_name, circles_md)

            candidates.append(SuggestionCandidate(
                contract_id=cid,
                metric_name=tn.short_name,
                tree_path=get_path_to_root(tn),
                priority=priority,
                reason="Метрика отмечена для контракта, но ещё не согласована",
                stakeholders=stakeholders,
                related_to=None,
            ))

        # Sort by priority
        def _sort_key(c: SuggestionCandidate) -> tuple:
            p = c.priority if c.priority is not None else 999
            depth = c.tree_path.count("→")
            return (p, depth)

        candidates.sort(key=_sort_key)
        return candidates

    def filter_already_suggested(self, candidates: list[SuggestionCandidate]) -> list[SuggestionCandidate]:
        """Triple dedup: index + suggestions.json + cooldown."""
        if not candidates:
            return []

        # 1. Already in index with any active status
        index = self.memory.list_contracts() or []
        active_ids = set()
        for c in index:
            if isinstance(c, dict) and c.get("status") in ("draft", "in_review", "approved", "active", "agreed"):
                active_ids.add(str(c.get("id", "")).lower())

        # 2. Recently suggested (cooldown)
        suggestions = self.memory.get_suggestions()
        now = datetime.now(timezone.utc)
        recent_ids: set[str] = set()
        dismissed_ids: set[str] = set()

        for s in suggestions:
            cid = s.get("contract_id", "")
            status = s.get("status", "")
            suggested_at = s.get("suggested_at", "")

            try:
                dt = datetime.fromisoformat(suggested_at)
            except (ValueError, TypeError):
                continue

            if status == "dismissed":
                if now - dt < timedelta(days=_DISMISS_COOLDOWN_DAYS):
                    dismissed_ids.add(cid)
            elif status in ("suggested", "accepted"):
                if now - dt < timedelta(days=_COOLDOWN_DAYS):
                    recent_ids.add(cid)

        result: list[SuggestionCandidate] = []
        for c in candidates:
            if c.contract_id in active_ids:
                continue
            if c.contract_id in recent_ids:
                continue
            if c.contract_id in dismissed_ids:
                continue
            result.append(c)

        return result

    def can_suggest_today(self) -> bool:
        """Rate limit: max 1 suggestion per day (UTC)."""
        suggestions = self.memory.get_suggestions()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        count = 0
        for s in suggestions:
            suggested_at = s.get("suggested_at", "")
            if suggested_at.startswith(today):
                count += 1

        return count < _MAX_PER_DAY

    def record_suggestion(
        self,
        candidates: list[SuggestionCandidate],
        trigger: str,
        thread_id: str | None = None,
    ) -> None:
        """Save suggestion records to tasks/suggestions.json."""
        suggestions = self.memory.get_suggestions()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y%m%d")

        # Generate sequential ID for today
        existing_today = sum(1 for s in suggestions if s.get("id", "").startswith(f"sug_{today}"))

        for i, c in enumerate(candidates):
            seq = existing_today + i + 1
            suggestions.append({
                "id": f"sug_{today}_{seq:03d}",
                "contract_id": c.contract_id,
                "metric_name": c.metric_name,
                "trigger": trigger,
                "suggested_at": now.isoformat(),
                "thread_id": thread_id,
                "status": "suggested",
                "status_updated_at": now.isoformat(),
            })

        self.memory.save_suggestions(suggestions)

    def format_suggestion_message(
        self,
        candidates: list[SuggestionCandidate],
        trigger: str,
        use_poll: bool = False,
    ) -> str:
        """Format suggestion(s) as a Mattermost message."""
        if not candidates:
            return ""

        if use_poll and len(candidates) > 1:
            return self._format_poll_message(candidates)

        parts: list[str] = []
        for c in candidates:
            stakeholders_str = ", ".join(f"@{s}" for s in c.stakeholders) if c.stakeholders else "—"
            parts.append(
                f":dart: **Предложение: следующий Data Contract**\n\n"
                f"**{c.metric_name}** (`{c.contract_id}`)\n\n"
                f"Почему сейчас: {c.reason}\n"
                f"Путь: {c.tree_path}\n"
                f"Ответственные: {stakeholders_str}\n\n"
                f"> Хотите начать? Ответьте здесь или: `начни контракт {c.contract_id}`"
            )

        return "\n\n---\n\n".join(parts)

    def _format_poll_message(self, candidates: list[SuggestionCandidate]) -> str:
        """Format as poll command for Matterpoll."""
        options = " ".join(f'"{c.metric_name}"' for c in candidates)
        return f'/poll "Какой контракт согласуем следующим?" {options}'

    def format_coverage_message(self, candidates: list[SuggestionCandidate]) -> str:
        """Format coverage scan results as a message."""
        if not candidates:
            return ""

        lines = [":bar_chart: **Обзор покрытия метрик контрактами**\n"]
        lines.append(f"Найдено {len(candidates)} метрик без согласованного контракта:\n")

        for i, c in enumerate(candidates, 1):
            priority_str = f" (приоритет {c.priority})" if c.priority else ""
            stakeholders_str = ", ".join(f"@{s}" for s in c.stakeholders) if c.stakeholders else ""
            lines.append(f"{i}. **{c.metric_name}**{priority_str} — {c.tree_path}")
            if stakeholders_str:
                lines.append(f"   Ответственные: {stakeholders_str}")

        lines.append(f"\n> Хотите начать с какого-то? Напишите: `начни контракт <id>`")
        return "\n".join(lines)
