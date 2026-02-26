"""Tests for suggestion_engine.py — proactive contract suggestions."""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from src.memory import Memory
from src.suggestion_engine import (
    SuggestionCandidate,
    SuggestionEngine,
    _parse_circles,
    _resolve_stakeholders,
)

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

CIRCLES_MD = """\
# Круги компании

## Sales
- Ответственный: @ivan_sales
- Метрики: WIN NI, конверсия воронки, pipeline

## Product
- Ответственный: @maria_product
- Метрики: MAU, activation, feature adoption

## Customer Success
- Ответственный: @olga_cs
- Метрики: Churn Rate, REC, NPS
"""


@pytest.fixture
def data_dir(tmp_path):
    """Set up a temporary data directory with test files."""
    # metrics tree
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "metrics_tree.md").write_text(SAMPLE_TREE_MD, encoding="utf-8")
    (tmp_path / "context" / "circles.md").write_text(CIRCLES_MD, encoding="utf-8")

    # contracts index
    (tmp_path / "contracts").mkdir()
    index = {
        "contracts": [
            {"id": "contract_churn", "name": "Contract Churn", "status": "agreed"},
        ]
    }
    (tmp_path / "contracts" / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )

    # queue
    (tmp_path / "tasks").mkdir()
    queue = {
        "queue": [
            {"id": "win_ni", "priority": 1, "reason": "Расхождение 27.5 vs 31 млн"},
            {"id": "churn_rate", "priority": 2, "reason": "Sales и Product считают по-разному"},
        ]
    }
    (tmp_path / "tasks" / "queue.json").write_text(
        json.dumps(queue, ensure_ascii=False), encoding="utf-8"
    )

    # empty suggestions
    (tmp_path / "tasks" / "suggestions.json").write_text(
        '{"suggestions": []}', encoding="utf-8"
    )

    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


@pytest.fixture
def engine(memory):
    return SuggestionEngine(memory)


class TestParseCircles:
    def test_parses_circles(self):
        result = _parse_circles(CIRCLES_MD)
        assert result["Sales"] == "ivan_sales"
        assert result["Product"] == "maria_product"
        assert result["Customer Success"] == "olga_cs"

    def test_empty(self):
        assert _parse_circles("") == {}
        assert _parse_circles(None) == {}


class TestResolveStakeholders:
    def test_win_ni_matches_sales(self):
        result = _resolve_stakeholders("WIN NI", CIRCLES_MD)
        assert "ivan_sales" in result

    def test_churn_matches_cs(self):
        result = _resolve_stakeholders("Usage Churn", CIRCLES_MD)
        assert "olga_cs" in result

    def test_activation_matches_product(self):
        result = _resolve_stakeholders("Activation Rate", CIRCLES_MD)
        assert "maria_product" in result

    def test_no_match(self):
        result = _resolve_stakeholders("Unknown Metric XYZ", CIRCLES_MD)
        assert result == []


class TestSuggestAfterAgreement:
    def test_suggests_sibling(self, engine):
        candidates = engine.suggest_after_agreement("contract_churn")
        assert len(candidates) > 0
        names = [c.metric_name for c in candidates]
        assert "Usage Churn" in names

    def test_returns_max_2(self, engine):
        candidates = engine.suggest_after_agreement("contract_churn")
        assert len(candidates) <= 2

    def test_unknown_contract(self, engine):
        candidates = engine.suggest_after_agreement("nonexistent_contract")
        assert candidates == []

    def test_related_to_set(self, engine):
        candidates = engine.suggest_after_agreement("contract_churn")
        for c in candidates:
            assert c.related_to == "contract_churn"

    def test_has_tree_path(self, engine):
        candidates = engine.suggest_after_agreement("contract_churn")
        for c in candidates:
            assert "→" in c.tree_path
            assert "Extra Time" in c.tree_path


class TestCoverageScan:
    def test_returns_uncovered(self, engine):
        candidates = engine.coverage_scan()
        names = [c.metric_name for c in candidates]
        # Contract Churn is agreed + in index, but tree still has ✅ in our test
        # and also in index as "agreed" → should be excluded
        assert "Contract Churn" not in names

    def test_excludes_indexed(self, engine):
        """Contracts already in index with active status are excluded."""
        candidates = engine.coverage_scan()
        ids = [c.contract_id for c in candidates]
        assert "contract_churn" not in ids

    def test_has_priority_from_queue(self, engine):
        candidates = engine.coverage_scan()
        win_ni = [c for c in candidates if c.contract_id == "win_ni"]
        if win_ni:
            assert win_ni[0].priority == 1


class TestFilterAlreadySuggested:
    def test_filters_recently_suggested(self, engine, memory):
        now = datetime.now(timezone.utc)
        memory.save_suggestions([{
            "id": "sug_test",
            "contract_id": "usage_churn",
            "status": "suggested",
            "suggested_at": now.isoformat(),
        }])

        candidates = [
            SuggestionCandidate(
                contract_id="usage_churn",
                metric_name="Usage Churn",
                tree_path="test",
                priority=None,
                reason="test",
                stakeholders=[],
                related_to=None,
            ),
            SuggestionCandidate(
                contract_id="activation_rate",
                metric_name="Activation Rate",
                tree_path="test",
                priority=None,
                reason="test",
                stakeholders=[],
                related_to=None,
            ),
        ]

        filtered = engine.filter_already_suggested(candidates)
        ids = [c.contract_id for c in filtered]
        assert "usage_churn" not in ids
        assert "activation_rate" in ids

    def test_allows_after_cooldown(self, engine, memory):
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        memory.save_suggestions([{
            "id": "sug_old",
            "contract_id": "usage_churn",
            "status": "suggested",
            "suggested_at": old_date,
        }])

        candidates = [
            SuggestionCandidate(
                contract_id="usage_churn",
                metric_name="Usage Churn",
                tree_path="test",
                priority=None,
                reason="test",
                stakeholders=[],
                related_to=None,
            ),
        ]
        filtered = engine.filter_already_suggested(candidates)
        assert len(filtered) == 1

    def test_filters_dismissed(self, engine, memory):
        now = datetime.now(timezone.utc)
        memory.save_suggestions([{
            "id": "sug_dismissed",
            "contract_id": "usage_churn",
            "status": "dismissed",
            "suggested_at": now.isoformat(),
        }])

        candidates = [
            SuggestionCandidate(
                contract_id="usage_churn",
                metric_name="Usage Churn",
                tree_path="test",
                priority=None,
                reason="test",
                stakeholders=[],
                related_to=None,
            ),
        ]
        filtered = engine.filter_already_suggested(candidates)
        assert len(filtered) == 0

    def test_filters_active_in_index(self, engine, memory):
        # contract_churn is in index as "agreed"
        candidates = [
            SuggestionCandidate(
                contract_id="contract_churn",
                metric_name="Contract Churn",
                tree_path="test",
                priority=None,
                reason="test",
                stakeholders=[],
                related_to=None,
            ),
        ]
        filtered = engine.filter_already_suggested(candidates)
        assert len(filtered) == 0


class TestCanSuggestToday:
    def test_empty_suggestions(self, engine):
        assert engine.can_suggest_today() is True

    def test_one_today(self, engine, memory):
        now = datetime.now(timezone.utc)
        memory.save_suggestions([{
            "id": "sug_today",
            "contract_id": "test",
            "status": "suggested",
            "suggested_at": now.isoformat(),
        }])
        assert engine.can_suggest_today() is False

    def test_yesterday_ok(self, engine, memory):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        memory.save_suggestions([{
            "id": "sug_yesterday",
            "contract_id": "test",
            "status": "suggested",
            "suggested_at": yesterday,
        }])
        assert engine.can_suggest_today() is True


class TestRecordSuggestion:
    def test_records(self, engine, memory):
        candidates = [
            SuggestionCandidate(
                contract_id="usage_churn",
                metric_name="Usage Churn",
                tree_path="test path",
                priority=6,
                reason="test",
                stakeholders=["olga_cs"],
                related_to="contract_churn",
            ),
        ]
        engine.record_suggestion(candidates, "agreed:contract_churn", "post_123")

        saved = memory.get_suggestions()
        assert len(saved) == 1
        assert saved[0]["contract_id"] == "usage_churn"
        assert saved[0]["trigger"] == "agreed:contract_churn"
        assert saved[0]["thread_id"] == "post_123"
        assert saved[0]["status"] == "suggested"


class TestFormatSuggestionMessage:
    def test_format_single(self, engine):
        candidates = [
            SuggestionCandidate(
                contract_id="usage_churn",
                metric_name="Usage Churn",
                tree_path="Usage Churn → Retention → MAU → Extra Time",
                priority=6,
                reason="Связан с contract_churn",
                stakeholders=["olga_cs"],
                related_to="contract_churn",
            ),
        ]
        msg = engine.format_suggestion_message(candidates, "agreed:contract_churn")
        assert "Usage Churn" in msg
        assert "usage_churn" in msg
        assert "@olga_cs" in msg
        assert "начни контракт" in msg

    def test_format_empty(self, engine):
        assert engine.format_suggestion_message([], "test") == ""

    def test_format_coverage(self, engine):
        candidates = [
            SuggestionCandidate(
                contract_id="win_ni",
                metric_name="WIN NI",
                tree_path="WIN NI → New Clients → MAU",
                priority=1,
                reason="test",
                stakeholders=["ivan_sales"],
                related_to=None,
            ),
        ]
        msg = engine.format_coverage_message(candidates)
        assert "WIN NI" in msg
        assert "приоритет 1" in msg
        assert "@ivan_sales" in msg
