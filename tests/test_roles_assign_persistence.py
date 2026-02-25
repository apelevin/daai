import os
import json
import tempfile
import unittest

from src.agent import Agent
from src.memory import Memory
from src.router import route


class FakeLLM:
    def call_cheap(self, system, user, **kw):
        return "{}"

    def call_heavy(self, system, user, **kw):
        return "ok"


class FakeMM:
    def send_dm(self, *a, **kw):
        return None

    def resolve_username(self, text: str):
        # simulate mapping display names -> usernames
        m = {
            "павел": "pavelpetrin",
            "павелпетрин": "pavelpetrin",
            "павелпетрин": "pavelpetrin",
            "никита": "korabovtsev",
        }
        t = (text or "").strip().lower().replace(" ", "")
        return m.get(t)


class FakeMemoryForRouter:
    def read_file(self, path: str):
        return "{}"


class RolesAssignPersistenceTest(unittest.TestCase):
    def test_router_detects_assignments(self):
        llm = FakeLLM()
        mem = FakeMemoryForRouter()
        msg = "Data Lead — @pavelpetrin\nCircle Lead - @korabovtsev"
        r = route(llm, mem, "u", msg, "channel", None)
        self.assertEqual(r["type"], "roles_assign")
        self.assertIn("data_lead:pavelpetrin", r["entity"])
        self.assertIn("circle_lead:korabovtsev", r["entity"])

    def test_agent_persists_roles_json(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["DATA_DIR"] = td
            mem = Memory()
            mem.write_json("tasks/roles.json", {"roles": {}})
            agent = Agent(FakeLLM(), mem, FakeMM())

            result = agent.process_message("pelevin", "Data Lead — @pavelpetrin\nCircle Lead — @korabovtsev", "channel", None, None)
            self.assertIn("tasks/roles.json", result.reply)

            roles = mem.read_json("tasks/roles.json")
            self.assertEqual(roles["roles"]["data_lead"], ["pavelpetrin"])
            self.assertEqual(roles["roles"]["circle_lead"], ["korabovtsev"])


if __name__ == "__main__":
    unittest.main()
