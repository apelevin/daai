"""Tests for planner.py — ContinuousPlanner cycle, state machine, rate limiter."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.memory import Memory
from src.planner import ContinuousPlanner
from src.planner_scoring import ScoredCandidate


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "planner_state.json").write_text(json.dumps({
        "initiatives": [],
        "daily_stats": {},
        "cooldowns": {},
        "last_plan_at": None,
    }), encoding="utf-8")
    (tmp_path / "tasks" / "queue.json").write_text('{"queue": []}', encoding="utf-8")
    (tmp_path / "tasks" / "reminders.json").write_text('{"reminders": []}', encoding="utf-8")
    (tmp_path / "tasks" / "suggestions.json").write_text('{"suggestions": []}', encoding="utf-8")
    (tmp_path / "tasks" / "active_threads.json").write_text('{"threads": {}}', encoding="utf-8")
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text('{"contracts": []}', encoding="utf-8")
    (tmp_path / "drafts").mkdir()
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "metrics_tree.md").write_text(
        "## Дерево\n```\nExtra Time\n├── MAU\n│   ├── Activation ← DATA CONTRACT\n│   └── Retention ← DATA CONTRACT\n└── Revenue\n    └── WIN NI ← DATA CONTRACT ✅\n```",
        encoding="utf-8",
    )
    (tmp_path / "context" / "circles.md").write_text(
        "## Product\nОтветственный: @product_lead\n\n## Sales\nОтветственный: @sales_lead",
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "planner_system.md").write_text("You are a planner.", encoding="utf-8")
    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


@pytest.fixture
def mm():
    mock = MagicMock()
    mock.send_to_channel.return_value = {"id": "post_123"}
    mock.send_dm.return_value = {"id": "dm_123"}
    mock.channel_id = "ch_001"
    return mock


@pytest.fixture
def llm():
    mock = MagicMock()
    mock.call_heavy.return_value = json.dumps({
        "analysis": "Test analysis",
        "actions": [
            {
                "type": "start_thread",
                "contract_id": "activation",
                "reason": "Uncovered metric",
                "message_hint": "Обсуждение Activation",
            }
        ],
    })
    return mock


@pytest.fixture
def planner(memory, mm, llm):
    return ContinuousPlanner(memory, mm, llm)


class TestGather:
    def test_gather_returns_expected_keys(self, planner):
        gathered = planner._gather()
        assert "contracts" in gathered
        assert "tree_md" in gathered
        assert "queue" in gathered
        assert "reminders" in gathered
        assert "conflicts" in gathered
        assert "uncovered" in gathered
        assert "discussions" in gathered

    def test_gather_finds_uncovered_metrics(self, planner):
        gathered = planner._gather()
        uncovered_ids = [u.contract_id for u in gathered["uncovered"]]
        assert len(uncovered_ids) >= 1  # Activation and Retention are uncovered


class TestScore:
    def test_score_returns_candidates(self, planner, memory):
        gathered = planner._gather()
        state = memory.get_planner_state()
        candidates = planner._score(gathered, state)
        assert len(candidates) >= 1

    def test_candidates_have_valid_scores(self, planner, memory):
        gathered = planner._gather()
        state = memory.get_planner_state()
        candidates = planner._score(gathered, state)
        for c in candidates:
            assert 0.0 <= c.score <= 1.0

    def test_active_initiatives_excluded(self, planner, memory):
        """Candidates with active initiatives should be excluded."""
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "id": "init_test",
            "contract_id": "activation",
            "status": "active",
            "type": "new_contract",
        }]
        memory.save_planner_state(state)

        gathered = planner._gather()
        state = memory.get_planner_state()
        candidates = planner._score(gathered, state)
        active_ids = [c.contract_id for c in candidates if c.candidate_type == "new_contract"]
        assert "activation" not in active_ids


class TestCheckLimits:
    def test_message_cap_blocks(self, planner, memory):
        state = memory.get_planner_state()
        daily = {"messages_sent": 8, "threads_started": 0}
        now = datetime.now(timezone.utc)
        action = {"type": "ask_question", "contract_id": "test"}
        assert planner._check_limits(action, state, daily, now) is False

    def test_thread_cap_blocks(self, planner, memory):
        state = memory.get_planner_state()
        daily = {"messages_sent": 0, "threads_started": 2}
        now = datetime.now(timezone.utc)
        action = {"type": "start_thread", "contract_id": "test"}
        assert planner._check_limits(action, state, daily, now) is False

    def test_active_initiatives_cap_blocks(self, planner, memory):
        state = memory.get_planner_state()
        state["initiatives"] = [
            {"contract_id": f"c{i}", "status": "active"} for i in range(3)
        ]
        daily = {"messages_sent": 0, "threads_started": 0}
        now = datetime.now(timezone.utc)
        action = {"type": "start_thread", "contract_id": "new"}
        assert planner._check_limits(action, state, daily, now) is False

    def test_cooldown_blocks(self, planner, memory):
        now = datetime.now(timezone.utc)
        state = memory.get_planner_state()
        state["cooldowns"] = {
            "propose_resolution:test": (now + timedelta(hours=24)).isoformat()
        }
        daily = {"messages_sent": 0, "threads_started": 0}
        action = {"type": "propose_resolution", "contract_id": "test"}
        assert planner._check_limits(action, state, daily, now) is False

    def test_allowed_when_no_limits_hit(self, planner, memory):
        state = memory.get_planner_state()
        daily = {"messages_sent": 0, "threads_started": 0}
        now = datetime.now(timezone.utc)
        action = {"type": "start_thread", "contract_id": "test"}
        assert planner._check_limits(action, state, daily, now) is True

    def test_per_initiative_daily_limit(self, planner, memory):
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "contract_id": "test",
            "status": "active",
            "actions_today": 2,
        }]
        daily = {"messages_sent": 0, "threads_started": 0}
        now = datetime.now(timezone.utc)
        action = {"type": "ask_question", "contract_id": "test"}
        assert planner._check_limits(action, state, daily, now) is False

    def test_followup_wait_blocks(self, planner, memory):
        now = datetime.now(timezone.utc)
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "contract_id": "test",
            "status": "waiting_response",
            "actions_today": 0,
            "next_action_after": (now + timedelta(hours=12)).isoformat(),
        }]
        daily = {"messages_sent": 0, "threads_started": 0}
        action = {"type": "follow_up", "contract_id": "test"}
        assert planner._check_limits(action, state, daily, now) is False


class TestInitiativeManagement:
    def test_create_new_initiative(self, planner, memory):
        state = memory.get_planner_state()
        now = datetime.now(timezone.utc)
        action = {"type": "start_thread", "contract_id": "test_metric"}
        candidates = [ScoredCandidate("test_metric", "Test Metric", 0.7, {}, "new_contract", stakeholders=["user1"])]

        init = planner._get_or_create_initiative(action, state, candidates, now)

        assert init["contract_id"] == "test_metric"
        assert init["status"] == "active"
        assert init["stakeholders"] == ["user1"]
        assert len(state["initiatives"]) == 1

    def test_reuse_existing_initiative(self, planner, memory):
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "id": "init_existing",
            "contract_id": "test_metric",
            "status": "active",
            "thread_id": "thread_001",
        }]
        now = datetime.now(timezone.utc)
        action = {"type": "ask_question", "contract_id": "test_metric"}

        init = planner._get_or_create_initiative(action, state, [], now)
        assert init["id"] == "init_existing"
        assert len(state["initiatives"]) == 1

    def test_abandon_stale_initiative(self, planner, memory):
        state = memory.get_planner_state()
        old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        state["initiatives"] = [{
            "id": "init_old",
            "contract_id": "stale",
            "status": "active",
            "created_at": old,
            "updated_at": old,
        }]

        now = datetime.now(timezone.utc)
        planner._abandon_stale_initiatives(state, now)

        assert state["initiatives"][0]["status"] == "abandoned"

    def test_fresh_initiative_not_abandoned(self, planner, memory):
        state = memory.get_planner_state()
        fresh = datetime.now(timezone.utc).isoformat()
        state["initiatives"] = [{
            "id": "init_fresh",
            "contract_id": "fresh",
            "status": "active",
            "created_at": fresh,
            "updated_at": fresh,
        }]

        now = datetime.now(timezone.utc)
        planner._abandon_stale_initiatives(state, now)

        assert state["initiatives"][0]["status"] == "active"


class TestNotifyThreadActivity:
    def test_removes_user_from_waiting(self, planner, memory):
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "id": "init_001",
            "contract_id": "test",
            "status": "waiting_response",
            "thread_id": "thread_abc",
            "waiting_for": ["user1", "user2"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]
        memory.save_planner_state(state)

        planner.notify_thread_activity("thread_abc", "user1")

        updated = memory.get_planner_state()
        init = updated["initiatives"][0]
        assert "user1" not in init["waiting_for"]
        assert "user2" in init["waiting_for"]

    def test_transitions_to_active(self, planner, memory):
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "id": "init_001",
            "contract_id": "test",
            "status": "waiting_response",
            "thread_id": "thread_abc",
            "waiting_for": ["user1"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]
        memory.save_planner_state(state)

        planner.notify_thread_activity("thread_abc", "user1")

        updated = memory.get_planner_state()
        assert updated["initiatives"][0]["status"] == "active"

    def test_ignores_unrelated_thread(self, planner, memory):
        state = memory.get_planner_state()
        state["initiatives"] = [{
            "id": "init_001",
            "contract_id": "test",
            "status": "waiting_response",
            "thread_id": "thread_abc",
            "waiting_for": ["user1"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]
        memory.save_planner_state(state)

        planner.notify_thread_activity("thread_xyz", "user1")

        updated = memory.get_planner_state()
        assert updated["initiatives"][0]["status"] == "waiting_response"


class TestRunCycle:
    def test_full_cycle_executes(self, planner, memory, mm, llm):
        """Full cycle: gather → score → plan → execute → persist."""
        planner._run_cycle()

        # LLM was called
        llm.call_heavy.assert_called_once()

        # Message was sent
        assert mm.send_to_channel.called

        # State was persisted
        state = memory.get_planner_state()
        assert state["last_plan_at"] is not None

    def test_cycle_with_no_candidates(self, planner, memory, mm, llm, data_dir):
        """Cycle with no uncovered metrics produces no actions."""
        # Clear the tree to have no uncovered nodes
        (data_dir / "context" / "metrics_tree.md").write_text(
            "## Дерево\n```\nExtra Time\n└── Revenue ← DATA CONTRACT ✅\n```",
            encoding="utf-8",
        )

        planner._run_cycle()

        # LLM should NOT have been called
        llm.call_heavy.assert_not_called()
        mm.send_to_channel.assert_not_called()

        # State still persisted
        state = memory.get_planner_state()
        assert state["last_plan_at"] is not None

    def test_cycle_respects_message_cap(self, planner, memory, mm, llm):
        """If daily cap is reached, actions are skipped."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = memory.get_planner_state()
        state["daily_stats"] = {today: {"messages_sent": 8, "threads_started": 2}}
        memory.save_planner_state(state)

        planner._run_cycle()

        # Message was NOT sent (cap reached)
        mm.send_to_channel.assert_not_called()

    def test_cycle_handles_llm_error(self, planner, memory, mm, llm):
        """Cycle handles LLM failure gracefully."""
        llm.call_heavy.side_effect = Exception("API error")

        planner._run_cycle()

        # State still persisted
        state = memory.get_planner_state()
        assert state["last_plan_at"] is not None

    def test_cycle_handles_invalid_json(self, planner, memory, mm, llm):
        """Cycle handles malformed LLM response."""
        llm.call_heavy.return_value = "This is not JSON"

        planner._run_cycle()

        mm.send_to_channel.assert_not_called()
        state = memory.get_planner_state()
        assert state["last_plan_at"] is not None

    def test_planner_log_written(self, planner, memory):
        """Planner log is appended after cycle."""
        planner._run_cycle()

        log = memory.read_jsonl("tasks/planner_log.jsonl")
        assert len(log) >= 1
        assert any(entry.get("event") == "cycle_complete" for entry in log)
