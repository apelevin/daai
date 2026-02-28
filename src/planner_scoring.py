"""Priority scoring for Continuous Planner candidates.

Formula (0.0–1.0):
    score = 0.30 * tree_depth_score
          + 0.25 * queue_priority_score
          + 0.15 * blocker_age_score
          + 0.15 * stakeholder_avail
          + 0.10 * has_conflicts
          + 0.05 * in_progress_boost
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoredCandidate:
    contract_id: str
    metric_name: str
    score: float
    breakdown: dict[str, float]
    candidate_type: str  # new_contract | conflict_resolution | partial_update | stale_review
    tree_depth: int | None = None
    conflict_ids: list[str] | None = None
    stakeholders: list[str] | None = None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def tree_depth_score(depth: int | None, max_depth: int = 6) -> float:
    """Closer to root = higher score. depth=0 → 1.0, depth=max_depth → 0.0."""
    if depth is None:
        return 0.0
    return _clamp(1.0 - depth / max_depth)


def queue_priority_score(priority: int | None, max_priority: int = 20) -> float:
    """Lower priority number = higher score. priority=1 → 1.0, priority=max → 0.0."""
    if priority is None:
        return 0.0
    return _clamp(1.0 - (priority - 1) / max(max_priority - 1, 1))


def blocker_age_score(days_blocked: float) -> float:
    """Longer blocked = more urgent. 0 days → 0.0, 14+ days → 1.0."""
    return _clamp(days_blocked / 14.0)


def stakeholder_availability_score(available: bool) -> float:
    """1.0 if stakeholders are available (workday), 0.0 otherwise."""
    return 1.0 if available else 0.0


def conflict_score(has_conflicts: bool) -> float:
    """1.0 if there are active conflicts, 0.0 otherwise."""
    return 1.0 if has_conflicts else 0.0


def in_progress_boost_score(is_in_progress: bool) -> float:
    """1.0 if initiative is already in progress, 0.0 otherwise."""
    return 1.0 if is_in_progress else 0.0


def compute_priority_score(
    *,
    depth: int | None = None,
    priority: int | None = None,
    days_blocked: float = 0.0,
    stakeholder_available: bool = True,
    has_conflicts: bool = False,
    is_in_progress: bool = False,
) -> tuple[float, dict[str, float]]:
    """Compute weighted priority score and return (score, breakdown)."""
    td = tree_depth_score(depth)
    qp = queue_priority_score(priority)
    ba = blocker_age_score(days_blocked)
    sa = stakeholder_availability_score(stakeholder_available)
    cs = conflict_score(has_conflicts)
    ip = in_progress_boost_score(is_in_progress)

    score = (
        0.30 * td
        + 0.25 * qp
        + 0.15 * ba
        + 0.15 * sa
        + 0.10 * cs
        + 0.05 * ip
    )

    breakdown = {
        "tree_depth": td,
        "queue_priority": qp,
        "blocker_age": ba,
        "stakeholder_avail": sa,
        "has_conflicts": cs,
        "in_progress": ip,
    }

    return round(score, 4), breakdown


def rank_candidates(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Sort candidates by score descending."""
    return sorted(candidates, key=lambda c: c.score, reverse=True)
