"""Tests for ToolExecutor handlers."""

import json
import os
import tempfile
import unittest

from src.memory import Memory
from src.tools import ToolExecutor


VALID_CONTRACT_MD = """# Data Contract: Test Metric

## Статус
Согласован

## Определение
Тестовая метрика для юнит-тестов.

## Формула
Человеческая: количество тестов / общее количество.

Псевдо-SQL: SELECT COUNT(*) FROM tests WHERE status = 'pass';

## Источник данных
CI/CD pipeline, поле test_results.

## Включает
Все автоматические тесты.

## Исключения
Ручные тесты не входят.

## Гранулярность
Ежедневно, per commit.

## Ответственный за данные
@testlead

## Ответственный за расчёт
@devops

## Связь с Extra Time
Test Coverage → Code Quality → Extra Time

## Потребители
Engineering, QA

## Состояние данных
Данные есть, качество подтверждено: CI pipeline.

## Известные проблемы
Flaky tests не учитываются.

## Связанные контракты
- code_quality

## Согласовано
@testlead — 2026-02-25
@devops — 2026-02-25

## История изменений
2026-02-25 — создан
"""

INVALID_CONTRACT_MD = """# Data Contract: Broken

## Статус
Черновик

## Определение
Что-то.
"""


class FakeMM:
    def resolve_username(self, raw):
        if raw == "Никита":
            return "korabovtsev"
        return None


class ToolExecutorReadTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.tmpdir
        self.mem = Memory()
        self.executor = ToolExecutor(self.mem)

    def test_read_contract_found(self):
        self.mem.write_file("contracts/test.md", "# Test")
        result = self.executor.execute("read_contract", {"contract_id": "test"})
        self.assertEqual(result["contract_id"], "test")
        self.assertIn("# Test", result["content"])

    def test_read_contract_not_found(self):
        result = self.executor.execute("read_contract", {"contract_id": "nonexistent"})
        self.assertIn("error", result)

    def test_read_draft_found(self):
        self.mem.write_file("drafts/test.md", "# Draft")
        result = self.executor.execute("read_draft", {"contract_id": "test"})
        self.assertEqual(result["content"], "# Draft")

    def test_read_draft_not_found(self):
        result = self.executor.execute("read_draft", {"contract_id": "nope"})
        self.assertIn("error", result)

    def test_read_discussion(self):
        self.mem.write_json("drafts/test_discussion.json", {"status": "open", "positions": {}})
        result = self.executor.execute("read_discussion", {"contract_id": "test"})
        self.assertEqual(result["discussion"]["status"], "open")

    def test_read_roles(self):
        self.mem.write_json("context/roles.json", {"roles": {"data_lead": ["alice"]}})
        self.mem.write_json("tasks/roles.json", {"roles": {"circle_lead": ["bob"]}})
        result = self.executor.execute("read_roles", {})
        self.assertIn("alice", result["roles"]["data_lead"])
        self.assertIn("bob", result["roles"]["circle_lead"])

    def test_validate_contract_ok(self):
        result = self.executor.execute("validate_contract", {"contract_md": VALID_CONTRACT_MD})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["issues"]), 0)

    def test_validate_contract_invalid(self):
        result = self.executor.execute("validate_contract", {"contract_md": INVALID_CONTRACT_MD})
        self.assertFalse(result["ok"])
        self.assertGreater(len(result["issues"]), 0)

    def test_list_contracts(self):
        self.mem.write_json("contracts/index.json", {
            "contracts": [
                {"id": "a", "name": "A", "status": "active"},
                {"id": "b", "name": "B", "status": "draft"},
            ]
        })
        result = self.executor.execute("list_contracts", {})
        self.assertEqual(len(result["contracts"]), 2)

    def test_unknown_tool(self):
        result = self.executor.execute("nonexistent_tool", {})
        self.assertIn("error", result)


class ToolExecutorWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.tmpdir
        self.mem = Memory()
        self.executor = ToolExecutor(self.mem, FakeMM())

    def test_save_contract_valid(self):
        self.mem.write_json("contracts/index.json", {"contracts": []})
        result = self.executor.execute("save_contract", {
            "contract_id": "test_metric",
            "content": VALID_CONTRACT_MD,
        })
        self.assertTrue(result["success"], f"Expected success, got: {result}")
        self.assertEqual(result["contract_id"], "test_metric")
        # Verify file exists
        md = self.mem.get_contract("test_metric")
        self.assertIsNotNone(md)
        # Verify index updated
        idx = self.mem.read_json("contracts/index.json")
        rec = [c for c in idx["contracts"] if c["id"] == "test_metric"][0]
        self.assertEqual(rec["status"], "agreed")

    def test_save_contract_invalid(self):
        self.mem.write_json("contracts/index.json", {"contracts": []})
        result = self.executor.execute("save_contract", {
            "contract_id": "broken",
            "content": INVALID_CONTRACT_MD,
        })
        self.assertFalse(result["success"])
        self.assertGreater(len(result["errors"]), 0)
        # Contract should NOT be saved
        md = self.mem.get_contract("broken")
        self.assertIsNone(md)

    def test_save_contract_governance_failure(self):
        self.mem.write_json("contracts/index.json", {
            "contracts": [{"id": "test_metric", "tier": "tier_2"}]
        })
        self.mem.write_json("context/governance.json", {
            "tiers": {
                "tier_2": {
                    "approval_required": ["data_lead", "circle_lead"],
                    "consensus_threshold": 1.0,
                }
            }
        })
        # No roles assigned — governance should fail
        result = self.executor.execute("save_contract", {
            "contract_id": "test_metric",
            "content": VALID_CONTRACT_MD,
        })
        self.assertFalse(result["success"])
        has_governance_error = any("Governance" in e or "роле" in e for e in result["errors"])
        self.assertTrue(has_governance_error, f"Expected governance error in: {result['errors']}")

    def test_save_draft(self):
        self.mem.write_json("contracts/index.json", {"contracts": []})
        result = self.executor.execute("save_draft", {
            "contract_id": "my_draft",
            "content": "# Draft content",
        })
        self.assertTrue(result["success"])
        md = self.mem.get_draft("my_draft")
        self.assertEqual(md, "# Draft content")

    def test_update_discussion(self):
        result = self.executor.execute("update_discussion", {
            "contract_id": "test",
            "discussion": {"status": "in_progress", "positions": {"alice": "agree"}},
        })
        self.assertTrue(result["success"])
        data = self.mem.get_discussion("test")
        self.assertEqual(data["status"], "in_progress")

    def test_add_reminder(self):
        self.mem.write_json("tasks/reminders.json", {"reminders": []})
        result = self.executor.execute("add_reminder", {
            "reminder": {"id": "rem_1", "contract_id": "test", "target_user": "alice"},
        })
        self.assertTrue(result["success"])
        reminders = self.mem.get_reminders()
        self.assertEqual(len(reminders), 1)

    def test_update_participant(self):
        result = self.executor.execute("update_participant", {
            "username": "alice",
            "content": "# Alice\nData engineer",
        })
        self.assertTrue(result["success"])
        profile = self.mem.get_participant("alice")
        self.assertIn("Data engineer", profile)

    def test_save_decision(self):
        result = self.executor.execute("save_decision", {
            "decision": {"contract": "test", "decision": "approved", "agreed_by": ["alice"]},
        })
        self.assertTrue(result["success"])

    def test_assign_role(self):
        self.mem.write_json("tasks/roles.json", {"roles": {}})
        result = self.executor.execute("assign_role", {
            "role": "data_lead",
            "username": "alice",
        })
        self.assertTrue(result["success"])
        roles = self.mem.read_json("tasks/roles.json")
        self.assertIn("alice", roles["roles"]["data_lead"])

    def test_assign_role_dedup(self):
        self.mem.write_json("tasks/roles.json", {"roles": {"data_lead": ["alice"]}})
        result = self.executor.execute("assign_role", {
            "role": "data_lead",
            "username": "alice",
        })
        self.assertTrue(result["success"])
        roles = self.mem.read_json("tasks/roles.json")
        self.assertEqual(roles["roles"]["data_lead"].count("alice"), 1)

    def test_set_contract_status(self):
        self.mem.write_json("contracts/index.json", {
            "contracts": [{"id": "test", "status": "draft"}]
        })
        result = self.executor.execute("set_contract_status", {
            "contract_id": "test",
            "status": "in_review",
        })
        self.assertTrue(result["success"])
        idx = self.mem.read_json("contracts/index.json")
        rec = [c for c in idx["contracts"] if c["id"] == "test"][0]
        self.assertEqual(rec["status"], "in_review")

    def test_set_contract_status_invalid(self):
        self.mem.write_json("contracts/index.json", {"contracts": []})
        result = self.executor.execute("set_contract_status", {
            "contract_id": "test",
            "status": "banana",
        })
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
