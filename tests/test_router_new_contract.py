"""Tests for new_contract_init: _slugify, load_files sanitization, entity normalization."""

from __future__ import annotations

import unittest

from src.router import route, _slugify


class FakeLLM:
    def __init__(self, raw: str):
        self.raw = raw

    def call_cheap(self, system, user, **kw):
        return self.raw


class FakeMemory:
    def read_file(self, path: str):
        return "{}"


class SlugifyTest(unittest.TestCase):
    def test_cyrillic_basic(self):
        self.assertEqual(_slugify("Количество сотрудников"), "kolichestvo_sotrudnikov")

    def test_already_ascii(self):
        self.assertEqual(_slugify("employee_count"), "employee_count")

    def test_mixed_cyrillic_ascii(self):
        slug = _slugify("MAU по продукту")
        self.assertEqual(slug, "mau_po_produktu")

    def test_trims_length(self):
        long_text = "а" * 100
        self.assertLessEqual(len(_slugify(long_text)), 60)

    def test_special_chars_stripped(self):
        self.assertEqual(_slugify("тест!@#$%^&*()"), "test")

    def test_multiple_spaces_collapsed(self):
        self.assertEqual(_slugify("один   два   три"), "odin_dva_tri")

    def test_empty_string(self):
        self.assertEqual(_slugify(""), "")

    def test_hyphens_and_underscores(self):
        self.assertEqual(_slugify("клиент-сегмент_тип"), "klient_segment_tip")


class NewContractInitRouteTest(unittest.TestCase):
    def _route_new_contract(self, entity: str | None, load_files: list | None = None):
        """Helper: simulate LLM returning new_contract_init."""
        payload = {
            "type": "new_contract_init",
            "entity": entity,
            "load_files": load_files or ["drafts/some_other.md", "contracts/old.md"],
            "model": "heavy",
        }
        import json
        llm = FakeLLM(json.dumps(payload))
        mem = FakeMemory()
        return route(llm, mem, "testuser", "новый контракт", "channel", None)

    def test_load_files_sanitized(self):
        """new_contract_init must not carry foreign contract/draft files."""
        result = self._route_new_contract("employee_count", ["drafts/other.md", "contracts/old.md"])
        self.assertEqual(result["load_files"], ["context/company.md", "context/metrics_tree.md"])

    def test_cyrillic_entity_normalized(self):
        result = self._route_new_contract("Количество сотрудников")
        self.assertTrue(result["entity"].isascii(), f"Entity not ASCII: {result['entity']}")
        self.assertEqual(result["entity"], "kolichestvo_sotrudnikov")

    def test_ascii_entity_preserved(self):
        result = self._route_new_contract("employee_count")
        self.assertEqual(result["entity"], "employee_count")

    def test_none_entity_preserved(self):
        result = self._route_new_contract(None)
        self.assertIsNone(result["entity"])

    def test_model_is_heavy(self):
        result = self._route_new_contract("test")
        self.assertEqual(result["model"], "heavy")


if __name__ == "__main__":
    unittest.main()
