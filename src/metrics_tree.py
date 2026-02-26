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
    line_index: int = -1
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

    # Compute absolute line offset of code block content
    before_code_text = sections[0] + "## Дерево" + after[:m.start()] + "```\n"
    _code_block_offset = before_code_text.count("\n")

    # Parse lines into nodes using a stack
    root: TreeNode | None = None
    stack: list[TreeNode] = []  # stack of (node) at each depth

    for local_idx, line in enumerate(lines):
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
            line_index=_code_block_offset + local_idx,
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


# ── Linkage path parser ─────────────────────────────────────────────────────

_ARROW_RE = re.compile(r'\s*(?:→|->|—>|=>)\s*')


def parse_linkage_path(linkage_section: str) -> list[str]:
    """Parse 'X → Y → ... → Extra Time' into ['X', 'Y', ..., 'Extra Time'].

    Returns the path from leaf to root (left to right as written).
    """
    if not linkage_section:
        return []

    for line in linkage_section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ARROW_RE.search(stripped):
            parts = _ARROW_RE.split(stripped)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 2:
                return parts

    return []


# ── Tree growth ──────────────────────────────────────────────────────────────


def _find_last_descendant_line(node: TreeNode) -> int:
    """Return the line_index of the last descendant (or the node itself)."""
    if not node.children:
        return node.line_index
    return max(_find_last_descendant_line(c) for c in node.children)


def _names_match(a: str, b: str) -> bool:
    """Case-insensitive name comparison with slugification for non-ASCII."""
    if a.lower() == b.lower():
        return True
    from src.router import _slugify
    sa = _slugify(a) if not a.isascii() else a.lower().replace(" ", "_")
    sb = _slugify(b) if not b.isascii() else b.lower().replace(" ", "_")
    return sa == sb


def _fix_last_to_middle(lines: list[str], node: TreeNode) -> None:
    """Change └── to ├── on node's line and fix descendant continuation marks."""
    line_idx = node.line_index
    depth = node.depth
    if depth == 0:
        return  # root has no branch prefix

    branch_pos = (depth - 1) * 4
    line = lines[line_idx]

    # Replace └ with ├
    if branch_pos < len(line) and line[branch_pos] == "└":
        lines[line_idx] = line[:branch_pos] + "├" + line[branch_pos + 1:]

    # Fix descendant lines: change "    " to "│   " at branch_pos
    last_desc_line = _find_last_descendant_line(node)
    for i in range(line_idx + 1, last_desc_line + 1):
        desc_line = lines[i]
        if len(desc_line) > branch_pos + 3:
            segment = desc_line[branch_pos:branch_pos + 4]
            if segment == "    ":
                lines[i] = desc_line[:branch_pos] + "│   " + desc_line[branch_pos + 4:]


def ensure_path_in_tree(tree_md: str, path_parts: list[str]) -> MetricsTreePatchResult:
    """Ensure all nodes in path_parts exist in the metrics tree.

    path_parts: leaf-to-root order, e.g. ["SLA", "Leads", "New Clients", "MAU", "Extra Time"]
    Returns MetricsTreePatchResult with updated tree text.
    """
    if not tree_md:
        return MetricsTreePatchResult(ok=False, changed=False, message="empty tree", new_text=tree_md or "")

    if len(path_parts) < 2:
        return MetricsTreePatchResult(ok=False, changed=False, message="path too short", new_text=tree_md)

    root = parse_tree(tree_md)
    if root is None:
        return MetricsTreePatchResult(ok=False, changed=False, message="cannot parse tree", new_text=tree_md)

    # Reverse to get root-to-leaf order
    path_rtl = list(reversed(path_parts))

    # Verify root matches
    if not _names_match(path_rtl[0], root.short_name):
        return MetricsTreePatchResult(
            ok=False, changed=False,
            message=f"root mismatch: tree='{root.short_name}', path='{path_rtl[0]}'",
            new_text=tree_md,
        )

    lines = tree_md.splitlines()
    changed = False
    current_node = root

    for i, part_name in enumerate(path_rtl[1:], 1):
        is_leaf = (i == len(path_rtl) - 1)

        # Search children of current_node
        found = None
        for child in current_node.children:
            if _names_match(part_name, child.short_name) or _names_match(part_name, child.name):
                found = child
                break

        if found is not None:
            current_node = found
            continue

        # Need to insert new node
        new_depth = current_node.depth + 1
        insert_after = _find_last_descendant_line(current_node)

        # Build prefix for new node
        if current_node.children:
            # Fix last child's └── to ├── and update descendant continuations
            last_child = current_node.children[-1]
            _fix_last_to_middle(lines, last_child)

            # Copy continuation prefix from existing sibling
            sibling_line = lines[last_child.line_index]
            cont_prefix = sibling_line[:(new_depth - 1) * 4]
            new_prefix = cont_prefix + "└── "
        else:
            # Build from parent
            if current_node.depth == 0:
                new_prefix = "└── "
            else:
                parent_line = lines[current_node.line_index]
                parent_branch_pos = (current_node.depth - 1) * 4
                parent_branch = parent_line[parent_branch_pos:parent_branch_pos + 1]
                parent_cont = parent_line[:parent_branch_pos]
                if parent_branch in ("├", "│"):
                    parent_cont_segment = "│   "
                else:
                    parent_cont_segment = "    "
                new_prefix = parent_cont + parent_cont_segment + "└── "

        # Build line content
        marker = " ← DATA CONTRACT ✅" if is_leaf else ""
        new_line = f"{new_prefix}{part_name}{marker}"

        # Insert line
        lines.insert(insert_after + 1, new_line)

        # Create TreeNode for the new node
        new_node = TreeNode(
            name=part_name,
            short_name=part_name,
            has_contract_marker=is_leaf,
            is_agreed=is_leaf,
            depth=new_depth,
            line_index=insert_after + 1,
        )
        new_node.parent = current_node
        current_node.children.append(new_node)
        current_node = new_node
        changed = True

    if not changed:
        return MetricsTreePatchResult(ok=True, changed=False, message="all nodes exist", new_text=tree_md)

    trailing = "\n" if tree_md.endswith("\n") else ""
    new_text = "\n".join(lines) + trailing
    return MetricsTreePatchResult(ok=True, changed=True, message="tree grown", new_text=new_text)
