"""Tests for contract_summary.py — deterministic summary extraction + prompt formatting."""

import os
import pytest

from src.contract_summary import generate_summary, format_summaries_for_prompt, _snippet


FULL_CONTRACT = """# Data Contract: Revenue per Client

## Определение

Выручка от клиента за отчётный период, включая все продукты.

## Формула

SUM(invoice_amount) WHERE client_id = X AND period = Y

## Источник данных

billing.invoices (PostgreSQL, обновление ежедневно)

## Связь с Extra Time

Revenue > Client Metrics > Revenue per Client
"""

MINIMAL_DRAFT = """# Data Contract: New Metric

## Определение

(уточняется)
"""


class TestSnippet:
    def test_short_text(self):
        assert _snippet("hello world", 120) == "hello world"

    def test_long_text_truncated(self):
        long_text = "A" * 200
        result = _snippet(long_text, 120)
        assert len(result) == 120
        assert result.endswith("…")

    def test_multiline_takes_first(self):
        text = "first line\nsecond line\nthird line"
        assert _snippet(text, 120) == "first line"

    def test_empty_text(self):
        assert _snippet("", 120) == ""
        assert _snippet(None, 120) == ""

    def test_blank_lines_skipped(self):
        text = "\n\n  \nactual content"
        assert _snippet(text, 120) == "actual content"


class TestExtractName:
    def test_standard_header(self):
        s = generate_summary("rev_001", "# Data Contract: Revenue per Client\n\n## Определение\ntest", "agreed")
        assert s["name"] == "Revenue per Client"

    def test_missing_header_falls_back_to_id(self):
        s = generate_summary("rev_001", "## Определение\nsome text", "draft")
        assert s["name"] == "rev_001"


class TestGenerateSummary:
    def test_full_contract(self):
        s = generate_summary("rev_001", FULL_CONTRACT, "agreed")
        assert s["id"] == "rev_001"
        assert s["name"] == "Revenue per Client"
        assert s["status"] == "agreed"
        assert "Выручка" in s["definition"]
        assert "SUM" in s["formula"]
        assert "billing" in s["data_source"]
        assert "Revenue" in s["extra_time_path"]

    def test_minimal_draft(self):
        s = generate_summary("new_001", MINIMAL_DRAFT, "draft")
        assert s["id"] == "new_001"
        assert s["name"] == "New Metric"
        assert s["status"] == "draft"
        assert s["definition"] == "(уточняется)"
        assert s["formula"] == ""

    def test_empty_markdown(self):
        s = generate_summary("empty_001", "", "draft")
        assert s["name"] == "empty_001"
        assert s["definition"] == ""
        assert s["formula"] == ""


class TestFormatSummaries:
    def test_empty_returns_empty_string(self):
        assert format_summaries_for_prompt({}) == ""

    def test_grouped_by_status(self):
        summaries = {
            "draft_001": {"id": "draft_001", "name": "Draft", "status": "draft", "definition": "d", "formula": "", "data_source": "", "extra_time_path": ""},
            "agreed_001": {"id": "agreed_001", "name": "Agreed", "status": "agreed", "definition": "a", "formula": "f", "data_source": "", "extra_time_path": "ET"},
            "review_001": {"id": "review_001", "name": "Review", "status": "in_review", "definition": "r", "formula": "", "data_source": "", "extra_time_path": ""},
        }
        result = format_summaries_for_prompt(summaries)
        # agreed should come before in_review, in_review before draft
        pos_agreed = result.index("Согласованные")
        pos_review = result.index("На ревью")
        pos_draft = result.index("Черновики")
        assert pos_agreed < pos_review < pos_draft

    def test_contains_instructions(self):
        summaries = {
            "x": {"id": "x", "name": "X", "status": "agreed", "definition": "def", "formula": "", "data_source": "", "extra_time_path": ""},
        }
        result = format_summaries_for_prompt(summaries)
        assert "Ландшафт контрактов" in result
        assert "read_contract" in result
        assert "НЕ дублировать" in result

    def test_summary_line_format(self):
        summaries = {
            "m1": {"id": "m1", "name": "Metric One", "status": "agreed", "definition": "def1", "formula": "SUM(x)", "data_source": "", "extra_time_path": "A > B"},
        }
        result = format_summaries_for_prompt(summaries)
        assert "`m1` — Metric One" in result
        assert "Опр: def1" in result
        assert "Формула: SUM(x)" in result
        assert "ET: A > B" in result


class TestMemoryRoundtrip:
    def test_get_save_update(self, tmp_path):
        from src.memory import Memory
        m = Memory()
        m.base_dir = str(tmp_path)
        os.makedirs(tmp_path / "contracts", exist_ok=True)

        # Initially empty
        assert m.get_summaries() == {}

        # Save
        m.save_summaries({"a": {"id": "a", "name": "A"}})
        assert m.get_summaries() == {"a": {"id": "a", "name": "A"}}

        # Update adds new entry
        m.update_summary("b", {"id": "b", "name": "B"})
        result = m.get_summaries()
        assert "a" in result
        assert "b" in result
        assert result["b"]["name"] == "B"

        # Update overwrites existing entry
        m.update_summary("a", {"id": "a", "name": "A2"})
        assert m.get_summaries()["a"]["name"] == "A2"
