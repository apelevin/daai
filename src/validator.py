from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ValidationIssue:
    code: str
    message: str


@dataclass
class ValidationReport:
    ok: bool
    issues: list[ValidationIssue]


REQUIRED_SECTIONS = [
    "Статус",
    "Определение",
    "Формула",
    "Источник данных",
    "Включает",
    "Исключения",
    "Гранулярность",
    "Ответственный за данные",
    "Ответственный за расчёт",
    "Связь с Extra Time",
    "Потребители",
    "Состояние данных",
    "Согласовано",
    "История изменений",
]

OPTIONAL_SECTIONS = ["Известные проблемы", "Связанные контракты"]

# All accepted arrow characters/sequences for "Связь с Extra Time"
ARROW_PATTERNS = ["→", "->", "—>", "=>"]


def _extract_sections(md: str) -> dict[str, str]:
    """Extract sections by '## <title>' headings."""
    md = md or ""
    lines = md.splitlines()
    sections: dict[str, list[str]] = {}
    current = None

    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)

    return {k: "\n".join(v).strip() for k, v in sections.items()}


def validate_contract(contract_md: str) -> ValidationReport:
    """Deterministic validation for Data Contract markdown."""
    issues: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    sections = _extract_sections(contract_md)

    # 1) required sections
    missing = [s for s in REQUIRED_SECTIONS if s not in sections or not sections[s]]
    for s in missing:
        issues.append(ValidationIssue(code="missing_section", message=f"Не заполнена секция: {s}"))

    # 1b) optional sections — warn but do not block
    for s in OPTIONAL_SECTIONS:
        if s not in sections or not sections[s]:
            warnings.append(ValidationIssue(code="missing_optional_section", message=f"Рекомендуется заполнить секцию: {s}"))

    # 2) formula — must be non-empty (soft warnings for missing sub-formats)
    formula = sections.get("Формула", "")
    if formula:
        if "человеческая" not in formula.lower():
            warnings.append(
                ValidationIssue(
                    code="formula_missing_human",
                    message="Рекомендуется добавить строку «Человеческая: ...» в секцию «Формула»",
                )
            )
        if "псевдо" not in formula.lower() or "sql" not in formula.lower():
            warnings.append(
                ValidationIssue(
                    code="formula_missing_sql",
                    message="Рекомендуется добавить блок «Псевдо‑SQL: ...» в секцию «Формула»",
                )
            )

    # 3) extra time linkage path — accept multiple arrow styles
    linkage = sections.get("Связь с Extra Time", "")
    if linkage:
        has_extra_time = "extra time" in linkage.lower()
        has_arrow = any(arrow in linkage for arrow in ARROW_PATTERNS)
        if not has_extra_time or not has_arrow:
            issues.append(
                ValidationIssue(
                    code="missing_extra_time_path",
                    message="В секции «Связь с Extra Time» должен быть путь вида «X → ... → Extra Time»",
                )
            )

    ok = len(issues) == 0
    return ValidationReport(ok=ok, issues=issues + warnings)
