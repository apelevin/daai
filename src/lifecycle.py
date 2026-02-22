from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


ALLOWED_STATUSES = {"draft", "in_review", "approved", "active", "deprecated", "archived"}


@dataclass
class StatusUpdateResult:
    ok: bool
    changed: bool
    message: str


def set_status(index: dict, contract_id: str, status: str) -> StatusUpdateResult:
    if status not in ALLOWED_STATUSES:
        return StatusUpdateResult(ok=False, changed=False, message=f"Invalid status: {status}")

    cid = (contract_id or "").strip().lower()
    if not cid:
        return StatusUpdateResult(ok=False, changed=False, message="Missing contract_id")

    if not index or not isinstance(index, dict):
        index = {"contracts": []}

    items = index.get("contracts")
    if not isinstance(items, list):
        items = []
        index["contracts"] = items

    for c in items:
        if isinstance(c, dict) and str(c.get("id") or "").lower() == cid:
            prev = c.get("status")
            if prev == status:
                return StatusUpdateResult(ok=True, changed=False, message=f"Status already {status}")
            c["status"] = status
            c["status_updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return StatusUpdateResult(ok=True, changed=True, message=f"Status {prev} -> {status}")

    # If not found, create a minimal record
    items.append({
        "id": cid,
        "name": cid,
        "status": status,
        "status_updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    return StatusUpdateResult(ok=True, changed=True, message=f"Created contract with status {status}")


def ensure_in_review(index: dict, contract_id: str) -> StatusUpdateResult:
    """If contract is missing or in draft, set to in_review."""
    cid = (contract_id or "").strip().lower()
    if not cid:
        return StatusUpdateResult(ok=False, changed=False, message="Missing contract_id")

    if not index or not isinstance(index, dict):
        index = {"contracts": []}

    items = index.get("contracts")
    if not isinstance(items, list):
        items = []
        index["contracts"] = items

    for c in items:
        if isinstance(c, dict) and str(c.get("id") or "").lower() == cid:
            st = c.get("status")
            if st in (None, "", "draft"):
                return set_status(index, cid, "in_review")
            return StatusUpdateResult(ok=True, changed=False, message=f"Status already {st}")

    # Not found -> create as in_review
    return set_status(index, cid, "in_review")
