"""Tests for planner datamart_needed candidate scoring and action dispatch."""

import json
import unittest
from unittest.mock import MagicMock, patch


class FakeMemory:
    def __init__(self):
        self._files = {}
        self.base_dir = "/tmp/test_planner"

    def read_file(self, path: str):
        return self._files.get(path, "")

    def write_file(self, path: str, content: str):
        self._files[path] = content

    def read_json(self, path: str):
        raw = self._files.get(path, "")
        if raw:
            return json.loads(raw)
        return None

    def write_json(self, path: str, data):
        self._files[path] = json.dumps(data)


class FakeLLM:
    expert_model = "fake/expert"

    def call_cheap(self, system, user, **kw):
        return "{}"

    def call_heavy(self, system, user, max_tokens=2000):
        return "# Spec"

    def call_with_tools(self, **kw):
        return "ok"


class TestPlannerDatamartCandidate(unittest.TestCase):
    """Test that planner identifies agreed contracts without specs as datamart_needed."""

    def _make_planner(self, memory=None):
        from src.planner import ContinuousPlanner
        mem = memory or FakeMemory()
        mm = MagicMock()
        mm.bot_user_id = "bot1"
        mm.channel_id = "chan1"
        return ContinuousPlanner(mem, mm, FakeLLM())

    def test_agreed_contract_without_spec_becomes_candidate(self):
        mem = FakeMemory()

        planner = self._make_planner(memory=mem)

        gathered = {
            "contracts": [
                {"id": "contract_churn", "name": "Churn Rate", "status": "agreed"},
            ],
            "queue": [],
            "uncovered": [],
            "conflicts": [],
        }
        state = {"initiatives": []}

        candidates = planner._score(gathered, state)

        datamart_candidates = [c for c in candidates if c.candidate_type == "datamart_needed"]
        self.assertTrue(len(datamart_candidates) > 0,
                        f"Expected datamart_needed candidates, got types: "
                        f"{[c.candidate_type for c in candidates]}")
        self.assertEqual(datamart_candidates[0].contract_id, "contract_churn")
        self.assertEqual(datamart_candidates[0].breakdown.get("datamart_boost"), 15.0)

    def test_agreed_contract_with_spec_not_candidate(self):
        mem = FakeMemory()
        mem._files["specs/contract_churn_datamart.md"] = "# existing spec"

        planner = self._make_planner(memory=mem)

        gathered = {
            "contracts": [
                {"id": "contract_churn", "name": "Churn Rate", "status": "agreed"},
            ],
            "queue": [],
            "uncovered": [],
            "conflicts": [],
        }
        state = {"initiatives": []}

        candidates = planner._score(gathered, state)

        datamart_candidates = [c for c in candidates if c.candidate_type == "datamart_needed"]
        self.assertEqual(len(datamart_candidates), 0,
                         "Contract with existing spec should not be a datamart candidate")

    def test_draft_contract_not_datamart_candidate(self):
        mem = FakeMemory()

        planner = self._make_planner(memory=mem)

        gathered = {
            "contracts": [
                {"id": "contract_churn", "name": "Churn Rate", "status": "draft"},
            ],
            "queue": [],
            "uncovered": [],
            "conflicts": [],
        }
        state = {"initiatives": []}

        candidates = planner._score(gathered, state)

        datamart_candidates = [c for c in candidates if c.candidate_type == "datamart_needed"]
        self.assertEqual(len(datamart_candidates), 0)


class TestPlannerDatamartAction(unittest.TestCase):
    """Test _generate_datamart_spec action handler in ActionDispatcher."""

    def test_generate_datamart_spec_action(self):
        from src.planner_actions import ActionDispatcher

        mem = FakeMemory()
        mm = MagicMock()
        mm.send_to_channel.return_value = {"id": "post_123"}

        with patch("src.tools.ToolExecutor") as MockExecutor:
            mock_exec = MagicMock()
            mock_exec._tool_generate_datamart_spec.return_value = {
                "success": True,
                "contract_id": "contract_churn",
                "spec": "# Datamart spec content",
                "spec_file": "specs/contract_churn_datamart.md",
            }
            MockExecutor.return_value = mock_exec

            dispatcher = ActionDispatcher(mem, mm, FakeLLM())
            result = dispatcher._generate_datamart_spec(
                {"contract_id": "contract_churn"},
                {"thread_id": "thread_1"},
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "generate_datamart_spec")
        self.assertEqual(result["contract_id"], "contract_churn")
        mm.send_to_channel.assert_called_once()

    def test_generate_datamart_spec_action_error(self):
        from src.planner_actions import ActionDispatcher

        mem = FakeMemory()
        mm = MagicMock()

        with patch("src.tools.ToolExecutor") as MockExecutor:
            mock_exec = MagicMock()
            mock_exec._tool_generate_datamart_spec.return_value = {
                "error": "Контракт не найден",
            }
            MockExecutor.return_value = mock_exec

            dispatcher = ActionDispatcher(mem, mm, FakeLLM())
            result = dispatcher._generate_datamart_spec(
                {"contract_id": "nonexistent"},
                {"thread_id": "thread_1"},
            )

        self.assertIsNone(result)
        mm.send_to_channel.assert_not_called()


class TestGetDataLead(unittest.TestCase):
    """Test _get_data_lead helper."""

    def test_finds_data_lead_from_tasks_roles(self):
        from src.planner_actions import ActionDispatcher

        mem = FakeMemory()
        mem._files["tasks/roles.json"] = json.dumps({
            "roles": {"data_lead": ["data_user1", "data_user2"]}
        })

        dispatcher = ActionDispatcher(mem, MagicMock(), FakeLLM())
        lead = dispatcher._get_data_lead()
        self.assertEqual(lead, "data_user1")

    def test_finds_data_lead_from_context_roles(self):
        from src.planner_actions import ActionDispatcher

        mem = FakeMemory()
        mem._files["context/roles.json"] = json.dumps({
            "roles": {"data_lead": ["context_user"]}
        })

        dispatcher = ActionDispatcher(mem, MagicMock(), FakeLLM())
        lead = dispatcher._get_data_lead()
        self.assertEqual(lead, "context_user")

    def test_returns_none_when_no_roles(self):
        from src.planner_actions import ActionDispatcher

        mem = FakeMemory()
        dispatcher = ActionDispatcher(mem, MagicMock(), FakeLLM())
        lead = dispatcher._get_data_lead()
        self.assertIsNone(lead)

    def test_returns_none_when_empty_data_lead(self):
        from src.planner_actions import ActionDispatcher

        mem = FakeMemory()
        mem._files["tasks/roles.json"] = json.dumps({
            "roles": {"data_lead": []}
        })

        dispatcher = ActionDispatcher(mem, MagicMock(), FakeLLM())
        lead = dispatcher._get_data_lead()
        self.assertIsNone(lead)


if __name__ == "__main__":
    unittest.main()
