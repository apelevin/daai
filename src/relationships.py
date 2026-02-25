from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Relationship:
    from_id: str
    to_id: str
    type: str  # mentions|subset_of|aggregates|inverse|depends_on
    description: str


def _norm_id(s: str) -> str:
    return (s or "").strip().lower()


def detect_mentions(*, contract_id: str, contract_md: str, known_contract_ids: list[str]) -> list[Relationship]:
    """Very simple deterministic relationship detector.

    v1: if contract text mentions another known contract id (as a whole word),
    add a relationship: contract_id --mentions--> other.

    This avoids LLM and is safe.
    """
    cid = _norm_id(contract_id)
    text = (contract_md or "").lower()
    rels: list[Relationship] = []

    for other in known_contract_ids:
        oid = _norm_id(other)
        if not oid or oid == cid:
            continue
        # word boundary match (ids are snake_case-ish)
        if re.search(rf"\b{re.escape(oid)}\b", text):
            rels.append(Relationship(
                from_id=cid,
                to_id=oid,
                type="mentions",
                description=f"{cid} mentions {oid} in contract text",
            ))

    return rels


def upsert_relationships(index: dict, new_rels: list[Relationship]) -> tuple[dict, int]:
    """Upsert relationships into a relationships.json-like dict.

    Dedup key: (from,to,type)
    """
    if not index or not isinstance(index, dict):
        index = {"relationships": []}

    items = index.get("relationships")
    if not isinstance(items, list):
        items = []

    existing = {(r.get("from"), r.get("to"), r.get("type")) for r in items if isinstance(r, dict)}
    added = 0
    for r in new_rels:
        key = (r.from_id, r.to_id, r.type)
        if key in existing:
            continue
        items.append({
            "from": r.from_id,
            "to": r.to_id,
            "type": r.type,
            "description": r.description,
        })
        existing.add(key)
        added += 1

    index["relationships"] = items
    return index, added
