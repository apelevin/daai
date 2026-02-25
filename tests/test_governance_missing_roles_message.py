import os
import tempfile
import unittest

from src.agent import Agent
from src.memory import Memory


class FakeLLM:
    def call_cheap(self, *a, **kw):
        return "{}"

    def call_heavy(self, *a, **kw):
        return "ok"


class FakeMM:
    def send_dm(self, *a, **kw):
        return None


class GovernanceMissingRolesMessageTest(unittest.TestCase):
    def test_missing_roles_includes_short_descriptions(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["DATA_DIR"] = td
            mem = Memory()
            # policy requires circle_lead and data_lead
            mem.write_json(
                "context/governance.json",
                {"tiers": {"tier_2": {"approval_required": ["circle_lead", "data_lead"], "consensus_threshold": 1.0}}},
            )
            mem.write_json("context/roles.json", {"roles": {}})
            mem.write_json("contracts/index.json", {"contracts": [{"id": "x", "name": "x", "status": "in_review"}]})

            agent = Agent(FakeLLM(), mem, FakeMM())

            route_data = {"type": "contract_discussion", "entity": "x", "channel_type": "channel"}
            raw = (
                "[SAVE_CONTRACT:x]\n"
                "# Data Contract: X\n\n"
                "## Статус\nСогласован\n\n"
                "## Определение\nX\n\n"
                "## Формула\nЧеловеческая: x\n\nПсевдо‑SQL: SELECT 1;\n\n"
                "## Источник данных\nx\n\n"
                "## Включает\nx\n\n"
                "## Исключения\nx\n\n"
                "## Гранулярность\nx\n\n"
                "## Ответственный за данные\n@a\n\n"
                "## Ответственный за расчёт\n@a\n\n"
                "## Связь с Extra Time\nX → Extra Time\n\n"
                "## Потребители\nx\n\n"
                "## Состояние данных\nx\n\n"
                "## Известные проблемы\nx\n\n"
                "## Связанные контракты\n- y\n\n"
                "## Согласовано\n@a — 2026-02-24\n\n"
                "## История изменений\n2026-02-24 — ok\n"
                "[/SAVE_CONTRACT]"
            )
            reply, _info = agent._handle_side_effects(raw, route_data, user_message="зафиксируй контракт")
            self.assertIn("Не хватает ролей", reply)
            self.assertIn("Data Lead", reply)
            self.assertIn("Circle Lead", reply)


if __name__ == "__main__":
    unittest.main()
