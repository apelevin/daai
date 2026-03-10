"""Tests for expert opinion route detection and handling."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from src.router import route


class FakeLLM:
    expert_model = "fake/expert"

    def __init__(self, raw: str = "{}"):
        self.raw = raw

    def call_cheap(self, system, user, **kw):
        return self.raw


class FakeMemory:
    def read_file(self, path: str):
        return "{}"


class TestExpertOpinionRouteDetection(unittest.TestCase):
    """Test that @mention + opinion keywords trigger expert_opinion route."""

    def setUp(self):
        self.llm = FakeLLM()
        self.mem = FakeMemory()
        self.env_patch = patch.dict(os.environ, {
            "MATTERMOST_BOT_USERNAME": "finist",
            "BOT_DISPLAY_NAME": "Финист",
        })
        self.env_patch.start()
        # Patch config constants already imported in router
        self._p1 = patch("src.router.BOT_USERNAME", "finist")
        self._p2 = patch("src.router.BOT_DISPLAY_NAME", "Финист")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self.env_patch.stop()

    def test_direct_mention_with_opinion_keyword(self):
        res = route(self.llm, self.mem, "user1",
                    "@finist как ты думаешь про эту метрику?", "channel")
        self.assertEqual(res["type"], "expert_opinion")
        self.assertEqual(res["model"], "expert")

    def test_display_name_mention_with_opinion(self):
        res = route(self.llm, self.mem, "user1",
                    "@Финист какие риски у этого подхода?", "channel")
        self.assertEqual(res["type"], "expert_opinion")

    def test_mention_with_tvoe_mnenie(self):
        res = route(self.llm, self.mem, "user1",
                    "@finist твоё мнение по contract_churn?", "channel")
        self.assertEqual(res["type"], "expert_opinion")

    def test_mention_with_recommend(self):
        res = route(self.llm, self.mem, "user1",
                    "@Финист порекомендуй подход к расчёту", "channel")
        self.assertEqual(res["type"], "expert_opinion")

    def test_mention_with_analyze(self):
        res = route(self.llm, self.mem, "user1",
                    "@finist проанализируй связь между churn и retention", "channel")
        self.assertEqual(res["type"], "expert_opinion")

    def test_mention_with_kak_schitaesh(self):
        res = route(self.llm, self.mem, "user1",
                    "@Финист как считаешь, стоит ли объединить?", "channel")
        self.assertEqual(res["type"], "expert_opinion")

    def test_no_mention_no_expert(self):
        """Without @mention, opinion keywords should NOT trigger expert route."""
        # This should go through LLM router, not expert_opinion
        self.llm.raw = json.dumps({
            "type": "general_question", "entity": None,
            "load_files": [], "model": "heavy",
        })
        res = route(self.llm, self.mem, "user1",
                    "как ты думаешь про эту метрику?", "channel")
        self.assertNotEqual(res["type"], "expert_opinion")

    def test_mention_without_opinion_keyword_no_expert(self):
        """@mention without opinion keywords should NOT trigger expert route."""
        self.llm.raw = json.dumps({
            "type": "general_question", "entity": None,
            "load_files": [], "model": "heavy",
        })
        res = route(self.llm, self.mem, "user1",
                    "@finist покажи список контрактов", "channel")
        self.assertNotEqual(res["type"], "expert_opinion")

    def test_expert_extracts_contract_id(self):
        """Should extract contract_id from message when it's first matchable word."""
        res = route(self.llm, self.mem, "user1",
                    "@finist оцени contract_churn", "channel")
        self.assertEqual(res["type"], "expert_opinion")
        # re.search finds first 3+ char word that's not in skip set
        self.assertEqual(res["entity"], "contract_churn")

    def test_expert_extracts_contract_id_from_thread(self):
        """Should extract contract_id from thread context if not in message."""
        res = route(self.llm, self.mem, "user1",
                    "@Финист что скажешь?", "channel",
                    thread_context="AI-архитектор: работаем над контрактом contract_churn")
        self.assertEqual(res["type"], "expert_opinion")
        # "что" and "скажешь" are 3+ char words but matched first;
        # entity extraction from thread uses regex for "контракт[а]? X"
        self.assertEqual(res["entity"], "contract_churn")

    def test_expert_load_files(self):
        """Expert route should load contract files when entity found."""
        res = route(self.llm, self.mem, "user1",
                    "@finist оцени contract_churn", "channel")
        self.assertEqual(res["type"], "expert_opinion")
        self.assertIn("drafts/contract_churn.md", res["load_files"])
        self.assertIn("contracts/contract_churn.md", res["load_files"])

    def test_expert_no_entity_empty_load_files(self):
        """Expert route with no entity should have empty load_files."""
        res = route(self.llm, self.mem, "user1",
                    "@Финист как ты думаешь?", "channel")
        self.assertEqual(res["type"], "expert_opinion")
        self.assertEqual(res["load_files"], [])

    def test_fast_path_takes_priority_over_expert(self):
        """Fast-path commands should win even with @mention."""
        res = route(self.llm, self.mem, "user1",
                    "покажи контракт contract_churn", "channel")
        self.assertEqual(res["type"], "show_contract")


if __name__ == "__main__":
    unittest.main()
