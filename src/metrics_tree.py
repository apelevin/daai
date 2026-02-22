import re
from dataclasses import dataclass


@dataclass
class MetricsTreePatchResult:
    ok: bool
    changed: bool
    message: str
    new_text: str


def mark_contract_agreed(tree_md: str, contract_name_or_id: str) -> MetricsTreePatchResult:
    """Mark a contract node as agreed (✅) in context/metrics_tree.md.

    Strategy (v1): find the first line that contains the contract name (case-insensitive)
    and either '← DATA CONTRACT' or looks like a contract row, and append ' ✅' if missing.

    We keep this deterministic and conservative to avoid accidental edits.
    """

    if not tree_md:
        return MetricsTreePatchResult(
            ok=False,
            changed=False,
            message="metrics_tree.md is empty",
            new_text=tree_md,
        )

    target = (contract_name_or_id or "").strip()
    if not target:
        return MetricsTreePatchResult(
            ok=False,
            changed=False,
            message="missing contract identifier",
            new_text=tree_md,
        )

    lines = tree_md.splitlines()

    # Prefer exact-ish match on a node line that indicates a contract
    # Example: "│   │   ├── WIN NI ... ← DATA CONTRACT"
    pat = re.compile(re.escape(target), re.IGNORECASE)

    def is_contract_line(s: str) -> bool:
        low = s.lower()
        return ("data contract" in low) or ("←" in s) or ("контракт" in low)

    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        if not is_contract_line(line):
            continue
        if "✅" in line:
            return MetricsTreePatchResult(
                ok=True,
                changed=False,
                message=f"Already marked ✅ for {target}",
                new_text=tree_md,
            )
        # Append checkmark
        lines[i] = line.rstrip() + " ✅"
        return MetricsTreePatchResult(
            ok=True,
            changed=True,
            message=f"Marked ✅ for {target}",
            new_text="\n".join(lines) + ("\n" if tree_md.endswith("\n") else ""),
        )

    return MetricsTreePatchResult(
        ok=False,
        changed=False,
        message=f"Could not find contract node for '{target}' in metrics_tree.md",
        new_text=tree_md,
    )
