"""Tests for generate_datamart_spec tool and check_data_availability suggested tables."""

import unittest
from unittest.mock import MagicMock, patch


class FakeMemory:
    def __init__(self):
        self._files = {}

    def read_file(self, path: str):
        return self._files.get(path, "")

    def write_file(self, path: str, content: str):
        self._files[path] = content

    def read_json(self, path: str):
        return None


class FakeLLM:
    expert_model = "fake/expert"

    def call_heavy(self, system, user, max_tokens=2000):
        return "# Datamart spec\n\nCREATE TABLE ai_bi.test_mart AS ..."

    def call_cheap(self, system, user, **kw):
        return "table1\ntable2"


CONTRACT_MD = """\
# contract_churn

## Определение
Коэффициент оттока клиентов за период.

## Формула
churned_customers / total_customers * 100

## Гранулярность
Месяц

## Включает
Все клиенты с активными контрактами.

## Исключения
Тестовые аккаунты.

## Источник данных
ai_bi.customers, ai_bi.subscriptions

## Ответственный за данные
data_team

## Ответственный за расчёт
analytics_team
"""

MCP_PATCH = "src.mcp_client.MCPClient"


class TestGenerateDatamartSpec(unittest.TestCase):
    def _make_executor(self, memory=None, llm=None, mm=None):
        from src.tools import ToolExecutor
        mem = memory or FakeMemory()
        return ToolExecutor(mem, mm or MagicMock(), llm or FakeLLM(), thread_root_id="t1")

    def test_generates_spec_from_contract(self):
        mem = FakeMemory()
        mem._files["contracts/contract_churn.md"] = CONTRACT_MD
        mem._files["prompts/datamart_spec.md"] = "Generate datamart spec."

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = [
                {"table": "customers"}, {"table": "subscriptions"},
            ]
            MockMCP.return_value = mock_client

            executor = self._make_executor(memory=mem)
            result = executor._tool_generate_datamart_spec("contract_churn")

        self.assertTrue(result.get("success"))
        self.assertEqual(result["contract_id"], "contract_churn")
        self.assertIn("spec", result)
        self.assertIn("specs/contract_churn_datamart.md", mem._files)

    def test_returns_error_when_contract_not_found(self):
        mem = FakeMemory()
        executor = self._make_executor(memory=mem)
        result = executor._tool_generate_datamart_spec("nonexistent")
        self.assertIn("error", result)

    def test_falls_back_to_draft(self):
        mem = FakeMemory()
        mem._files["drafts/contract_churn.md"] = CONTRACT_MD
        mem._files["prompts/datamart_spec.md"] = "Generate datamart spec."

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = []
            MockMCP.return_value = mock_client

            executor = self._make_executor(memory=mem)
            result = executor._tool_generate_datamart_spec("contract_churn")

        self.assertTrue(result.get("success"))

    def test_returns_error_when_no_spec_prompt(self):
        mem = FakeMemory()
        mem._files["contracts/contract_churn.md"] = CONTRACT_MD

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = []
            MockMCP.return_value = mock_client

            executor = self._make_executor(memory=mem)
            result = executor._tool_generate_datamart_spec("contract_churn")

        self.assertIn("error", result)

    def test_returns_error_when_llm_returns_empty(self):
        mem = FakeMemory()
        mem._files["contracts/contract_churn.md"] = CONTRACT_MD
        mem._files["prompts/datamart_spec.md"] = "Generate spec."

        llm = FakeLLM()
        llm.call_heavy = MagicMock(return_value="")

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = []
            MockMCP.return_value = mock_client

            executor = self._make_executor(memory=mem, llm=llm)
            result = executor._tool_generate_datamart_spec("contract_churn")

        self.assertIn("error", result)

    def test_mcp_unavailable_continues(self):
        """Should still generate spec even if MCP is unavailable."""
        mem = FakeMemory()
        mem._files["contracts/contract_churn.md"] = CONTRACT_MD
        mem._files["prompts/datamart_spec.md"] = "Generate spec."

        with patch(MCP_PATCH) as MockMCP:
            MockMCP.return_value.initialize.side_effect = Exception("connection refused")

            executor = self._make_executor(memory=mem)
            result = executor._tool_generate_datamart_spec("contract_churn")

        self.assertTrue(result.get("success"))


class TestCheckDataAvailabilitySuggested(unittest.TestCase):
    """Test suggested_tables logic in check_data_availability."""

    def _make_executor(self, llm=None):
        from src.tools import ToolExecutor
        return ToolExecutor(FakeMemory(), MagicMock(), llm or FakeLLM(), thread_root_id="t1")

    def test_suggests_similar_tables(self):
        """When tables are missing, should suggest similar ones from schema."""
        llm = FakeLLM()
        llm.call_cheap = MagicMock(return_value="customer_churn_daily")

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = [
                {"table": "customer_churn_monthly"},
                {"table": "customer_retention"},
                {"table": "orders"},
            ]
            MockMCP.return_value = mock_client

            executor = self._make_executor(llm=llm)
            result = executor._tool_check_data_availability(
                "contract_churn", "customer_churn_daily table"
            )

        self.assertFalse(result["available"])
        self.assertIn("customer_churn_daily", result["tables_missing"])
        suggested = result.get("suggested_tables", [])
        self.assertTrue(
            any("customer" in t for t in suggested),
            f"Expected customer-related suggestion, got {suggested}"
        )

    def test_all_tables_found(self):
        """When all tables found, available should be True."""
        llm = FakeLLM()
        llm.call_cheap = MagicMock(return_value="customers\nsubscriptions")

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = [
                {"table": "customers"}, {"table": "subscriptions"},
            ]
            MockMCP.return_value = mock_client

            executor = self._make_executor(llm=llm)
            result = executor._tool_check_data_availability(
                "contract_churn", "customers and subscriptions tables"
            )

        self.assertTrue(result["available"])
        self.assertEqual(result["tables_missing"], [])

    def test_no_candidate_tables(self):
        """When LLM can't extract tables, available should be None."""
        llm = FakeLLM()
        llm.call_cheap = MagicMock(return_value="нет")

        with patch(MCP_PATCH) as MockMCP:
            mock_client = MagicMock()
            mock_client.list_objects.return_value = [{"table": "customers"}]
            MockMCP.return_value = mock_client

            executor = self._make_executor(llm=llm)
            result = executor._tool_check_data_availability(
                "contract_churn", "какие-то данные"
            )

        self.assertIsNone(result["available"])


if __name__ == "__main__":
    unittest.main()
