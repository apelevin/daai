import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.agent import Agent, ProcessResult, THREAD_TRACKING_TYPES
from src.memory import Memory


class FakeLLM:
    def call_cheap(self, system, user, **kw):
        return json.dumps({"type": "contract_discussion", "entity": "headcount"})

    def call_heavy(self, system, user, **kw):
        return "ok"

    def call_with_tools(self, **kw):
        return "tool reply"


class FakeMM:
    bot_user_id = "bot123"

    def send_dm(self, *a, **kw):
        return None

    def get_user_info(self, uid):
        return {"username": "testuser", "display_name": "Test User"}

    def get_thread(self, root_id):
        return [
            {"id": "p1", "user_id": "u1", "message": "first message"},
            {"id": "p2", "user_id": "bot123", "message": "bot reply"},
        ]

    def resolve_username(self, text):
        return None


# ── Memory tests ──────────────────────────────────────────────────────


class TestMemoryActiveThreads(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.tmpdir
        self.mem = Memory()

    def tearDown(self):
        os.environ.pop("DATA_DIR", None)

    def test_roundtrip(self):
        """set_active_thread then get_active_thread returns the root_post_id."""
        self.mem.set_active_thread("headcount", "root_abc")
        result = self.mem.get_active_thread("headcount")
        self.assertEqual(result, "root_abc")

    def test_missing_file_returns_none(self):
        """get_active_thread returns None when the file doesn't exist."""
        result = self.mem.get_active_thread("headcount")
        self.assertIsNone(result)

    def test_missing_contract_returns_none(self):
        """get_active_thread returns None for unknown contract."""
        self.mem.set_active_thread("headcount", "root_abc")
        result = self.mem.get_active_thread("other_contract")
        self.assertIsNone(result)

    def test_expired_entry_returns_none(self):
        """get_active_thread returns None when entry is older than TTL."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        data = {
            "threads": {
                "headcount": {
                    "root_post_id": "root_old",
                    "updated_at": old_ts,
                }
            }
        }
        self.mem.write_json(Memory._ACTIVE_THREADS_FILE, data)
        result = self.mem.get_active_thread("headcount")
        self.assertIsNone(result)

    def test_fresh_entry_returns_value(self):
        """get_active_thread returns value when entry is within TTL."""
        fresh_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        data = {
            "threads": {
                "headcount": {
                    "root_post_id": "root_fresh",
                    "updated_at": fresh_ts,
                }
            }
        }
        self.mem.write_json(Memory._ACTIVE_THREADS_FILE, data)
        result = self.mem.get_active_thread("headcount")
        self.assertEqual(result, "root_fresh")

    def test_update_overwrites(self):
        """set_active_thread overwrites previous entry."""
        self.mem.set_active_thread("headcount", "root_v1")
        self.mem.set_active_thread("headcount", "root_v2")
        result = self.mem.get_active_thread("headcount")
        self.assertEqual(result, "root_v2")

    def test_corrupt_data_returns_none(self):
        """get_active_thread handles corrupt JSON gracefully."""
        full = os.path.join(self.tmpdir, "tasks")
        os.makedirs(full, exist_ok=True)
        with open(os.path.join(full, "active_threads.json"), "w") as f:
            f.write("not json")
        result = self.mem.get_active_thread("headcount")
        self.assertIsNone(result)


# ── Agent tests ───────────────────────────────────────────────────────


class TestAgentProcessResult(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.tmpdir
        self.mem = Memory()
        self.mm = FakeMM()
        self.llm = FakeLLM()
        # Create required context files
        os.makedirs(os.path.join(self.tmpdir, "prompts"), exist_ok=True)
        self.mem.write_file("prompts/system_full.md", "system prompt")
        self.mem.write_file("prompts/router.md", "{}")

    def tearDown(self):
        os.environ.pop("DATA_DIR", None)

    def test_returns_process_result(self):
        """process_message returns a ProcessResult, not a plain string."""
        agent = Agent(self.llm, self.mem, self.mm)
        result = agent.process_message(
            username="testuser",
            message="Data Lead — @pavelpetrin",
            channel_type="channel",
            thread_context=None,
            post_id="p1",
        )
        self.assertIsInstance(result, ProcessResult)
        self.assertIn("tasks/roles.json", result.reply)

    def test_thread_reuse_top_level_message(self):
        """Top-level message about known contract reuses active thread."""
        # Pre-register an active thread
        self.mem.set_active_thread("headcount", "existing_root")

        agent = Agent(self.llm, self.mem, self.mm)
        result = agent.process_message(
            username="testuser",
            message="обсудим headcount",
            channel_type="channel",
            thread_context=None,
            post_id="new_post",
            root_id=None,  # top-level
        )
        self.assertIsInstance(result, ProcessResult)
        self.assertEqual(result.thread_root_id, "existing_root")

    def test_first_top_level_message_registers_thread(self):
        """First top-level message about a contract registers post_id as active thread."""
        agent = Agent(self.llm, self.mem, self.mm)
        result = agent.process_message(
            username="testuser",
            message="обсудим headcount",
            channel_type="channel",
            thread_context=None,
            post_id="first_post_id",
            root_id=None,  # top-level, no existing thread
        )
        self.assertIsInstance(result, ProcessResult)
        # post_id should be registered as the active thread
        stored = self.mem.get_active_thread("headcount")
        self.assertEqual(stored, "first_post_id")

    def test_no_entity_no_thread_redirect(self):
        """Message without entity → thread_root_id is None."""
        # Make LLM return a type without entity
        llm = FakeLLM()
        llm.call_cheap = lambda s, u, **kw: json.dumps({"type": "general_question", "entity": ""})
        llm.call_with_tools = lambda **kw: "general reply"

        agent = Agent(llm, self.mem, self.mm)
        result = agent.process_message(
            username="testuser",
            message="что вообще такое data contract?",
            channel_type="channel",
            thread_context=None,
            post_id="p1",
            root_id=None,
        )
        self.assertIsInstance(result, ProcessResult)
        self.assertIsNone(result.thread_root_id)

    def test_in_thread_message_updates_registry(self):
        """When user writes in a thread (root_id set), the registry is updated."""
        agent = Agent(self.llm, self.mem, self.mm)
        result = agent.process_message(
            username="testuser",
            message="уточним headcount",
            channel_type="channel",
            thread_context="previous context",
            post_id="p5",
            root_id="thread_root_99",
        )
        self.assertIsInstance(result, ProcessResult)
        # Thread should be registered
        stored = self.mem.get_active_thread("headcount")
        self.assertEqual(stored, "thread_root_99")

    def test_dm_skips_thread_lookup(self):
        """DM messages skip thread lookup even with entity."""
        agent = Agent(self.llm, self.mem, self.mm)
        self.mem.set_active_thread("headcount", "channel_root")

        result = agent.process_message(
            username="testuser",
            message="расскажи про headcount",
            channel_type="dm",
            thread_context=None,
            post_id="dm_post",
            root_id=None,
        )
        self.assertIsInstance(result, ProcessResult)
        # Should NOT redirect to channel thread
        self.assertIsNone(result.thread_root_id)


if __name__ == "__main__":
    unittest.main()
