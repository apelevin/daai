import re
from dataclasses import dataclass


@dataclass
class Conflict:
    type: str
    severity: str  # low|medium|high
    title: str
    details: str
    contracts: list[str]  # contract ids


RE_H2 = re.compile(r"^##\s+(.+?)\s*$")

STOP_WORDS_RU = {
    "и", "в", "во", "на", "по", "из", "для", "что", "это", "как", "когда", "где", "или", "а",
    "мы", "вы", "они", "он", "она", "оно", "этот", "эта", "эти", "тот", "та", "те",
    "не", "нет", "да", "же", "ли", "бы",
    "секция", "контракт", "метрика", "показатель",
}


def _extract_sections(md: str) -> dict[str, str]:
    md = md or ""
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


def _normalize_name(name: str) -> str:
    s = (name or "").strip().lower()
    # treat punctuation/hyphens/underscores as spaces
    s = re.sub(r"[\-_/:]+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize_definition(text: str) -> set[str]:
    text = (text or "").lower()
    # keep words and digits
    tokens = re.findall(r"[a-zа-я0-9_\-]+", text, flags=re.IGNORECASE)
    out: set[str] = set()
    for t in tokens:
        t = t.strip("-_")
        if len(t) < 3:
            continue
        if t in STOP_WORDS_RU:
            continue
        out.add(t)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _extract_name(md: str) -> str | None:
    for line in (md or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith("# data contract:"):
            return s.split(":", 1)[1].strip() or None
    return None


def _extract_related_contract_ids(md: str) -> list[str]:
    sections = _extract_sections(md)
    rel = sections.get("Связанные контракты", "")
    if not rel:
        return []
    ids: list[str] = []
    for line in rel.splitlines():
        s = line.strip()
        if not s:
            continue
        # accept "- id" / "* id" / "id"
        s = re.sub(r"^[\-*•]\s+", "", s)
        # take first token-ish
        s = s.split("(", 1)[0].strip()
        s = re.sub(r"[^a-zA-Z0-9_\-]", "", s)
        if s:
            ids.append(s.lower())
    return ids


class MetricsAnalyzer:
    def __init__(self, memory):
        self.memory = memory

    def detect_conflicts(self, *, only_contract_ids: list[str] | None = None) -> list[Conflict]:
        """Detect obvious conflicts between agreed contracts.

        v1.3 (deterministic):
        - Same name, different formula
        - Missing/invalid Extra Time linkage path
        - Missing formula/definition/data-source sections
        - Quality red flags (ambiguous terms in formula)
        - Unknown/self related-contract references
        - Cyclic dependencies based on "Связанные контракты" (deduped)
        - Overlapping definitions heuristic (Jaccard on ключевые термины)
        """
        conflicts: list[Conflict] = []

        items = self.memory.list_contracts() or []
        # prefer agreed/active; if absent, include all
        filtered = []
        for c in items:
            cid = c.get("id")
            if not cid:
                continue
            if only_contract_ids and cid not in only_contract_ids:
                continue
            filtered.append(c)

        # Load markdown for each contract
        loaded: dict[str, dict] = {}
        for c in filtered:
            cid = c["id"]
            md = self.memory.get_contract(cid) or ""
            sections = _extract_sections(md)
            name = _extract_name(md) or c.get("name") or cid
            formula = sections.get("Формула", "")
            linkage = sections.get("Связь с Extra Time", "")
            related = _extract_related_contract_ids(md)
            definition = sections.get("Определение", "")
            loaded[cid] = {
                "id": cid,
                "name": name,
                "name_norm": _normalize_name(name),
                "formula": (formula or "").strip(),
                "linkage": (linkage or "").strip(),
                "related": related,
                "definition": (definition or "").strip(),
                "def_tokens": _tokenize_definition(definition or ""),
            }

        # Missing key sections + basic quality checks
        ambiguous_words = ["примерно", "около", "приблизительно", "где-то", "как-то", "иногда"]

        for cid, d in loaded.items():
            md = self.memory.get_contract(cid) or ""
            sections = _extract_sections(md)

            if not (d.get("formula") or "").strip():
                conflicts.append(Conflict(
                    type="missing_formula",
                    severity="high",
                    title=f"Нет формулы: {d['name']}",
                    details="Секция «Формула» пустая или отсутствует.",
                    contracts=[cid],
                ))
            else:
                lowf = d["formula"].lower()
                if any(w in lowf for w in ambiguous_words):
                    conflicts.append(Conflict(
                        type="ambiguous_formula",
                        severity="medium",
                        title=f"Неоднозначная формула: {d['name']}",
                        details="В формуле есть слова неопределённости (например: 'примерно/около'). Лучше сделать формулу однозначной.",
                        contracts=[cid],
                    ))

            definition = sections.get("Определение", "").strip()
            if not definition:
                conflicts.append(Conflict(
                    type="missing_definition",
                    severity="high",
                    title=f"Нет определения: {d['name']}",
                    details="Секция «Определение» пустая или отсутствует.",
                    contracts=[cid],
                ))

            src = sections.get("Источник данных", "").strip()
            if not src:
                conflicts.append(Conflict(
                    type="missing_data_source",
                    severity="high",
                    title=f"Нет источника данных: {d['name']}",
                    details="Секция «Источник данных» пустая или отсутствует.",
                    contracts=[cid],
                ))

        # Missing/invalid linkage
        for cid, d in loaded.items():
            link = d.get("linkage", "")
            if not link:
                conflicts.append(Conflict(
                    type="missing_extra_time_linkage",
                    severity="high",
                    title=f"Нет связи с Extra Time: {d['name']}",
                    details="Секция «Связь с Extra Time» пустая или отсутствует. Нужен путь вида: X → ... → Extra Time.",
                    contracts=[cid],
                ))
                continue
            low_link = link.lower()
            if "extra time" not in low_link or "→" not in link:
                conflicts.append(Conflict(
                    type="invalid_extra_time_linkage",
                    severity="medium",
                    title=f"Неочевидный путь к Extra Time: {d['name']}",
                    details="В «Связь с Extra Time» должен быть путь вида: X → ... → Extra Time.",
                    contracts=[cid],
                ))
                continue

            # Stronger checks (v1.2+): should end with Extra Time and start with metric name
            parts = [p.strip() for p in link.split("→") if p.strip()]
            if len(parts) < 2:
                conflicts.append(Conflict(
                    type="extra_time_path_too_short",
                    severity="low",
                    title=f"Слишком короткий путь к Extra Time: {d['name']}",
                    details="Ожидается путь вида «X → ... → Extra Time» (минимум 2 узла).",
                    contracts=[cid],
                ))
            else:
                if _normalize_name(parts[-1]) != _normalize_name("Extra Time"):
                    conflicts.append(Conflict(
                        type="extra_time_path_not_ending",
                        severity="medium",
                        title=f"Путь не заканчивается на Extra Time: {d['name']}",
                        details=f"Последний узел пути должен быть 'Extra Time'. Сейчас: '{parts[-1]}'.",
                        contracts=[cid],
                    ))

                # Compare first part with metric name (normalized)
                if _normalize_name(parts[0]) != _normalize_name(d["name"]):
                    conflicts.append(Conflict(
                        type="extra_time_path_not_starting",
                        severity="low",
                        title=f"Путь к Extra Time не начинается с метрики: {d['name']}",
                        details=f"Первый узел пути должен быть названием метрики ('{d['name']}'). Сейчас: '{parts[0]}'.",
                        contracts=[cid],
                    ))

        # Name collisions with different formula
        by_name: dict[str, list[str]] = {}
        for cid, d in loaded.items():
            by_name.setdefault(d["name_norm"], []).append(cid)

        for name_norm, cids in by_name.items():
            if len(cids) < 2:
                continue
            # Compare formulas
            formulas = {loaded[cid]["formula"] for cid in cids}
            if len(formulas) <= 1:
                continue
            name = loaded[cids[0]]["name"]
            details_lines = ["Одинаковое название метрики, но разные формулы:", ""]
            for cid in cids:
                f = loaded[cid]["formula"] or "(пусто)"
                # keep short
                f_short = (f[:240] + "…") if len(f) > 240 else f
                details_lines.append(f"- {cid}: {f_short}")
            conflicts.append(Conflict(
                type="same_name_different_formula",
                severity="high",
                title=f"Конфликт формулы: {name}",
                details="\n".join(details_lines),
                contracts=cids,
            ))

        # Related-contract reference checks + build graph for cycle detection
        graph: dict[str, list[str]] = {}
        for cid, d in loaded.items():
            rel = d.get("related") or []
            if cid in rel:
                conflicts.append(Conflict(
                    type="self_related_reference",
                    severity="medium",
                    title=f"Самоссылка в связанных контрактах: {d['name']}",
                    details="В «Связанные контракты» указан сам контракт. Это почти всегда ошибка.",
                    contracts=[cid],
                ))

            unknown = [x for x in rel if x not in loaded]
            if unknown:
                conflicts.append(Conflict(
                    type="unknown_related_contract",
                    severity="low",
                    title=f"Неизвестные связанные контракты: {d['name']}",
                    details="В «Связанные контракты» есть id, которых нет в contracts/index.json: " + ", ".join(unknown),
                    contracts=[cid],
                ))

            # keep only edges to known contracts
            graph[cid] = [x for x in rel if x in loaded]

        # Cycle detection (dedup by canonical cycle key)
        seen: set[str] = set()
        stack: set[str] = set()
        path: list[str] = []
        reported_cycles: set[tuple[str, ...]] = set()

        def _canon_cycle(cycle: list[str]) -> tuple[str, ...]:
            if len(cycle) < 3:
                return tuple(cycle)
            core = cycle[:-1] if cycle and cycle[0] == cycle[-1] else cycle
            if not core:
                return tuple()
            rots = [tuple(core[i:] + core[:i]) for i in range(len(core))]
            return min(rots)

        def dfs(u: str):
            seen.add(u)
            stack.add(u)
            path.append(u)
            for v in graph.get(u, []):
                if v not in seen:
                    dfs(v)
                elif v in stack:
                    try:
                        idx = path.index(v)
                        cycle = path[idx:] + [v]
                    except ValueError:
                        cycle = [u, v, u]
                    key = _canon_cycle(cycle)
                    if key and key not in reported_cycles:
                        reported_cycles.add(key)
                        title = "Циклическая зависимость контрактов"
                        details = "Обнаружен цикл по секции «Связанные контракты»: " + " → ".join(cycle)
                        conflicts.append(Conflict(
                            type="cyclic_dependency",
                            severity="high",
                            title=title,
                            details=details,
                            contracts=list(dict.fromkeys(cycle)),
                        ))
            stack.remove(u)
            path.pop()

        for cid in graph.keys():
            if cid not in seen:
                dfs(cid)

        # Overlapping definitions heuristic
        cids = list(loaded.keys())
        for i in range(len(cids)):
            for j in range(i + 1, len(cids)):
                a = loaded[cids[i]]
                b = loaded[cids[j]]
                if a["name_norm"] == b["name_norm"]:
                    continue
                a_tokens = a.get("def_tokens") or set()
                b_tokens = b.get("def_tokens") or set()
                sim = _jaccard(a_tokens, b_tokens)
                inter = a_tokens & b_tokens

                # Heuristic: either high Jaccard OR enough shared keywords
                # NOTE: for RU text with synonyms, Jaccard can be low; allow >=5 shared terms as a soft signal.
                if (
                    (sim >= 0.45 and len(a_tokens) >= 6 and len(b_tokens) >= 6)
                    or (len(inter) >= 5)
                ):
                    shared_preview = ", ".join(sorted(list(inter))[:12])
                    conflicts.append(Conflict(
                        type="overlapping_definitions",
                        severity="medium",
                        title=f"Похоже пересекающиеся определения: {a['name']} ↔ {b['name']}",
                        details=(
                            f"Эвристика: сходство определений (Jaccard) = {sim:.2f}. "
                            f"Общие термины: {shared_preview or '(нет)'}"
                        ),
                        contracts=[a["id"], b["id"]],
                    ))

        return conflicts


def render_conflicts(conflicts: list[Conflict]) -> str:
    if not conflicts:
        return "✅ Конфликтов не найдено."

    sev_rank = {"high": 0, "medium": 1, "low": 2}

    # Split per-contract issues vs cross-contract issues
    per_contract: dict[str, list[Conflict]] = {}
    cross: list[Conflict] = []
    for c in conflicts:
        if len(c.contracts) == 1:
            cid = c.contracts[0]
            per_contract.setdefault(cid, []).append(c)
        else:
            cross.append(c)

    lines = ["🔍 Проактивный аудит: найдено проблем: %d" % len(conflicts), ""]

    # Cross-contract conflicts first
    if cross:
        cross_sorted = sorted(cross, key=lambda c: (sev_rank.get(c.severity, 9), c.type, c.title))
        lines.append("### Межконтрактные конфликты")
        for c in cross_sorted[:8]:
            ids = ", ".join([f"`{x}`" for x in c.contracts])
            lines.append(f"- [{c.severity}] {c.title} ({ids})")
        if len(cross_sorted) > 8:
            lines.append(f"…и ещё {len(cross_sorted)-8}")
        lines.append("")

    # Group per-contract issues
    if per_contract:
        lines.append("### Проблемы по контрактам")
        # sort contract groups by max severity
        def group_key(item):
            cid, items = item
            max_rank = min(sev_rank.get(x.severity, 9) for x in items)
            return (max_rank, cid)

        for cid, items in sorted(per_contract.items(), key=group_key)[:10]:
            items_sorted = sorted(items, key=lambda c: (sev_rank.get(c.severity, 9), c.type, c.title))
            # render compact: contract id + up to 3 issue titles
            titles = "; ".join([x.title for x in items_sorted[:3]])
            more = "" if len(items_sorted) <= 3 else f"; …+{len(items_sorted)-3}"
            max_sev = items_sorted[0].severity
            lines.append(f"- [{max_sev}] `{cid}`: {titles}{more}")

        if len(per_contract) > 10:
            lines.append(f"…и ещё {len(per_contract)-10} контракт(ов) с проблемами")

    lines.append("")
    lines.append("Если хочешь — напиши: «покажи детали конфликтов», и я разверну список с подробностями.")
    return "\n".join(lines)
