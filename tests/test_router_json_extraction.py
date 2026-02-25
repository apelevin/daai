import unittest

from src.router import route


class FakeLLM:
    def __init__(self, raw: str):
        self.raw = raw

    def call_cheap(self, system, user, **kw):
        return self.raw


class FakeMemory:
    def read_file(self, path: str):
        return "{}"  # router prompt not relevant


class RouterJsonExtractionTest(unittest.TestCase):
    def test_parses_json_with_trailing_garbage(self):
        raw = (
            '{\n'
            '  "type": "contract_discussion",\n'
            '  "entity": "client_tier_segmentation",\n'
            '  "load_files": ["contracts/index.json"],\n'
            '  "model": "heavy"\n'
            '}\n\nSOME TRAILING TEXT'
        )
        llm = FakeLLM(raw)
        mem = FakeMemory()
        res = route(llm, mem, "u", "зафиксируй контракт client_tier_segmentation", "channel", None)
        self.assertEqual(res["type"], "contract_discussion")
        self.assertEqual(res["entity"], "client_tier_segmentation")
        self.assertEqual(res["model"], "heavy")

    def test_parses_markdown_codeblock(self):
        raw = "```json\n{\"type\":\"general_question\",\"entity\":null,\"load_files\":[],\"model\":\"heavy\"}\n```"
        llm = FakeLLM(raw)
        mem = FakeMemory()
        res = route(llm, mem, "u", "hi", "channel", None)
        self.assertEqual(res["type"], "general_question")


if __name__ == "__main__":
    unittest.main()
