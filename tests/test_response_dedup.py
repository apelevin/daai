"""Tests for response deduplication in Listener."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.agent import Agent, ProcessResult
from src.listener import Listener
from src.memory import Memory


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text(
        '{"contracts": []}', encoding="utf-8"
    )
    (tmp_path / "tasks").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "router.md").write_text("{}", encoding="utf-8")
    (tmp_path / "prompts" / "system_full.md").write_text("system", encoding="utf-8")
    (tmp_path / "prompts" / "system_short.md").write_text("short", encoding="utf-8")
    (tmp_path / "context").mkdir()
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
    mock = MagicMock()
    mock.bot_user_id = "bot123"
    mock.channel_id = "chan456"
    mock.get_user_info.return_value = {
        "user_id": "user789", "username": "testuser",
        "display_name": "Test User", "email": "",
    }
    mock.send_to_channel.return_value = {"id": "reply_001"}
    mock.send_dm.return_value = {"id": "dm_001"}
    return mock


@pytest.fixture
def agent(memory, mm):
    llm = MagicMock()
    return Agent(llm_client=llm, memory=memory, mattermost_client=mm)


@pytest.fixture
def listener(agent, mm):
    return Listener(agent=agent, mattermost_client=mm)


def _make_event(message, post_id, channel_id="chan456", user_id="user789",
                root_id="", channel_type="O"):
    post = {
        "id": post_id, "channel_id": channel_id,
        "user_id": user_id, "message": message,
        "root_id": root_id, "type": "",
    }
    return {
        "event": "posted",
        "data": {"post": json.dumps(post), "channel_type": channel_type},
    }


class TestResponseDedup:
    def test_identical_reply_suppressed(self, listener, agent, mm):
        """Same reply to same thread within dedup window should be suppressed."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Одинаковый ответ", thread_root_id=None)
        )

        # First message in thread — should be sent
        listener._handle_event(_make_event("msg1", "p1", root_id="thread_root"))
        assert mm.send_to_channel.call_count == 1

        # Second message in same thread, same reply — should be suppressed
        listener._handle_event(_make_event("msg2", "p2", root_id="thread_root"))
        assert mm.send_to_channel.call_count == 1  # still 1

    def test_different_reply_not_suppressed(self, listener, agent, mm):
        """Different replies to same thread should both be sent."""
        replies = iter(["Ответ 1", "Ответ 2 совсем другой"])
        agent.process_message = MagicMock(
            side_effect=lambda **kw: ProcessResult(reply=next(replies), thread_root_id=None)
        )

        listener._handle_event(_make_event("msg1", "p1"))
        assert mm.send_to_channel.call_count == 1

        listener._handle_event(_make_event("msg2", "p2"))
        assert mm.send_to_channel.call_count == 2

    def test_same_reply_different_threads_not_suppressed(self, listener, agent, mm):
        """Same reply to different threads should both be sent."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Одинаковый ответ", thread_root_id=None)
        )

        listener._handle_event(_make_event("msg1", "p1", root_id="thread_a"))
        assert mm.send_to_channel.call_count == 1

        listener._handle_event(_make_event("msg2", "p2", root_id="thread_b"))
        assert mm.send_to_channel.call_count == 2

    @patch("src.listener.RESPONSE_DEDUP_WINDOW_SECONDS", 0)
    def test_expired_dedup_allows_same_reply(self, listener, agent, mm):
        """After dedup window expires, same reply should be sent again."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="Одинаковый ответ", thread_root_id=None)
        )

        listener._handle_event(_make_event("msg1", "p1"))
        assert mm.send_to_channel.call_count == 1

        # Window is 0 seconds, so it should have expired
        time.sleep(0.01)
        listener._handle_event(_make_event("msg2", "p2"))
        assert mm.send_to_channel.call_count == 2

    def test_dm_dedup_works(self, listener, agent, mm):
        """Dedup should also work for DM replies."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="DM ответ", thread_root_id=None)
        )

        listener._handle_event(_make_event("msg1", "p1", channel_id="dm_chan",
                                           channel_type="D", root_id="dm_thread"))
        assert mm.send_dm.call_count == 1

        listener._handle_event(_make_event("msg2", "p2", channel_id="dm_chan",
                                           channel_type="D", root_id="dm_thread"))
        assert mm.send_dm.call_count == 1  # suppressed

    def test_is_duplicate_reply_method(self, listener):
        """Unit test for _is_duplicate_reply."""
        assert listener._is_duplicate_reply("thread1", "hello world") is False
        assert listener._is_duplicate_reply("thread1", "hello world") is True
        assert listener._is_duplicate_reply("thread1", "different text") is False
        assert listener._is_duplicate_reply("thread2", "hello world") is False

    def test_empty_reply_not_sent(self, listener, agent, mm):
        """Empty replies should not be sent at all."""
        agent.process_message = MagicMock(
            return_value=ProcessResult(reply="", thread_root_id=None)
        )
        listener._handle_event(_make_event("msg1", "p1"))
        mm.send_to_channel.assert_not_called()
