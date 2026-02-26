"""Tests for analyzer.py — conflict detection, cycle detection, Jaccard similarity."""

import json
import pytest

from src.memory import Memory
from src.analyzer import (
    MetricsAnalyzer,
    Conflict,
    render_conflicts,
    _extract_sections,
    _normalize_name,
    _tokenize_definition,
    _jaccard,
    _extract_related_contract_ids,
)


# ── Helper tests ─────────────────────────────────────────────────────────────

class TestExtractSections:
    def test_basic(self):
        md = "# Title\n## Формула\nA / B\n## Определение\nSomething\n"
        sections = _extract_sections(md)
        assert "Формула" in sections
        assert sections["Формула"] == "A / B"
        assert "Определение" in sections

    def test_empty(self):
        assert _extract_sections("") == {}
        assert _extract_sections(None) == {}

    def test_no_h2(self):
        assert _extract_sections("# Only H1\nSome text") == {}


class TestNormalizeName:
    def test_basic(self):
        assert _normalize_name("WIN NI") == "win ni"
        assert _normalize_name("Contract-Churn") == "contract churn"
        assert _normalize_name("") == ""

    def test_special_chars(self):
        assert _normalize_name("A/B Test") == "a b test"


class TestTokenizeDefinition:
    def test_basic(self):
        tokens = _tokenize_definition("Количество клиентов, которые ушли за месяц")
        assert "клиентов" in tokens
        assert "которые" in tokens
        assert "ушли" in tokens
        # short words filtered
        assert "за" not in tokens

    def test_stop_words(self):
        tokens = _tokenize_definition("и в на по из для что это")
        assert len(tokens) == 0

    def test_empty(self):
        assert _tokenize_definition("") == set()
        assert _tokenize_definition(None) == set()


class TestJaccard:
    def test_identical(self):
        s = {"a", "b", "c"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint(self):
        assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial(self):
        # {a, b, c} & {b, c, d} = {b, c}, union = {a, b, c, d}
        assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 2 / 4

    def test_empty(self):
        assert _jaccard(set(), set()) == 1.0
        assert _jaccard({"a"}, set()) == 0.0


class TestExtractRelatedContractIds:
    def test_basic(self):
        md = "## Связанные контракты\n- win_ni\n- churn_rate\n"
        ids = _extract_related_contract_ids(md)
        assert ids == ["win_ni", "churn_rate"]

    def test_empty(self):
        assert _extract_related_contract_ids("## Формула\nA / B") == []

    def test_with_description(self):
        md = "## Связанные контракты\n- win_ni (зависимость)\n"
        ids = _extract_related_contract_ids(md)
        assert ids == ["win_ni"]


# ── Analyzer integration tests ───────────────────────────────────────────────

VALID_CONTRACT = """\
# Data Contract: Test Metric

## Определение
Количество тестовых метрик за период.

## Формула
count(test_events) where period = month

## Источник данных
PostgreSQL: analytics.test_events

## Связь с Extra Time
Test Metric → MAU → Extra Time
"""

CONTRACT_NO_FORMULA = """\
# Data Contract: Bad Metric

## Определение
Количество чего-то.

## Источник данных
PostgreSQL: db.table

## Связь с Extra Time
Bad Metric → Extra Time
"""

CONTRACT_NO_LINKAGE = """\
# Data Contract: No Link

## Определение
Определение.

## Формула
count(x)

## Источник данных
PostgreSQL: db.table
"""

CONTRACT_AMBIGUOUS_FORMULA = """\
# Data Contract: Fuzzy Metric

## Определение
Определение метрики.

## Формула
примерно 80% от общего count

## Источник данных
PostgreSQL: db.table

## Связь с Extra Time
Fuzzy Metric → MAU → Extra Time
"""


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text(
        json.dumps({"contracts": []}, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


def _setup_contract(memory, cid, content, status="agreed"):
    """Write a contract and add to index."""
    memory.write_file(f"contracts/{cid}.md", content)
    memory.update_contract_index(cid, {"name": cid, "status": status})


class TestDetectConflicts:
    def test_valid_contract_no_conflicts(self, memory):
        _setup_contract(memory, "test_metric", VALID_CONTRACT)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        # Valid contract should have no high-severity issues
        high = [c for c in conflicts if c.severity == "high"]
        assert len(high) == 0

    def test_missing_formula(self, memory):
        _setup_contract(memory, "bad_metric", CONTRACT_NO_FORMULA)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        types = [c.type for c in conflicts]
        assert "missing_formula" in types

    def test_missing_linkage(self, memory):
        _setup_contract(memory, "no_link", CONTRACT_NO_LINKAGE)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        types = [c.type for c in conflicts]
        assert "missing_extra_time_linkage" in types

    def test_ambiguous_formula(self, memory):
        _setup_contract(memory, "fuzzy_metric", CONTRACT_AMBIGUOUS_FORMULA)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        types = [c.type for c in conflicts]
        assert "ambiguous_formula" in types

    def test_self_reference(self, memory):
        contract = VALID_CONTRACT + "\n## Связанные контракты\n- self_ref\n"
        _setup_contract(memory, "self_ref", contract)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        types = [c.type for c in conflicts]
        assert "self_related_reference" in types

    def test_cyclic_dependency(self, memory):
        contract_a = VALID_CONTRACT + "\n## Связанные контракты\n- contract_b\n"
        contract_b = VALID_CONTRACT.replace("Test Metric", "Contract B") + "\n## Связанные контракты\n- contract_a\n"
        _setup_contract(memory, "contract_a", contract_a)
        _setup_contract(memory, "contract_b", contract_b)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        types = [c.type for c in conflicts]
        assert "cyclic_dependency" in types

    def test_unknown_related_contract(self, memory):
        contract = VALID_CONTRACT + "\n## Связанные контракты\n- nonexistent_id\n"
        _setup_contract(memory, "test_metric", contract)
        analyzer = MetricsAnalyzer(memory)
        conflicts = analyzer.detect_conflicts()
        types = [c.type for c in conflicts]
        assert "unknown_related_contract" in types

    def test_only_contract_ids_filter(self, memory):
        _setup_contract(memory, "test_metric", VALID_CONTRACT)
        _setup_contract(memory, "bad_metric", CONTRACT_NO_FORMULA)
        analyzer = MetricsAnalyzer(memory)
        # Only check test_metric
        conflicts = analyzer.detect_conflicts(only_contract_ids=["test_metric"])
        contract_ids = set()
        for c in conflicts:
            contract_ids.update(c.contracts)
        assert "bad_metric" not in contract_ids


class TestRenderConflicts:
    def test_no_conflicts(self):
        assert "Конфликтов не найдено" in render_conflicts([])

    def test_renders_conflicts(self):
        conflicts = [
            Conflict(
                type="missing_formula",
                severity="high",
                title="Нет формулы: Test",
                details="Секция пустая.",
                contracts=["test"],
            ),
        ]
        result = render_conflicts(conflicts)
        assert "Нет формулы" in result
        assert "test" in result
        assert "найдено проблем: 1" in result

    def test_cross_contract_conflict(self):
        conflicts = [
            Conflict(
                type="cyclic_dependency",
                severity="high",
                title="Цикл: A ↔ B",
                details="Цикл.",
                contracts=["a", "b"],
            ),
        ]
        result = render_conflicts(conflicts)
        assert "Межконтрактные" in result
