"""Integration test: Listener._handle_posted → Agent.process_message → reply.

Tests the full flow from a WebSocket 'posted' event to sending a reply,
with mocked Mattermost and LLM clients.
"""

import json
import threading
from unittest.mock import MagicMock, patch, call

import pytest

from src.memory import Memory
from src.agent import Agent, ProcessResult
from src.listener import Listener


@pytest.fixture
def data_dir(tmp_path):
    """Minimal data directory."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text(
        '{"contracts": []}', encoding="utf-8"
    )
    (tmp_path / "tasks").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "router.md").write_text("router prompt", encoding="utf-8")
    (tmp_path / "prompts" / "system_full.md").write_text("system prompt", encoding="utf-8")
    (tmp_path / "prompts" / "system_short.md").write_text("short prompt", encoding="utf-8")
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "company.md").write_text("Company info", encoding="utf-8")
    (tmp_path / "context" / "metrics_tree.md").write_text("## Дерево\n```\nRoot\n```", encoding="utf-8")
    (tmp_path / "participants").mkdir()
    (tmp_path / "participants" / "index.json").write_text(
        '{"participants": [{"username": "testuser", "active": true, "onboarded": true}]}',
        encoding="utf-8",
    )
    (tmp_path / "participants" / "testuser.md").write_text("# testuser", encoding="utf-8")
    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


@pytest.fixture
def mm():
    """Mock MattermostClient."""
    mock = MagicMock()
    mock.bot_user_id = "bot123"
    mock.channel_id = "chan456"
    mock.get_user_info.return_value = {
        "user_id": "user789",
        "username": "testuser",
        "display_name": "Test User",
        "email": "test@example.com",
    }
    mock.send_to_channel.return_value = {"id": "reply_post_id"}
    mock.send_dm.return_value = {"id": "dm_reply_id"}
    return mock


@pytest.fixture
def agent(memory, mm):
    """Agent with mocked LLM."""
    llm = MagicMock()
    return Agent(llm_client=llm, memory=memory, mattermost_client=mm)


@pytest.fixture
def listener(agent, mm):
    return Listener(agent=agent, mattermost_client=mm)


def _make_posted_event(message, post_id="post_001", channel_id="chan456",
                       user_id="user789", root_id="", channel_type="O"):
    """Create a mock WebSocket 'posted' event."""
    post = {
        "id": post_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "message": message,
        "root_id": root_id,
        "type": "",
    }
    return {
        "event": "posted",
        "data": {
            "post": json.dumps(post),
            "channel_type": channel_type,
        },
    }


class TestListenerIntegration:
    def test_channel_message_processed_and_replied(self, listener, agent, mm):
        """Full flow: posted event → agent processes → reply sent to channel."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Ответ бота", thread_root_id=None)
        )

        event = _make_posted_event("покажи контракт test")
        listener._handle_event(event)

        agent.process_message.assert_called_once()
        call_kwargs = agent.process_message.call_args
        assert call_kwargs[1]["username"] == "testuser"
        assert call_kwargs[1]["message"] == "покажи контракт test"
        assert call_kwargs[1]["channel_type"] == "channel"

        mm.send_to_channel.assert_called_once()
        assert mm.send_to_channel.call_args[0][0] == "Ответ бота"

    def test_dm_message_processed(self, listener, agent, mm):
        """DM messages are processed and replied via send_dm."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="DM ответ")
        )

        event = _make_posted_event("привет", channel_id="dm_chan", channel_type="D")
        listener._handle_event(event)

        agent.process_message.assert_called_once()
        mm.send_dm.assert_called_once()
        assert mm.send_dm.call_args[0][1] == "DM ответ"

    def test_bot_own_messages_ignored(self, listener, agent, mm):
        """Bot's own messages should not be processed."""
        agent.process_message = MagicMock()

        event = _make_posted_event("test", user_id="bot123")
        listener._handle_event(event)

        agent.process_message.assert_not_called()

    def test_empty_message_ignored(self, listener, agent, mm):
        """Empty messages should not be processed."""
        agent.process_message = MagicMock()

        event = _make_posted_event("")
        listener._handle_event(event)

        agent.process_message.assert_not_called()

    def test_dedup_prevents_double_processing(self, listener, agent, mm):
        """Same post_id should only be processed once."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Ответ")
        )

        event = _make_posted_event("test", post_id="dup_001")
        listener._handle_event(event)
        listener._handle_event(event)

        assert agent.process_message.call_count == 1

    def test_different_posts_both_processed(self, listener, agent, mm):
        """Different post_ids should both be processed."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Ответ")
        )

        listener._handle_event(_make_posted_event("msg1", post_id="p1"))
        listener._handle_event(_make_posted_event("msg2", post_id="p2"))

        assert agent.process_message.call_count == 2

    def test_other_channel_ignored(self, listener, agent, mm):
        """Messages from other channels should be ignored."""
        agent.process_message = MagicMock()

        event = _make_posted_event("test", channel_id="other_chan", channel_type="O")
        listener._handle_event(event)

        agent.process_message.assert_not_called()

    def test_agent_error_returns_fallback(self, listener, agent, mm):
        """When agent raises, listener sends error fallback."""
        agent.process_message = MagicMock(side_effect=Exception("LLM crashed"))

        event = _make_posted_event("test")
        listener._handle_event(event)

        mm.send_to_channel.assert_called_once()
        msg = mm.send_to_channel.call_args[0][0]
        assert "ошибка" in msg.lower()

    def test_empty_reply_not_sent(self, listener, agent, mm):
        """When agent returns empty reply, nothing is sent."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="")
        )

        event = _make_posted_event("test")
        listener._handle_event(event)

        mm.send_to_channel.assert_not_called()
        mm.send_dm.assert_not_called()

    def test_inflight_cleanup_on_early_return(self, listener, agent, mm):
        """Even if processing returns early, inflight set is cleaned up."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="")
        )

        event = _make_posted_event("test", post_id="cleanup_test")
        listener._handle_event(event)

        # Post should be in seen (not inflight)
        assert "cleanup_test" in listener._seen_post_ids
        assert "cleanup_test" not in listener._inflight_post_ids

    def test_thread_context_passed(self, listener, agent, mm):
        """When root_id is set, thread context is built and passed."""
        mm.get_thread.return_value = [
            {"id": "root_post", "user_id": "user789", "message": "Начинаем", "create_at": 1000},
            {"id": "reply_post", "user_id": "bot123", "message": "Хорошо", "create_at": 2000},
        ]

        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Ответ")
        )

        event = _make_posted_event("продолжим", post_id="p3", root_id="root_post")
        listener._handle_event(event)

        call_kwargs = agent.process_message.call_args[1]
        assert call_kwargs["thread_context"] is not None
        assert "Начинаем" in call_kwargs["thread_context"]
