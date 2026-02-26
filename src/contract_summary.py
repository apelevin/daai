"""Cross-contract summaries: deterministic extraction + prompt formatting."""

from __future__ import annotations

import re

RE_H1 = re.compile(r"^#\s+Data Contract:\s*(.+?)\s*$", re.MULTILINE)
RE_H2 = re.compile(r"^##\s+(.+?)\s*$")

# Section name → max snippet length
_SECTION_LIMITS = {
    "Определение": 120,
    "Формула": 100,
    "Источник данных": 80,
    "Связь с Extra Time": 100,
}

_STATUS_ORDER = {"agreed": 0, "in_review": 1, "draft": 2}


def _snippet(text: str, max_len: int) -> str:
    """First non-empty line, truncated to max_len."""
    if not text:
        return ""
    first = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if not first:
        return ""
    if len(first) > max_len:
        return first[:max_len - 1] + "…"
    return first


def _extract_sections(md: str) -> dict[str, str]:
    """Extract ## sections from markdown (same logic as analyzer.py)."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in md.splitlines():
        m = RE_H2.match(line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def generate_summary(contract_id: str, markdown: str, status: str) -> dict:
    """Generate a deterministic summary dict from contract markdown."""
    md = markdown or ""

    # Extract name from # Data Contract: <name>
    m = RE_H1.search(md)
    name = m.group(1).strip() if m else contract_id

    sections = _extract_sections(md)

    result = {
        "id": contract_id,
        "name": name,
        "status": status,
    }

    field_map = {
        "Определение": "definition",
        "Формула": "formula",
        "Источник данных": "data_source",
        "Связь с Extra Time": "extra_time_path",
    }

    for section_name, field_key in field_map.items():
        raw = sections.get(section_name, "")
        result[field_key] = _snippet(raw, _SECTION_LIMITS[section_name])

    return result


def format_summaries_for_prompt(summaries: dict) -> str:
    """Format all summaries into a system prompt block.

    Returns "" if summaries is empty.
    """
    if not summaries:
        return ""

    # Group by status
    groups: dict[str, list[dict]] = {}
    for s in summaries.values():
        st = s.get("status", "draft")
        groups.setdefault(st, []).append(s)

    lines = [
        "# Ландшафт контрактов",
        "",
        "Ниже — краткие суммари всех контрактов. Используй их чтобы:",
        "- НЕ дублировать определения, которые уже зафиксированы",
        "- Сохранять единую терминологию",
        "- Ссылаться на связанные контракты",
        "- Для полного текста используй `read_contract` / `read_draft`",
        "",
    ]

    status_labels = {
        "agreed": "Согласованные",
        "in_review": "На ревью",
        "draft": "Черновики",
    }

    for status in sorted(groups.keys(), key=lambda s: _STATUS_ORDER.get(s, 99)):
        label = status_labels.get(status, status)
        lines.append(f"## {label}")
        lines.append("")
        for s in sorted(groups[status], key=lambda x: x.get("id", "")):
            parts = [f"`{s.get('id', '?')}` — {s.get('name', '?')}"]
            if s.get("definition"):
                parts.append(f"Опр: {s['definition']}")
            if s.get("formula"):
                parts.append(f"Формула: {s['formula']}")
            if s.get("extra_time_path"):
                parts.append(f"ET: {s['extra_time_path']}")
            lines.append(" | ".join(parts))
        lines.append("")

    return "\n".join(lines).rstrip()
