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
    "Известные проблемы",
    "Связанные контракты",
    "Согласовано",
    "История изменений",
]


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
    sections = _extract_sections(contract_md)

    # 1) required sections
    missing = [s for s in REQUIRED_SECTIONS if s not in sections or not sections[s]]
    for s in missing:
        issues.append(ValidationIssue(code="missing_section", message=f"Не заполнена секция: {s}"))

    # 2) formula must include human + pseudo-sql
    formula = sections.get("Формула", "")
    if formula:
        if "человеческая" not in formula.lower():
            issues.append(
                ValidationIssue(
                    code="formula_missing_human",
                    message="В секции «Формула» должна быть строка «Человеческая: ...»",
                )
            )
        if "псевдо" not in formula.lower() or "sql" not in formula.lower():
            issues.append(
                ValidationIssue(
                    code="formula_missing_sql",
                    message="В секции «Формула» должен быть блок «Псевдо‑SQL: ...»",
                )
            )

    # 3) extra time linkage path
    linkage = sections.get("Связь с Extra Time", "")
    if linkage:
        if "extra time" not in linkage.lower() or "→" not in linkage:
            issues.append(
                ValidationIssue(
                    code="missing_extra_time_path",
                    message="В секции «Связь с Extra Time» должен быть путь вида «X → ... → Extra Time»",
                )
            )

    ok = len(issues) == 0
    return ValidationReport(ok=ok, issues=issues)
