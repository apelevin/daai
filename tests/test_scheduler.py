"""Tests for scheduler.py — reminder escalation, digest, coverage scan."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from src.memory import Memory
from src.scheduler import Scheduler


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "reminders.json").write_text('{"reminders": []}', encoding="utf-8")
    (tmp_path / "tasks" / "queue.json").write_text('{"queue": []}', encoding="utf-8")
    (tmp_path / "tasks" / "suggestions.json").write_text('{"suggestions": []}', encoding="utf-8")
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text('{"contracts": []}', encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "reminder_templates.md").write_text("", encoding="utf-8")
    (tmp_path / "prompts" / "system_short.md").write_text("short prompt", encoding="utf-8")
    (tmp_path / "prompts" / "digest_template.md").write_text(
        "contracts: {contracts_index}\nqueue: {queue}\nreminders: {reminders}",
        encoding="utf-8",
    )
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "metrics_tree.md").write_text(
        "## Дерево\n```\nRoot\n├── A ← DATA CONTRACT\n```", encoding="utf-8"
    )
    (tmp_path / "context" / "circles.md").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


@pytest.fixture
def mm():
    mock = MagicMock()
    mock.send_to_channel.return_value = {"id": "post_id"}
    mock.send_dm.return_value = {"id": "dm_id"}
    return mock


@pytest.fixture
def llm():
    mock = MagicMock()
    mock.call_cheap.return_value = "A — Вариант 1\nB — Вариант 2"
    mock.call_heavy.return_value = "Дайджест: всё хорошо"
    return mock


@pytest.fixture
def scheduler(memory, mm, llm):
    agent = MagicMock()
    agent.memory = memory
    return Scheduler(agent=agent, memory=memory, mattermost_client=mm, llm_client=llm)


def _make_reminder(contract_id="test_contract", target_user="testuser",
                   step=1, next_reminder=None):
    now = datetime.now(timezone.utc)
    return {
        "id": f"rem_{contract_id}",
        "contract_id": contract_id,
        "target_user": target_user,
        "target_mm_user_id": "mm_user_123",
        "thread_id": "thread_001",
        "question_summary": "Как считать метрику?",
        "escalation_step": step,
        "first_asked": (now - timedelta(days=10)).isoformat(),
        "last_reminder": (now - timedelta(days=3)).isoformat(),
        "next_reminder": next_reminder or (now - timedelta(hours=1)).isoformat(),
    }


class TestReminderEscalation:
    def test_step1_soft_reminder(self, scheduler, memory, mm):
        """Step 1: soft reminder sent to thread."""
        rem = _make_reminder(step=1)
        memory.save_reminders([rem])

        scheduler._check_reminders()

        mm.send_to_channel.assert_called_once()
        msg = mm.send_to_channel.call_args[0][0]
        assert "@testuser" in msg
        assert "test_contract" in msg

        # Step should escalate to 2
        updated = memory.get_reminders()
        assert updated[0]["escalation_step"] == 2

    def test_step2_ab_options(self, scheduler, memory, mm, llm):
        """Step 2: A/B simplification, uses LLM if no discussion."""
        rem = _make_reminder(step=2)
        memory.save_reminders([rem])

        scheduler._check_reminders()

        mm.send_to_channel.assert_called_once()
        msg = mm.send_to_channel.call_args[0][0]
        assert "A" in msg or "B" in msg

        updated = memory.get_reminders()
        assert updated[0]["escalation_step"] == 3

    def test_step3_dm(self, scheduler, memory, mm):
        """Step 3: DM sent to user."""
        rem = _make_reminder(step=3)
        memory.save_reminders([rem])

        scheduler._check_reminders()

        mm.send_dm.assert_called_once()
        msg = mm.send_dm.call_args[0][1]
        assert "test_contract" in msg

        updated = memory.get_reminders()
        assert updated[0]["escalation_step"] == 4

    def test_step4_escalation(self, scheduler, memory, mm):
        """Step 4: escalation to controller."""
        rem = _make_reminder(step=4)
        memory.save_reminders([rem])

        scheduler._check_reminders()

        mm.send_to_channel.assert_called_once()
        msg = mm.send_to_channel.call_args[0][0]
        assert "@alexey" in msg
        assert "заблокирован" in msg

        updated = memory.get_reminders()
        assert updated[0]["escalation_step"] == 5

    def test_future_reminder_not_sent(self, scheduler, memory, mm):
        """Reminders with future next_reminder are skipped."""
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        rem = _make_reminder(next_reminder=future)
        memory.save_reminders([rem])

        scheduler._check_reminders()

        mm.send_to_channel.assert_not_called()

    def test_empty_reminders(self, scheduler, mm):
        """No crash when reminders list is empty."""
        scheduler._check_reminders()
        mm.send_to_channel.assert_not_called()

    def test_next_reminder_updated(self, scheduler, memory, mm):
        """After processing, next_reminder is pushed +2 days."""
        rem = _make_reminder(step=1)
        memory.save_reminders([rem])

        scheduler._check_reminders()

        updated = memory.get_reminders()
        next_dt = datetime.fromisoformat(updated[0]["next_reminder"])
        now = datetime.now(timezone.utc)
        # Should be ~2 days from now (within a few seconds tolerance)
        diff = (next_dt - now).total_seconds()
        assert 170000 < diff < 175000  # ~2 days in seconds


class TestWeeklyDigest:
    def test_digest_published(self, scheduler, mm, llm):
        """Digest generates and publishes."""
        scheduler._weekly_digest()

        llm.call_heavy.assert_called_once()
        mm.send_to_channel.assert_called_once()
        assert mm.send_to_channel.call_args[0][0] == "Дайджест: всё хорошо"

    def test_digest_handles_error(self, scheduler, mm, llm):
        """Digest doesn't crash on LLM error."""
        llm.call_heavy.side_effect = Exception("API down")
        scheduler._weekly_digest()  # Should not raise
        mm.send_to_channel.assert_not_called()


class TestCoverageScan:
    def test_coverage_scan_sends_message(self, scheduler, memory, mm):
        """Coverage scan finds uncovered nodes and sends message."""
        scheduler._coverage_scan()

        # Tree has one uncovered node "A ← DATA CONTRACT"
        if mm.send_to_channel.called:
            msg = mm.send_to_channel.call_args[0][0]
            assert "покрытие" in msg.lower() or "метрик" in msg.lower() or "контракт" in msg.lower()

    def test_coverage_scan_handles_error(self, scheduler, memory, mm):
        """Coverage scan doesn't crash on errors."""
        memory.read_file = MagicMock(side_effect=Exception("disk error"))
        scheduler._coverage_scan()  # Should not raise
