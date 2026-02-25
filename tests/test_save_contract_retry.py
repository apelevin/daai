import os
import json
import tempfile
import unittest

from src.agent import Agent
from src.memory import Memory


class FakeLLM:
    def __init__(self):
        self.heavy_calls = []

    def call_cheap(self, system, user, **kw):
        # Router/system isn't used in this unit test.
        return "{}"

    def call_heavy(self, system, user, **kw):
        self.heavy_calls.append((system, user))
        # First call: model "lies" and doesn't include SAVE_CONTRACT
        if len(self.heavy_calls) == 1:
            return "✅ Data Contract уже зафиксирован!"
        # Second call: must emit SAVE_CONTRACT
        return (
            "[SAVE_CONTRACT:client_tier_segmentation]\n"
            "# Data Contract: Сегментирование клиентов по тирам\n\n"
            "## Статус\nСогласован\n\n"
            "## Определение\nАвтоматическое распределение клиентов по тирам.\n\n"
            "## Формула\n"
            "Человеческая: сначала считаем по выручке, при отсутствии выручки — fallback по основному ОКВЭД.\n\n"
            "Псевдо‑SQL: SELECT CASE WHEN revenue>=10000000000 THEN 'Tier1' ELSE 'Tier2' END;\n\n"
            "## Источник данных\nCasebook API + internal API (ОКВЭД).\n\n"
            "## Включает\nВсе компании с ИНН.\n\n"
            "## Исключения\nНет.\n\n"
            "## Гранулярность\nКлиент (ИНН), последняя известная выручка.\n\n"
            "## Ответственный за данные\n@pavelpetrin\n\n"
            "## Ответственный за расчёт\n@pavelpetrin\n\n"
            "## Связь с Extra Time\nСегментация → LTV/CAC → Extra Time\n\n"
            "## Потребители\nSales, RevOps, Finance\n\n"
            "## Состояние данных\nДанные есть.\n\n"
            "## Известные проблемы\nПокрытие выручки неполное.\n\n"
            "## Связанные контракты\n- win_ni\n\n"
            "## Согласовано\n@a — 2026-02-24\n\n"
            "## История изменений\n2026-02-24 — согласован\n"
            "[/SAVE_CONTRACT]"
        )


class FakeMM:
    def send_dm(self, *a, **kw):
        return None


class SaveContractRetryTest(unittest.TestCase):
    def test_retry_saves_contract_and_updates_index(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["DATA_DIR"] = td
            mem = Memory()
            # Prepare a draft and discussion so retry has material
            mem.write_file("drafts/client_tier_segmentation.md", "# draft\n")
            mem.write_json("drafts/client_tier_segmentation_discussion.json", {"status": "draft_created"})
            mem.write_json("contracts/index.json", {"contracts": [{"id": "client_tier_segmentation", "name": "client_tier_segmentation", "status": "in_review", "file": "drafts/client_tier_segmentation.md"}]})

            llm = FakeLLM()
            # Simulate that the main heavy response already happened (and contained no SAVE_CONTRACT),
            # so the retry is the *second* heavy call.
            llm.heavy_calls.append(("system", "user"))
            agent = Agent(llm, mem, FakeMM())

            route_data = {"type": "contract_discussion", "entity": "client_tier_segmentation", "channel_type": "channel"}

            reply, info = agent._handle_side_effects(
                raw_response="✅ Data Contract уже зафиксирован!",
                route_data=route_data,
                user_message="зафиксируй контракт",
            )

            # Should have triggered one retry heavy call (total 2)
            self.assertGreaterEqual(len(llm.heavy_calls), 1)

            # Contract file should exist
            path = os.path.join(td, "contracts", "client_tier_segmentation.md")
            self.assertTrue(os.path.exists(path))

            # Index should be updated
            with open(os.path.join(td, "contracts", "index.json"), "r", encoding="utf-8") as f:
                idx = json.load(f)
            rec = [c for c in idx["contracts"] if c.get("id") == "client_tier_segmentation"][0]
            self.assertEqual(rec.get("status"), "agreed")
            self.assertEqual(rec.get("file"), "contracts/client_tier_segmentation.md")
            self.assertTrue(rec.get("agreed_date"))
            self.assertTrue(rec.get("status_updated_at"))


if __name__ == "__main__":
    unittest.main()
