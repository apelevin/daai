import os
import json
import tempfile
import unittest

from src.agent import Agent
from src.memory import Memory


class FakeLLM:
    def call_cheap(self, *a, **kw):
        return "{}"

    def call_heavy(self, *a, **kw):
        return "ok"


class FakeMM:
    def send_dm(self, *a, **kw):
        return None

    def resolve_username(self, text: str):
        # emulate display-name -> username mapping
        t = (text or "").strip().lower()
        if "никита" in t:
            return "korabovtsev"
        if "пав" in t:
            return "pavelpetrin"
        return None


class DisplayNameResolutionTest(unittest.TestCase):
    def test_russian_display_names_resolve_to_usernames(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["DATA_DIR"] = td
            mem = Memory()
            mem.write_json("contracts/index.json", {"contracts": []})

            agent = Agent(FakeLLM(), mem, FakeMM())
            msg = "Circle Lead — @Никита Корабовцев\nData Lead — @Павел Петрин"
            result = agent.process_message("pelevin", msg, "channel", None, None)

            self.assertIn("✅ Роли обновлены", result.reply)
            roles = mem.read_json("tasks/roles.json")
            self.assertEqual(roles["roles"]["circle_lead"], ["korabovtsev"])
            self.assertEqual(roles["roles"]["data_lead"], ["pavelpetrin"])


if __name__ == "__main__":
    unittest.main()
