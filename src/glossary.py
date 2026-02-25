from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class GlossaryIssue:
    canonical: str
    message: str


def _find_any(text: str, patterns: list[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    for p in patterns:
        if not p:
            continue
        if p.lower() in low:
            return True
    return False


def check_ambiguity(contract_md: str, glossary: dict | None) -> list[GlossaryIssue]:
    """Deterministic ambiguity checker based on context/glossary.json.

    v1: for any term that declares disambiguation groups, if the term (or its aliases)
    is present in the contract text, we require that at least one disambiguation
    group is also mentioned (via its keywords list). Otherwise we block save and ask.

    Glossary schema we support (subset):
    {
      "terms": [
        {
          "canonical": "Клиент",
          "aliases": ["клиент","customer"],
          "disambiguation": {
             "Юрлицо": ["юридическое лицо","юрлицо"],
             "Пользователь": ["пользователь","user"]
          }
        }
      ]
    }
    """

    if not glossary or not isinstance(glossary, dict):
        return []

    text = contract_md or ""
    low = text.lower()
    issues: list[GlossaryIssue] = []

    for t in glossary.get("terms", []) or []:
        if not isinstance(t, dict):
            continue
        canonical = (t.get("canonical") or "").strip()
        aliases = t.get("aliases") or []
        dis = t.get("disambiguation") or {}

        if not canonical or not dis or not isinstance(dis, dict):
            continue

        term_patterns = [canonical] + [a for a in aliases if isinstance(a, str)]
        if not _find_any(low, [p.lower() for p in term_patterns if p]):
            continue

        # collect disambiguation groups
        groups: list[tuple[str, list[str]]] = []
        for gname, kws in dis.items():
            if isinstance(kws, list):
                groups.append((str(gname), [str(x) for x in kws if isinstance(x, str)]))

        if not groups:
            continue

        any_group_mentioned = any(_find_any(low, [k.lower() for k in kws]) for _, kws in groups)
        if any_group_mentioned:
            continue

        # build question
        opts = "; ".join([name for name, _ in groups])
        msg = (
            f"Термин «{canonical}» выглядит неоднозначно. "
            f"Уточни, что именно имеется в виду: {opts}. "
            "После уточнения обновим контракт."
        )
        issues.append(GlossaryIssue(canonical=canonical, message=msg))

    return issues
