from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re


@dataclass
class ReviewItem:
    contract_id: str
    name: str
    agreed_date: str | None
    days: int
    reason: str


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # Accept YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def find_contracts_requiring_review(contracts: list[dict], *, now: datetime | None = None, days_threshold: int = 180) -> list[ReviewItem]:
    """Deterministic review trigger based on agreed_date.

    MVP: if agreed_date older than days_threshold -> requires review.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    items: list[ReviewItem] = []

    for c in contracts or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not cid:
            continue
        name = c.get("name") or cid
        agreed_date = c.get("agreed_date")
        dt = _parse_date(agreed_date)
        if not dt:
            continue
        days = (now - dt).days
        if days > days_threshold:
            items.append(ReviewItem(
                contract_id=str(cid),
                name=str(name),
                agreed_date=str(agreed_date),
                days=days,
                reason=f"прошло {days} дней с согласования (> {days_threshold})",
            ))

    # oldest first
    items.sort(key=lambda x: x.days, reverse=True)
    return items


@dataclass
class ApprovalPolicy:
    tier: str
    approval_required: list[str]
    consensus_threshold: float


@dataclass
class ApprovalCheck:
    ok: bool
    missing_roles: list[str]
    threshold: float
    have_ratio: float


def _extract_approvers(contract_md: str) -> list[str]:
    """Extract approver usernames from section '## Согласовано'."""
    if not contract_md:
        return []
    # naive: take lines starting with @
    lines = contract_md.splitlines()
    in_section = False
    users: list[str] = []
    for line in lines:
        if line.strip().startswith("## "):
            in_section = line.strip().lower().startswith("## согласовано")
            continue
        if not in_section:
            continue
        m = re.search(r"@([a-z0-9_\-.]+)", line, re.IGNORECASE)
        if m:
            users.append(m.group(1).lower())
    return list(dict.fromkeys(users))


def check_approval_policy(*, contract_md: str, policy: ApprovalPolicy, role_map: dict[str, str]) -> ApprovalCheck:
    """Check whether a contract meets tier approval requirements.

    role_map: username -> role key (ceo/cfo/circle_lead/data_lead)
    """
    approvers = _extract_approvers(contract_md)
    have_roles = {role_map.get(u) for u in approvers if role_map.get(u)}
    have_roles.discard(None)

    req = list(dict.fromkeys([r for r in (policy.approval_required or []) if r]))
    missing = [r for r in req if r not in have_roles]

    # ratio: how many required roles satisfied
    have = len(req) - len(missing)
    ratio = have / len(req) if req else 1.0

    ok = (ratio >= policy.consensus_threshold) and (len(missing) == 0 if policy.consensus_threshold == 1.0 else True)

    # For tier_1 with threshold 1.0, we require all roles explicitly.
    if policy.consensus_threshold == 1.0:
        ok = len(missing) == 0

    return ApprovalCheck(ok=ok, missing_roles=missing, threshold=policy.consensus_threshold, have_ratio=ratio)


def render_review_report(items: list[ReviewItem], *, days_threshold: int = 180) -> str:
    if not items:
        return f"✅ Нет контрактов, требующих пересмотра (порог {days_threshold} дней)."

    lines = [f"⏰ Контракты, требующие пересмотра (порог {days_threshold} дней):", ""]
    for it in items[:20]:
        lines.append(f"- `{it.contract_id}` ({it.name}) — {it.reason} — agreed_date={it.agreed_date}")
    if len(items) > 20:
        lines.append(f"…и ещё {len(items)-20}")
    return "\n".join(lines)
