"""Integration test: tool-use agent on the issues/3.md conversation.

Simulates the key failure scenario from the old agent:
- User discusses "Конверсия лида" metric
- User says "зафиксируй контракт"
- Old agent: falsely claims "сохранён" without actual save
- New agent: calls save_contract tool, gets structured error, reports truth

We test three scenarios:
1. Save attempt with missing governance roles → agent reports errors
2. Save attempt with invalid contract → agent reports validation errors
3. Save attempt with everything OK → agent confirms truthfully
"""

import json
import os
import tempfile
import unittest

from src.agent import Agent
from src.memory import Memory
from src.tools import ToolExecutor


# ── Fake LLM that simulates tool-calling behavior ───────────────────────────

class FakeToolCall:
    """Mimics openai ChatCompletionMessageToolCall."""
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)})()


class FakeMessage:
    """Mimics openai ChatCompletionMessage."""
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.role = "assistant"


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]
        self.usage = FakeUsage()


LEAD_CONVERSION_DRAFT = """# Data Contract: Конверсия лида

## Статус
На согласовании

## Определение
Конверсия лида — соотношение количества лидов к оплаченным лидам (фактическим заказам).

## Формула
Человеческая: Конверсия = Количество оплаченных заказов / Количество лидов × 100%

Псевдо-SQL: SELECT COUNT(orders) / COUNT(leads) * 100 FROM crm WHERE period = current_month;

## Источник данных
CRM. Лиды приходят автоматически с сайта, оплаты фиксируются автоматически.

## Включает
Все заявки с сайта считаются лидами.

## Исключения
Нет исключений.

## Гранулярность
Ежемесячно. Пересчитывается раз в неделю.

## Ответственный за данные
Круг Sales Operations

## Ответственный за расчёт
Круг Sales Operations

## Связь с Extra Time
Конверсия лида → Эффективность воронки → Revenue → Extra Time

## Потребители
Sales, Marketing

## Состояние данных
Данные есть, качество подтверждено: CRM автоматическая фиксация.

## Согласовано
@pelevin — 2026-02-25

## История изменений
2026-02-25 — создан черновик
"""


class ToolUseLLM:
    """Fake LLM that calls tools in a scripted sequence.

    For each call_with_tools invocation, pops the next scenario from the script.
    Each scenario is a list of turns. Each turn is either:
    - ("tool_calls", [...])  — LLM returns tool calls
    - ("text", "...")        — LLM returns final text
    """

    def __init__(self, scenarios: list[list[tuple]]):
        self.scenarios = list(scenarios)
        self.tool_calls_log: list[tuple[str, dict]] = []
        self.cheap_model = "fake/cheap"
        self.heavy_model = "fake/heavy"

    def call_cheap(self, system, user, **kw):
        return json.dumps({
            "type": "contract_discussion",
            "entity": "lead_conversion",
            "load_files": ["contracts/index.json", "drafts/lead_conversion.md", "drafts/lead_conversion_discussion.json"],
            "model": "heavy",
        })

    def call_heavy(self, system, user, **kw):
        return "(fake heavy)"

    def call_with_tools(self, system_prompt, user_message, tools, tool_executor, *, max_turns=5, max_tokens=2000):
        """Execute scripted tool-calling scenario."""
        if not self.scenarios:
            return "(no more scenarios)"
        scenario = self.scenarios.pop(0)

        for turn in scenario:
            kind = turn[0]
            if kind == "text":
                return turn[1]
            elif kind == "tool_calls":
                # Execute tool calls and record results
                for tc_name, tc_args in turn[1]:
                    self.tool_calls_log.append((tc_name, tc_args))
                    result = tool_executor(tc_name, tc_args)
                    # The result is available to the next turn in the script
                    # Store it for assertion
                    self.tool_calls_log.append(("_result", result))
            else:
                raise ValueError(f"Unknown turn kind: {kind}")

        return "(scenario ended without text)"


class FakeMM:
    def send_dm(self, *a, **kw):
        return None

    def resolve_username(self, raw):
        return None


# ── Test cases ───────────────────────────────────────────────────────────────

class TestToolUseIntegration(unittest.TestCase):
    """Test that the tool-use path correctly handles the issues/3.md scenario."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.tmpdir
        os.environ["AGENT_USE_TOOLS"] = "true"
        self.mem = Memory()

        # Seed files
        self.mem.write_file("prompts/system_full.md", "Ты — AI-архитектор метрик.")
        self.mem.write_file("prompts/system_short.md", "Кратко.")
        self.mem.write_file("prompts/router.md", "Классифицируй сообщение. Верни только JSON.")
        self.mem.write_json("contracts/index.json", {
            "contracts": [{"id": "lead_conversion", "name": "Конверсия лида", "status": "in_review", "tier": "tier_2"}]
        })
        self.mem.write_file("drafts/lead_conversion.md", LEAD_CONVERSION_DRAFT)
        self.mem.write_json("drafts/lead_conversion_discussion.json", {
            "entity": "lead_conversion",
            "status": "consensus_reached",
            "positions": {"pelevin": "agree"},
        })

    def tearDown(self):
        os.environ.pop("AGENT_USE_TOOLS", None)

    def test_scenario_1_governance_failure(self):
        """User asks to save, but governance roles are missing → agent reports error."""
        # Set up governance requiring roles that aren't assigned
        self.mem.write_json("context/governance.json", {
            "tiers": {
                "tier_2": {
                    "approval_required": ["data_lead", "circle_lead"],
                    "consensus_threshold": 1.0,
                    "description": "Операционные метрики",
                }
            }
        })
        # No roles assigned in tasks/roles.json

        # Script: LLM reads draft, then tries save_contract, gets error, reports it
        llm = ToolUseLLM(scenarios=[[
            ("tool_calls", [
                ("read_draft", {"contract_id": "lead_conversion"}),
            ]),
            ("tool_calls", [
                ("save_contract", {"contract_id": "lead_conversion", "content": LEAD_CONVERSION_DRAFT}),
            ]),
            ("text", "⚠️ Контракт НЕ сохранён. Проблемы:\n\n"
                     "- Governance (tier_2): не хватает ролей: data_lead, circle_lead\n\n"
                     "Назначьте роли и повторите."),
        ]])

        agent = Agent(llm, self.mem, FakeMM())
        reply = agent.process_message(
            username="pelevin",
            message="зафиксируй контракт lead_conversion",
            channel_type="O",
            thread_context=None,
        )

        # Verify tool calls were made
        tool_names = [name for name, _ in llm.tool_calls_log if not name.startswith("_")]
        self.assertIn("read_draft", tool_names)
        self.assertIn("save_contract", tool_names)

        # Verify save_contract returned failure
        save_results = [r for name, r in llm.tool_calls_log if name == "_result" and isinstance(r, dict) and "success" in r]
        self.assertTrue(len(save_results) > 0)
        save_result = save_results[-1]  # last save result
        self.assertFalse(save_result["success"])
        self.assertTrue(any("роле" in e or "Governance" in e for e in save_result["errors"]))

        # Contract should NOT exist on disk
        self.assertIsNone(self.mem.get_contract("lead_conversion"))

        # Reply should mention the problem (from scripted LLM)
        self.assertIn("НЕ сохранён", reply)

    def test_scenario_2_validation_failure(self):
        """User asks to save incomplete contract → validation errors reported."""
        incomplete_md = "# Data Contract: Broken\n\n## Статус\nЧерновик\n\n## Определение\nЧто-то.\n"
        self.mem.write_file("drafts/lead_conversion.md", incomplete_md)

        llm = ToolUseLLM(scenarios=[[
            ("tool_calls", [
                ("save_contract", {"contract_id": "lead_conversion", "content": incomplete_md}),
            ]),
            ("text", "⚠️ Контракт не прошёл валидацию. Не заполнены секции: Формула, Источник данных и другие."),
        ]])

        agent = Agent(llm, self.mem, FakeMM())
        reply = agent.process_message(
            username="pelevin",
            message="зафиксируй контракт lead_conversion",
            channel_type="O",
            thread_context=None,
        )

        # Check that save_contract returned validation errors
        save_results = [r for name, r in llm.tool_calls_log if name == "_result" and isinstance(r, dict) and "success" in r]
        self.assertTrue(len(save_results) > 0)
        self.assertFalse(save_results[0]["success"])
        self.assertTrue(any("Валидация" in e for e in save_results[0]["errors"]))

        # Contract NOT saved
        self.assertIsNone(self.mem.get_contract("lead_conversion"))

    def test_scenario_3_successful_save(self):
        """Roles assigned, valid contract → save succeeds, agent confirms truthfully."""
        self.mem.write_json("context/governance.json", {
            "tiers": {
                "tier_2": {
                    "approval_required": ["data_lead", "circle_lead"],
                    "consensus_threshold": 1.0,
                }
            }
        })
        self.mem.write_json("tasks/roles.json", {
            "roles": {"data_lead": ["pavelpetrin"], "circle_lead": ["korabovtsev"]}
        })

        llm = ToolUseLLM(scenarios=[[
            ("tool_calls", [
                ("read_draft", {"contract_id": "lead_conversion"}),
            ]),
            ("tool_calls", [
                ("save_contract", {"contract_id": "lead_conversion", "content": LEAD_CONVERSION_DRAFT}),
            ]),
            ("text", "✅ Контракт «Конверсия лида» сохранён! Все проверки пройдены."),
        ]])

        agent = Agent(llm, self.mem, FakeMM())
        reply = agent.process_message(
            username="pelevin",
            message="зафиксируй контракт lead_conversion",
            channel_type="O",
            thread_context=None,
        )

        # Check save_contract succeeded
        save_results = [r for name, r in llm.tool_calls_log if name == "_result" and isinstance(r, dict) and "success" in r]
        self.assertTrue(len(save_results) > 0)
        last_save = [r for r in save_results if "contract_id" in r][-1]
        self.assertTrue(last_save["success"], f"Expected success, got: {last_save}")

        # Contract MUST exist on disk
        md = self.mem.get_contract("lead_conversion")
        self.assertIsNotNone(md)
        self.assertIn("Конверсия лида", md)

        # Index updated
        idx = self.mem.read_json("contracts/index.json")
        rec = [c for c in idx["contracts"] if c["id"] == "lead_conversion"][0]
        self.assertEqual(rec["status"], "agreed")

        # Reply confirms save
        self.assertIn("сохранён", reply)

    def test_scenario_4_retry_after_role_assignment(self):
        """LLM tries save, fails on governance, assigns role, retries → success."""
        self.mem.write_json("context/governance.json", {
            "tiers": {
                "tier_2": {
                    "approval_required": ["data_lead", "circle_lead"],
                    "consensus_threshold": 1.0,
                }
            }
        })
        # Only data_lead assigned, circle_lead missing
        self.mem.write_json("tasks/roles.json", {
            "roles": {"data_lead": ["pavelpetrin"]}
        })

        llm = ToolUseLLM(scenarios=[[
            # Turn 1: try save → fails (missing circle_lead)
            ("tool_calls", [
                ("save_contract", {"contract_id": "lead_conversion", "content": LEAD_CONVERSION_DRAFT}),
            ]),
            # Turn 2: LLM assigns the missing role
            ("tool_calls", [
                ("assign_role", {"role": "circle_lead", "username": "korabovtsev"}),
            ]),
            # Turn 3: retry save → succeeds
            ("tool_calls", [
                ("save_contract", {"contract_id": "lead_conversion", "content": LEAD_CONVERSION_DRAFT}),
            ]),
            ("text", "✅ Назначил @korabovtsev как Circle Lead и сохранил контракт «Конверсия лида»!"),
        ]])

        agent = Agent(llm, self.mem, FakeMM())
        reply = agent.process_message(
            username="pelevin",
            message="зафиксируй контракт lead_conversion",
            channel_type="O",
            thread_context=None,
        )

        # Check tool call sequence
        tool_names = [name for name, _ in llm.tool_calls_log if not name.startswith("_")]
        self.assertEqual(tool_names, ["save_contract", "assign_role", "save_contract"])

        # First save failed, second succeeded
        save_results = [r for name, r in llm.tool_calls_log if name == "_result" and isinstance(r, dict) and "success" in r and "contract_id" in r]
        self.assertEqual(len(save_results), 2)
        self.assertFalse(save_results[0]["success"])  # first attempt
        self.assertTrue(save_results[1]["success"])   # second attempt

        # Contract saved
        self.assertIsNotNone(self.mem.get_contract("lead_conversion"))

        # Role was actually persisted
        roles = self.mem.read_json("tasks/roles.json")
        self.assertIn("korabovtsev", roles["roles"]["circle_lead"])

    def test_scenario_5_dm_no_write_tools(self):
        """In DM, write tools should NOT be available — agent uses only read tools."""
        llm = ToolUseLLM(scenarios=[[
            ("tool_calls", [
                ("read_draft", {"contract_id": "lead_conversion"}),
            ]),
            ("text", "Вот текущий черновик контракта «Конверсия лида». Сохранение возможно только в канале."),
        ]])

        agent = Agent(llm, self.mem, FakeMM())
        # DM channel type
        reply = agent.process_message(
            username="pelevin",
            message="покажи что в черновике lead_conversion",
            channel_type="dm",
            thread_context=None,
        )
        # Should work — read tools are available
        tool_names = [name for name, _ in llm.tool_calls_log if not name.startswith("_")]
        self.assertIn("read_draft", tool_names)


if __name__ == "__main__":
    unittest.main()
