"""Tests for LLMClient.call_with_tools() — mock agentic loop."""

import json
import unittest
from unittest.mock import MagicMock, patch


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]
        self.usage = FakeUsage()


class CallWithToolsTest(unittest.TestCase):
    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def _make_client(self):
        with patch("openai.OpenAI"):
            from src.llm_client import LLMClient
            client = LLMClient()
        return client

    def test_no_tool_calls_returns_text(self):
        """LLM returns text without tool calls — should return immediately."""
        client = self._make_client()
        msg = FakeMessage(content="Привет! Чем могу помочь?")
        client.client.chat.completions.create = MagicMock(return_value=FakeResponse(msg))

        result = client.call_with_tools(
            system_prompt="system",
            user_message="привет",
            tools=[],
            tool_executor=lambda name, args: {"error": "should not be called"},
        )
        self.assertEqual(result, "Привет! Чем могу помочь?")

    def test_single_tool_call_then_text(self):
        """LLM calls one tool, gets result, then returns text."""
        client = self._make_client()

        # First call: LLM wants to call read_draft
        tc = FakeToolCall("tc_1", "read_draft", {"contract_id": "test"})
        msg1 = FakeMessage(content=None, tool_calls=[tc])

        # Second call: LLM returns final text
        msg2 = FakeMessage(content="Черновик test содержит определение метрики.")

        client.client.chat.completions.create = MagicMock(
            side_effect=[FakeResponse(msg1), FakeResponse(msg2)]
        )

        calls = []
        def executor(name, args):
            calls.append((name, args))
            return {"contract_id": "test", "content": "# Draft content"}

        result = client.call_with_tools(
            system_prompt="system",
            user_message="что в черновике test?",
            tools=[{"type": "function", "function": {"name": "read_draft"}}],
            tool_executor=executor,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("read_draft", {"contract_id": "test"}))
        self.assertIn("Черновик", result)

    def test_multiple_tool_calls(self):
        """LLM calls two tools in one turn."""
        client = self._make_client()

        tc1 = FakeToolCall("tc_1", "read_draft", {"contract_id": "test"})
        tc2 = FakeToolCall("tc_2", "read_discussion", {"contract_id": "test"})
        msg1 = FakeMessage(content=None, tool_calls=[tc1, tc2])
        msg2 = FakeMessage(content="Вот анализ.")

        client.client.chat.completions.create = MagicMock(
            side_effect=[FakeResponse(msg1), FakeResponse(msg2)]
        )

        calls = []
        def executor(name, args):
            calls.append(name)
            if name == "read_draft":
                return {"content": "draft"}
            return {"discussion": {"status": "open"}}

        result = client.call_with_tools(
            system_prompt="s", user_message="u", tools=[], tool_executor=executor,
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("read_draft", calls)
        self.assertIn("read_discussion", calls)

    def test_max_turns_exceeded(self):
        """If LLM keeps calling tools beyond max_turns, we return gracefully."""
        client = self._make_client()

        tc = FakeToolCall("tc_1", "read_draft", {"contract_id": "x"})
        msg_with_tool = FakeMessage(content=None, tool_calls=[tc])

        # Always return tool calls — should hit max_turns
        client.client.chat.completions.create = MagicMock(
            return_value=FakeResponse(msg_with_tool)
        )

        result = client.call_with_tools(
            system_prompt="s", user_message="u", tools=[],
            tool_executor=lambda n, a: {"ok": True},
            max_turns=2,
        )
        # Should return empty string or last content (no text messages)
        self.assertIsInstance(result, str)

    def test_save_contract_flow(self):
        """Simulates: LLM calls save_contract, gets error, calls again, gets success."""
        client = self._make_client()

        # Turn 1: LLM calls save_contract
        tc1 = FakeToolCall("tc_1", "save_contract", {
            "contract_id": "test", "content": "bad content"
        })
        msg1 = FakeMessage(content=None, tool_calls=[tc1])

        # Turn 2: LLM calls save_contract again with fixed content
        tc2 = FakeToolCall("tc_2", "save_contract", {
            "contract_id": "test", "content": "good content"
        })
        msg2 = FakeMessage(content=None, tool_calls=[tc2])

        # Turn 3: LLM returns final text
        msg3 = FakeMessage(content="✅ Контракт test сохранён!")

        client.client.chat.completions.create = MagicMock(
            side_effect=[FakeResponse(msg1), FakeResponse(msg2), FakeResponse(msg3)]
        )

        call_count = [0]
        def executor(name, args):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"success": False, "errors": ["Валидация: missing section"]}
            return {"success": True, "contract_id": "test", "warnings": []}

        result = client.call_with_tools(
            system_prompt="s", user_message="зафиксируй test", tools=[],
            tool_executor=executor,
        )
        self.assertEqual(call_count[0], 2)
        self.assertIn("сохранён", result)


    def test_xml_fallback_tool_call(self):
        """LLM returns XML <invoke> in content — tool is executed, LLM answers with text."""
        client = self._make_client()

        xml_content = (
            'Сейчас посмотрю черновик.\n'
            '<invoke name="read_draft">'
            '<parameter name="contract_id">test</parameter>'
            '</invoke>'
        )
        msg1 = FakeMessage(content=xml_content, tool_calls=None)
        msg2 = FakeMessage(content="Черновик test содержит определение метрики.")

        client.client.chat.completions.create = MagicMock(
            side_effect=[FakeResponse(msg1), FakeResponse(msg2)]
        )

        calls = []
        def executor(name, args):
            calls.append((name, args))
            return {"contract_id": "test", "content": "# Draft content"}

        result = client.call_with_tools(
            system_prompt="system",
            user_message="что в черновике test?",
            tools=[{"type": "function", "function": {"name": "read_draft"}}],
            tool_executor=executor,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("read_draft", {"contract_id": "test"}))
        self.assertIn("Черновик", result)

    def test_xml_fallback_multiple_params(self):
        """XML with multiple <parameter> tags — all args are parsed correctly."""
        client = self._make_client()

        xml_content = (
            '<invoke name="save_draft">'
            '<parameter name="contract_id">revenue</parameter>'
            '<parameter name="title">Выручка</parameter>'
            '<parameter name="content">## Определение\nСумма всех продаж</parameter>'
            '</invoke>'
        )
        msg1 = FakeMessage(content=xml_content, tool_calls=None)
        msg2 = FakeMessage(content="Черновик сохранён.")

        client.client.chat.completions.create = MagicMock(
            side_effect=[FakeResponse(msg1), FakeResponse(msg2)]
        )

        calls = []
        def executor(name, args):
            calls.append((name, args))
            return {"success": True}

        result = client.call_with_tools(
            system_prompt="s", user_message="u", tools=[], tool_executor=executor,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "save_draft")
        self.assertEqual(calls[0][1]["contract_id"], "revenue")
        self.assertEqual(calls[0][1]["title"], "Выручка")
        self.assertIn("Определение", calls[0][1]["content"])
        self.assertIn("сохранён", result)


if __name__ == "__main__":
    unittest.main()
