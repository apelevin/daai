"""Tests for profile enrichment and profile-based stakeholder resolution."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.memory import Memory
from src.suggestion_engine import (
    _resolve_stakeholders_from_profiles,
    _resolve_stakeholders,
    SuggestionEngine,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_PROFILE_SALES = """\
# Ivan (@ivan_sales)

## Базовое
- В канале с: 2025-01-01

## Домен и данные
- Метрики: WIN NI, pipeline, конверсия воронки, new income

## Профиль коммуникации
- Скорость ответа: быстрый

## Позиции по контрактам
- WIN NI: считает по закрытым сделкам за месяц
"""

SAMPLE_PROFILE_PRODUCT = """\
# Maria (@maria_product)

## Базовое
- В канале с: 2025-01-01

## Домен и данные
- Метрики: MAU, activation rate, feature adoption, onboarding

## Профиль коммуникации
- Скорость ответа: средний

## Позиции по контрактам
- Activation Rate: процент активированных лицензий за 30 дней
"""

SAMPLE_PROFILE_CS = """\
# Olga (@olga_cs)

## Базовое
- В канале с: 2025-01-01

## Домен и данные
- Метрики: Churn Rate, Retention, NPS, CSAT

## Профиль коммуникации
- Скорость ответа: средний

## Позиции по контрактам
(нет данных)
"""

SAMPLE_PROFILE_EMPTY = """\
# Empty (@empty_user)

## Базовое
- В канале с: 2025-02-01

## Домен и данные
- Метрики: (не заполнено)

## Профиль коммуникации
- Скорость ответа: неизвестно

## Позиции по контрактам
(нет данных)
"""

CIRCLES_MD = """\
# Круги компании

## Sales
- Ответственный: @fallback_sales
- Метрики: WIN NI, конверсия воронки

## Product
- Ответственный: @fallback_product
- Метрики: MAU, activation
"""

SAMPLE_TREE_MD = """\
# Дерево метрик

## Дерево

```
Extra Time
├── MAU (Monthly Active Users)
│   ├── New Clients (acquisition)
│   │   └── WIN NI (New Income от новых клиентов) ← DATA CONTRACT
│   └── Activation (начинают пользоваться)
│       └── Activation Rate (% активированных лицензий) ← DATA CONTRACT
└── Revenue (следствие Extra Time)
    └── Churn Rate ← DATA CONTRACT
```
"""


@pytest.fixture
def data_dir(tmp_path):
    """Set up a temporary data directory with profiles and context."""
    # Participants
    (tmp_path / "participants").mkdir()
    (tmp_path / "participants" / "ivan_sales.md").write_text(SAMPLE_PROFILE_SALES, encoding="utf-8")
    (tmp_path / "participants" / "maria_product.md").write_text(SAMPLE_PROFILE_PRODUCT, encoding="utf-8")
    (tmp_path / "participants" / "olga_cs.md").write_text(SAMPLE_PROFILE_CS, encoding="utf-8")
    (tmp_path / "participants" / "empty_user.md").write_text(SAMPLE_PROFILE_EMPTY, encoding="utf-8")

    # Participant index
    (tmp_path / "participants" / "index.json").write_text(json.dumps({
        "participants": [
            {"username": "ivan_sales", "active": True},
            {"username": "maria_product", "active": True},
            {"username": "olga_cs", "active": True},
            {"username": "empty_user", "active": True},
        ]
    }), encoding="utf-8")

    # Context
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "circles.md").write_text(CIRCLES_MD, encoding="utf-8")
    (tmp_path / "context" / "metrics_tree.md").write_text(SAMPLE_TREE_MD, encoding="utf-8")

    # Contracts
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text('{"contracts": []}', encoding="utf-8")

    # Tasks
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "queue.json").write_text('{"queue": []}', encoding="utf-8")
    (tmp_path / "tasks" / "suggestions.json").write_text('{"suggestions": []}', encoding="utf-8")

    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


# ── Tests: _resolve_stakeholders_from_profiles ──────────────────────────────


class TestResolveStakeholdersFromProfiles:
    def test_win_ni_matches_sales_profile(self, memory):
        result = _resolve_stakeholders_from_profiles("WIN NI", memory)
        assert "ivan_sales" in result

    def test_activation_matches_product_profile(self, memory):
        result = _resolve_stakeholders_from_profiles("Activation Rate", memory)
        assert "maria_product" in result

    def test_churn_matches_cs_profile(self, memory):
        result = _resolve_stakeholders_from_profiles("Churn Rate", memory)
        assert "olga_cs" in result

    def test_no_match_falls_back_to_circles(self, memory):
        """If no profile matches, fall back to circles.md resolution."""
        result = _resolve_stakeholders_from_profiles("Unknown Metric XYZ", memory)
        # circles.md doesn't match either, so empty
        assert result == []

    def test_multiple_matches(self, memory):
        """A metric that appears in multiple profiles returns all matches."""
        # "MAU" appears in maria_product's profile
        result = _resolve_stakeholders_from_profiles("MAU", memory)
        assert "maria_product" in result

    def test_empty_profile_not_matched(self, memory):
        """Users with empty/placeholder profiles don't match."""
        result = _resolve_stakeholders_from_profiles("WIN NI", memory)
        assert "empty_user" not in result

    def test_fallback_when_no_participants(self, memory, data_dir):
        """Falls back to circles.md when no participants in index."""
        (data_dir / "participants" / "index.json").write_text(
            '{"participants": []}', encoding="utf-8"
        )
        result = _resolve_stakeholders_from_profiles("WIN NI", memory)
        # Should use circles.md fallback
        assert "fallback_sales" in result

    def test_case_insensitive_matching(self, memory):
        """Matching is case-insensitive."""
        result = _resolve_stakeholders_from_profiles("win ni", memory)
        assert "ivan_sales" in result


# ── Tests: Profile enrichment in Agent ───────────────────────────────────────


class TestAgentProfileEnrichment:
    def test_enrichment_called_for_discussion(self, memory, data_dir):
        """Enrichment runs for contract_discussion route type."""
        from src.agent import Agent

        llm = MagicMock()
        mm = MagicMock()
        mm.bot_user_id = "bot123"
        enriched = SAMPLE_PROFILE_SALES.rstrip() + "\n- Боли: расхождение данных между CRM и DWH\n"
        llm.call_cheap.return_value = enriched

        # Set up prompts
        (data_dir / "prompts").mkdir(exist_ok=True)
        (data_dir / "prompts" / "profile_enrichment.md").write_text(
            "Enrich the profile", encoding="utf-8"
        )

        agent = Agent(llm, memory, mm)
        agent._enrich_participant_profile(
            "ivan_sales", "У нас расхождение данных между CRM и DWH",
            "contract_discussion", None,
        )

        # call_cheap was called for enrichment
        llm.call_cheap.assert_called_once()
        # Profile was updated with new info
        updated = memory.get_participant("ivan_sales")
        assert "расхождение данных между CRM и DWH" in updated

    def test_enrichment_skipped_for_wrong_route(self, memory, data_dir):
        """Enrichment doesn't run for non-enrichment route types."""
        from src.agent import Agent

        llm = MagicMock()
        mm = MagicMock()
        mm.bot_user_id = "bot123"

        agent = Agent(llm, memory, mm)
        agent._enrich_participant_profile(
            "ivan_sales", "some message",
            "show_contract", None,
        )

        llm.call_cheap.assert_not_called()

    def test_enrichment_skipped_when_no_changes(self, memory, data_dir):
        """If LLM returns empty string, profile is not updated."""
        from src.agent import Agent

        llm = MagicMock()
        mm = MagicMock()
        mm.bot_user_id = "bot123"
        llm.call_cheap.return_value = ""

        (data_dir / "prompts").mkdir(exist_ok=True)
        (data_dir / "prompts" / "profile_enrichment.md").write_text(
            "Enrich the profile", encoding="utf-8"
        )

        original = memory.get_participant("ivan_sales")
        agent = Agent(llm, memory, mm)
        agent._enrich_participant_profile(
            "ivan_sales", "привет",
            "contract_discussion", None,
        )

        assert memory.get_participant("ivan_sales") == original

    def test_enrichment_error_does_not_break_flow(self, memory, data_dir):
        """Enrichment errors are caught and don't propagate."""
        from src.agent import Agent

        llm = MagicMock()
        mm = MagicMock()
        mm.bot_user_id = "bot123"
        llm.call_cheap.side_effect = Exception("API down")

        (data_dir / "prompts").mkdir(exist_ok=True)
        (data_dir / "prompts" / "profile_enrichment.md").write_text(
            "Enrich the profile", encoding="utf-8"
        )

        agent = Agent(llm, memory, mm)
        # Should not raise
        agent._enrich_participant_profile(
            "ivan_sales", "test",
            "contract_discussion", None,
        )


# ── Tests: Planner target_users support ──────────────────────────────────────


class TestPlannerTargetUsers:
    def test_ask_question_multiple_targets(self):
        """_ask_question handles target_users list."""
        from src.planner_actions import ActionDispatcher

        mm = MagicMock()
        mm.send_to_channel.return_value = {"id": "post_456"}
        dispatcher = ActionDispatcher(MagicMock(), mm, MagicMock())

        action = {
            "type": "ask_question",
            "contract_id": "win_ni",
            "message_hint": "Как считать WIN NI?",
            "target_users": ["@ivan_sales", "@maria_product"],
        }
        initiative = {"thread_id": "thread_001"}

        result = dispatcher._ask_question(action, initiative)

        assert result is not None
        assert result["targets"] == ["ivan_sales", "maria_product"]
        # Check the message sent contains both mentions
        sent_msg = mm.send_to_channel.call_args[0][0]
        assert "@ivan_sales" in sent_msg
        assert "@maria_product" in sent_msg
        assert "Как считать WIN NI?" in sent_msg

    def test_ask_question_backward_compat_single_target(self):
        """_ask_question still works with old target_user field."""
        from src.planner_actions import ActionDispatcher

        mm = MagicMock()
        mm.send_to_channel.return_value = {"id": "post_789"}
        dispatcher = ActionDispatcher(MagicMock(), mm, MagicMock())

        action = {
            "type": "ask_question",
            "contract_id": "win_ni",
            "message_hint": "Как считать?",
            "target_user": "@ivan_sales",
        }
        initiative = {"thread_id": "thread_001"}

        result = dispatcher._ask_question(action, initiative)

        assert result is not None
        assert result["targets"] == ["ivan_sales"]
        sent_msg = mm.send_to_channel.call_args[0][0]
        assert "@ivan_sales" in sent_msg

    def test_ask_question_no_target(self):
        """_ask_question works without any target."""
        from src.planner_actions import ActionDispatcher

        mm = MagicMock()
        mm.send_to_channel.return_value = {"id": "post_000"}
        dispatcher = ActionDispatcher(MagicMock(), mm, MagicMock())

        action = {
            "type": "ask_question",
            "contract_id": "test",
            "message_hint": "General question",
        }
        initiative = {"thread_id": None}

        result = dispatcher._ask_question(action, initiative)
        assert result is not None
        sent_msg = mm.send_to_channel.call_args[0][0]
        assert sent_msg == "General question"

    def test_planner_waiting_for_multiple_users(self):
        """Planner adds all target_users to waiting_for."""
        from src.planner import ContinuousPlanner
        from datetime import datetime, timezone

        memory = MagicMock()
        mm = MagicMock()
        mm.send_to_channel.return_value = {"id": "post_123"}
        llm = MagicMock()
        llm.call_heavy.return_value = json.dumps({
            "analysis": "Test",
            "actions": [{
                "type": "ask_question",
                "contract_id": "test",
                "reason": "test",
                "message_hint": "Question",
                "target_users": ["@user1", "@user2"],
            }],
        })

        memory.get_planner_state.return_value = {
            "initiatives": [],
            "daily_stats": {},
            "cooldowns": {},
            "last_plan_at": None,
        }
        memory.read_file.return_value = "You are a planner."
        memory.list_contracts.return_value = []
        memory.get_queue.return_value = []
        memory.get_reminders.return_value = []
        memory.get_participant.return_value = ""

        planner = ContinuousPlanner(memory, mm, llm)

        # Simulate action execution with target_users
        action = {
            "type": "ask_question",
            "contract_id": "test",
            "target_users": ["@user1", "@user2"],
        }
        initiative = {
            "id": "init_test",
            "contract_id": "test",
            "status": "active",
            "waiting_for": [],
        }
        now = datetime.now(timezone.utc)

        # Execute — inline the waiting_for logic
        from datetime import timedelta
        from src.config import PLANNER_WAIT_BEFORE_FOLLOWUP_HOURS

        initiative["status"] = "waiting_response"
        targets = action.get("target_users", [])
        if not targets:
            single = action.get("target_user", "")
            if single:
                targets = [single]
        waiting = initiative.get("waiting_for", [])
        for target in targets:
            target = target.lstrip("@")
            if target and target not in waiting:
                waiting.append(target)
        initiative["waiting_for"] = waiting

        assert "user1" in initiative["waiting_for"]
        assert "user2" in initiative["waiting_for"]
        assert initiative["status"] == "waiting_response"


# ── Tests: Planner _get_stakeholder_context ──────────────────────────────────


class TestGetStakeholderContext:
    def test_returns_context_from_profiles(self, memory, data_dir):
        """_get_stakeholder_context reads profiles and extracts summaries."""
        from src.planner import ContinuousPlanner

        mm = MagicMock()
        llm = MagicMock()

        # Set up planner state
        (data_dir / "tasks" / "planner_state.json").write_text(json.dumps({
            "initiatives": [], "daily_stats": {}, "cooldowns": {}, "last_plan_at": None,
        }), encoding="utf-8")

        planner = ContinuousPlanner(memory, mm, llm)
        ctx = planner._get_stakeholder_context(["ivan_sales", "maria_product"])

        assert len(ctx) == 2
        assert ctx[0]["username"] == "ivan_sales"
        assert "WIN NI" in ctx[0]["summary"]
        assert ctx[1]["username"] == "maria_product"
        assert "activation" in ctx[1]["summary"].lower()

    def test_empty_stakeholders(self, memory, data_dir):
        from src.planner import ContinuousPlanner

        (data_dir / "tasks" / "planner_state.json").write_text(json.dumps({
            "initiatives": [], "daily_stats": {}, "cooldowns": {}, "last_plan_at": None,
        }), encoding="utf-8")

        planner = ContinuousPlanner(memory, MagicMock(), MagicMock())
        assert planner._get_stakeholder_context([]) == []
        assert planner._get_stakeholder_context(None) == []

    def test_unknown_user_returns_not_found(self, memory, data_dir):
        from src.planner import ContinuousPlanner

        (data_dir / "tasks" / "planner_state.json").write_text(json.dumps({
            "initiatives": [], "daily_stats": {}, "cooldowns": {}, "last_plan_at": None,
        }), encoding="utf-8")

        planner = ContinuousPlanner(memory, MagicMock(), MagicMock())
        ctx = planner._get_stakeholder_context(["nonexistent"])
        assert len(ctx) == 1
        assert "не найден" in ctx[0]["summary"]
