"""Tests for metrics_tree.py — tree parser, traversal helpers."""

import pytest

from src.metrics_tree import (
    TreeNode,
    parse_tree,
    get_uncovered_nodes,
    get_path_to_root,
    get_siblings,
    find_node_by_id,
    mark_contract_agreed,
)

# Minimal tree markdown for tests
SAMPLE_TREE_MD = """\
# Дерево метрик

## Дерево

```
Extra Time
├── MAU (Monthly Active Users)
│   ├── New Clients (acquisition)
│   │   ├── WIN NI (New Income от новых клиентов) ← DATA CONTRACT
│   │   └── WIN REC (Recurring от новых клиентов) ← DATA CONTRACT
│   ├── Retention (не уходят)
│   │   ├── Contract Churn (непродление контракта) ← DATA CONTRACT ✅
│   │   └── Usage Churn (падение MAU ниже порога) ← DATA CONTRACT
│   └── Activation (начинают пользоваться)
│       └── Activation Rate (% активированных лицензий) ← DATA CONTRACT
├── Jobs per User (задач на пользователя)
│   └── Adoption (используют больше)
└── Revenue (следствие Extra Time)
    ├── New Income (NI) ← DATA CONTRACT
    └── Recurring Income (REC) ← DATA CONTRACT
```
"""


class TestParseTree:
    def test_parse_root(self):
        root = parse_tree(SAMPLE_TREE_MD)
        assert root is not None
        assert root.short_name == "Extra Time"
        assert root.depth == 0
        assert root.parent is None

    def test_top_level_children(self):
        root = parse_tree(SAMPLE_TREE_MD)
        names = [c.short_name for c in root.children]
        assert "MAU" in names
        assert "Jobs per User" in names
        assert "Revenue" in names

    def test_contract_markers(self):
        root = parse_tree(SAMPLE_TREE_MD)
        # WIN NI should have contract marker
        mau = [c for c in root.children if c.short_name == "MAU"][0]
        new_clients = [c for c in mau.children if "New Clients" in c.short_name][0]
        win_ni = [c for c in new_clients.children if "WIN NI" in c.short_name][0]
        assert win_ni.has_contract_marker is True
        assert win_ni.is_agreed is False

    def test_agreed_marker(self):
        root = parse_tree(SAMPLE_TREE_MD)
        mau = [c for c in root.children if c.short_name == "MAU"][0]
        retention = [c for c in mau.children if "Retention" in c.short_name][0]
        churn = [c for c in retention.children if "Contract Churn" in c.short_name][0]
        assert churn.has_contract_marker is True
        assert churn.is_agreed is True

    def test_non_contract_node(self):
        root = parse_tree(SAMPLE_TREE_MD)
        mau = [c for c in root.children if c.short_name == "MAU"][0]
        assert mau.has_contract_marker is False

    def test_empty_input(self):
        assert parse_tree("") is None
        assert parse_tree(None) is None

    def test_no_tree_section(self):
        assert parse_tree("# Just a heading\nSome text") is None

    def test_depth_correct(self):
        root = parse_tree(SAMPLE_TREE_MD)
        assert root.depth == 0
        mau = [c for c in root.children if c.short_name == "MAU"][0]
        assert mau.depth == 1
        new_clients = [c for c in mau.children if "New Clients" in c.short_name][0]
        assert new_clients.depth == 2
        win_ni = [c for c in new_clients.children if "WIN NI" in c.short_name][0]
        assert win_ni.depth == 3


class TestGetUncoveredNodes:
    def test_returns_uncovered(self):
        root = parse_tree(SAMPLE_TREE_MD)
        uncovered = get_uncovered_nodes(root)
        names = [n.short_name for n in uncovered]
        assert "WIN NI" in names
        assert "WIN REC" in names
        assert "Usage Churn" in names
        assert "Activation Rate" in names
        assert "New Income" in names
        assert "Recurring Income" in names

    def test_excludes_agreed(self):
        root = parse_tree(SAMPLE_TREE_MD)
        uncovered = get_uncovered_nodes(root)
        names = [n.short_name for n in uncovered]
        assert "Contract Churn" not in names

    def test_count(self):
        root = parse_tree(SAMPLE_TREE_MD)
        uncovered = get_uncovered_nodes(root)
        # WIN NI, WIN REC, Usage Churn, Activation Rate, New Income, Recurring Income
        assert len(uncovered) == 6


class TestGetPathToRoot:
    def test_leaf_path(self):
        root = parse_tree(SAMPLE_TREE_MD)
        mau = [c for c in root.children if c.short_name == "MAU"][0]
        new_clients = [c for c in mau.children if "New Clients" in c.short_name][0]
        win_ni = [c for c in new_clients.children if "WIN NI" in c.short_name][0]
        path = get_path_to_root(win_ni)
        assert "WIN NI" in path
        assert "New Clients" in path
        assert "MAU" in path
        assert "Extra Time" in path
        assert path.startswith("WIN NI")

    def test_root_path(self):
        root = parse_tree(SAMPLE_TREE_MD)
        assert get_path_to_root(root) == "Extra Time"


class TestGetSiblings:
    def test_siblings(self):
        root = parse_tree(SAMPLE_TREE_MD)
        mau = [c for c in root.children if c.short_name == "MAU"][0]
        retention = [c for c in mau.children if "Retention" in c.short_name][0]
        contract_churn = [c for c in retention.children if "Contract Churn" in c.short_name][0]
        sibs = get_siblings(contract_churn)
        sib_names = [s.short_name for s in sibs]
        assert "Usage Churn" in sib_names
        assert "Contract Churn" not in sib_names

    def test_root_has_no_siblings(self):
        root = parse_tree(SAMPLE_TREE_MD)
        assert get_siblings(root) == []


class TestFindNodeById:
    def test_find_by_slug(self):
        root = parse_tree(SAMPLE_TREE_MD)
        node = find_node_by_id(root, "contract_churn")
        assert node is not None
        assert "Contract Churn" in node.short_name

    def test_find_by_name(self):
        root = parse_tree(SAMPLE_TREE_MD)
        node = find_node_by_id(root, "WIN NI")
        assert node is not None
        assert node.has_contract_marker is True

    def test_not_found(self):
        root = parse_tree(SAMPLE_TREE_MD)
        assert find_node_by_id(root, "nonexistent_metric") is None

    def test_none_root(self):
        assert find_node_by_id(None, "win_ni") is None


class TestMarkContractAgreed:
    """Existing mark_contract_agreed still works after refactor."""

    def test_marks_checkmark(self):
        md = "│   ├── WIN NI (test) ← DATA CONTRACT\n"
        result = mark_contract_agreed(md, "WIN NI")
        assert result.ok
        assert result.changed
        assert "✅" in result.new_text

    def test_already_marked(self):
        md = "│   ├── Contract Churn ← DATA CONTRACT ✅\n"
        result = mark_contract_agreed(md, "Contract Churn")
        assert result.ok
        assert not result.changed
