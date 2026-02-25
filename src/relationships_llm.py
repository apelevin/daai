from __future__ import annotations

import json
from dataclasses import dataclass


ALLOWED_REL_TYPES = {"mentions", "subset_of", "aggregates", "inverse", "depends_on"}


@dataclass
class ProposedRelationship:
    from_id: str
    to_id: str
    type: str
    description: str


def build_prompt(*, contract_id: str, contract_md: str, known_contracts: list[dict]) -> tuple[str, str]:
    """Return (system, user) messages for LLM."""

    known = [
        {"id": c.get("id"), "name": c.get("name"), "status": c.get("status")}
        for c in known_contracts
        if isinstance(c, dict) and c.get("id")
    ]

    system = (
        "Ты — Data Architect. Твоя задача: предложить семантические связи между метриками (Data Contracts).\n\n"
        "Верни СТРОГО JSON без markdown и без пояснений. Схема:\n"
        "{\n"
        '  "relationships": [\n'
        '    {"from": "<id>", "to": "<id>", "type": "mentions|subset_of|aggregates|inverse|depends_on", "description": "..."}\n'
        "  ]\n"
        "}\n\n"
        "Правила:\n"
        "- Используй только id из списка известных контрактов.\n"
        "- Допускай максимум 10 связей.\n"
        "- from должен быть текущий contract_id.\n"
        "- type выбирай осмысленно: subset_of (подмножество), aggregates (агрегирует сущность), inverse (обратная связь), depends_on (нужен для расчёта/определения).\n"
        "- description: 1 короткое предложение по-русски."
    )

    user = (
        f"Текущий контракт id: {contract_id}\n\n"
        "Текст текущего контракта (markdown):\n"
        "---\n"
        f"{contract_md}\n"
        "---\n\n"
        "Известные контракты (id+name+status):\n"
        + json.dumps(known, ensure_ascii=False, indent=2)
    )

    return system, user


def parse_and_validate(raw: str, *, contract_id: str, known_ids: set[str]) -> list[ProposedRelationship]:
    """Parse LLM JSON and validate it defensively."""
    raw = (raw or "").strip()
    # strip accidental code fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join([ln for ln in lines if not ln.strip().startswith("```")]).strip()

    data = json.loads(raw)
    rels = data.get("relationships")
    if not isinstance(rels, list):
        return []

    out: list[ProposedRelationship] = []
    for item in rels[:10]:
        if not isinstance(item, dict):
            continue
        f = str(item.get("from") or "").strip().lower()
        t = str(item.get("to") or "").strip().lower()
        ty = str(item.get("type") or "").strip()
        desc = str(item.get("description") or "").strip()

        if f != contract_id.lower():
            continue
        if not t or t not in known_ids:
            continue
        if ty not in ALLOWED_REL_TYPES:
            continue
        if not desc:
            desc = f"{contract_id} → {t} ({ty})"

        out.append(ProposedRelationship(from_id=f, to_id=t, type=ty, description=desc))

    # dedup
    seen = set()
    uniq = []
    for r in out:
        key = (r.from_id, r.to_id, r.type)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq
