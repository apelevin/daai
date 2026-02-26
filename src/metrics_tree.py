from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MetricsTreePatchResult:
    ok: bool
    changed: bool
    message: str
    new_text: str


# ── Tree parser ──────────────────────────────────────────────────────────────

@dataclass
class TreeNode:
    name: str                          # "WIN NI (New Income от новых клиентов)"
    short_name: str                    # "WIN NI"
    has_contract_marker: bool          # True if "← DATA CONTRACT"
    is_agreed: bool                    # True if "✅"
    depth: int
    children: list[TreeNode] = field(default_factory=list)
    parent: TreeNode | None = field(default=None, repr=False)


# Box-drawing prefixes: "├── ", "└── ", "│   ", "    "
_BRANCH_RE = re.compile(r"^([│├└ ─]*?)([├└]──\s+|$)")
_CONTRACT_MARKER = "← DATA CONTRACT"


def _parse_depth(line: str) -> tuple[int, str]:
    """Return (depth, cleaned_name) from a tree line with box-drawing chars."""
    # Count depth units.  Each unit is 4 chars: "│   " or "├── " or "└── " or "    "
    stripped = line.rstrip()
    if not stripped:
        return 0, ""

    depth = 0
    i = 0
    while i < len(stripped):
        chunk = stripped[i:i+4]
        if chunk in ("│   ", "    "):
            depth += 1
            i += 4
        elif len(chunk) >= 4 and chunk[0] in ("├", "└"):
            # "├── " or "└── "
            depth += 1
            i += 4
            break
        else:
            break

    name = stripped[i:].strip()
    return depth, name


def _extract_short_name(name: str) -> str:
    """Extract short name: text before first '(' or the full name."""
    m = re.match(r"^([^(]+)", name)
    if m:
        return m.group(1).strip().rstrip("←").strip()
    return name.strip()


def parse_tree(tree_md: str) -> TreeNode | None:
    """Parse metrics_tree.md markdown → TreeNode hierarchy.

    Looks for the code block under '## Дерево' and parses box-drawing lines.
    """
    if not tree_md:
        return None

    # Find the code block that follows "## Дерево"
    sections = tree_md.split("## Дерево")
    if len(sections) < 2:
        return None
    after = sections[1]
    m = re.search(r"```\n(.*?)```", after, re.DOTALL)
    if not m:
        return None
    lines = m.group(1).splitlines()

    if not lines:
        return None

    # Parse lines into nodes using a stack
    root: TreeNode | None = None
    stack: list[TreeNode] = []  # stack of (node) at each depth

    for line in lines:
        depth, raw_name = _parse_depth(line)
        if not raw_name:
            continue

        has_marker = _CONTRACT_MARKER in raw_name
        is_agreed = "✅" in raw_name

        # Clean name: remove marker and checkmark
        clean_name = raw_name.replace(_CONTRACT_MARKER, "").replace("✅", "").strip()
        short_name = _extract_short_name(clean_name)

        node = TreeNode(
            name=clean_name,
            short_name=short_name,
            has_contract_marker=has_marker,
            is_agreed=is_agreed,
            depth=depth,
        )

        if depth == 0:
            root = node
            stack = [node]
        else:
            # Find parent: the most recent node at depth-1
            while len(stack) > depth:
                stack.pop()
            if stack:
                parent = stack[-1]
                parent.children.append(node)
                node.parent = parent
            # Push this node onto the stack
            if len(stack) == depth:
                stack.append(node)
            else:
                stack[depth] = node

    return root


def get_uncovered_nodes(root: TreeNode) -> list[TreeNode]:
    """Return nodes with has_contract_marker=True and is_agreed=False."""
    result: list[TreeNode] = []

    def _walk(node: TreeNode) -> None:
        if node.has_contract_marker and not node.is_agreed:
            result.append(node)
        for child in node.children:
            _walk(child)

    if root:
        _walk(root)
    return result


def get_path_to_root(node: TreeNode) -> str:
    """Return path like 'WIN NI → New Clients → MAU → Extra Time'."""
    parts: list[str] = []
    cur: TreeNode | None = node
    while cur is not None:
        parts.append(cur.short_name)
        cur = cur.parent
    return " → ".join(parts)


def get_siblings(node: TreeNode) -> list[TreeNode]:
    """Return sibling nodes (same parent, excluding self)."""
    if node.parent is None:
        return []
    return [c for c in node.parent.children if c is not node]


def find_node_by_id(root: TreeNode, contract_id: str) -> TreeNode | None:
    """Find a node by matching short_name against contract_id (slugified comparison)."""
    from src.router import _slugify

    target = _slugify(contract_id) if not contract_id.isascii() else contract_id.lower().replace(" ", "_")

    def _walk(node: TreeNode) -> TreeNode | None:
        slug = _slugify(node.short_name) if not node.short_name.isascii() else node.short_name.lower().replace(" ", "_")
        if slug == target or node.short_name.lower() == contract_id.lower():
            return node
        # Also try matching by name without parenthetical
        name_slug = _slugify(node.name) if not node.name.isascii() else node.name.lower().replace(" ", "_")
        if name_slug == target:
            return node
        for child in node.children:
            found = _walk(child)
            if found:
                return found
        return None

    return _walk(root) if root else None


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
