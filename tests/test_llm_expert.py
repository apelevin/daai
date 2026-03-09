"""Tests for LLM expert model configuration and call_expert method."""

import os
import unittest
from unittest.mock import MagicMock, patch


class TestExpertModelConfig(unittest.TestCase):
    """Test expert model initialisation and call_expert method."""

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-key",
        "EXPERT_MODEL": "openai/gpt-5.4",
    })
    @patch("openai.OpenAI")
    def test_expert_model_from_env(self, mock_openai):
        from importlib import reload
        import src.config
        reload(src.config)
        import src.llm_client
        reload(src.llm_client)
        client = src.llm_client.LLMClient()
        self.assertEqual(client.expert_model, "openai/gpt-5.4")

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-key",
        "EXPERT_MODEL": "test/expert-model",
    })
    @patch("openai.OpenAI")
    def test_call_expert_delegates_to_call(self, mock_openai_cls):
        from importlib import reload
        import src.config
        reload(src.config)
        import src.llm_client
        reload(src.llm_client)
        client = src.llm_client.LLMClient()

        client._call = MagicMock(return_value="expert response")

        result = client.call_expert("system prompt", "user message", max_tokens=2000)

        client._call.assert_called_once_with(
            model="test/expert-model",
            system_prompt="system prompt",
            user_message="user message",
            max_tokens=2000,
            temperature=0.4,
            label="expert",
        )
        self.assertEqual(result, "expert response")

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-key",
        "EXPERT_MODEL": "test/expert-model",
    })
    @patch("openai.OpenAI")
    def test_call_expert_default_max_tokens(self, mock_openai_cls):
        from importlib import reload
        import src.config
        reload(src.config)
        import src.llm_client
        reload(src.llm_client)
        client = src.llm_client.LLMClient()
        client._call = MagicMock(return_value="ok")

        client.call_expert("sys", "usr")

        args = client._call.call_args
        self.assertEqual(args.kwargs["max_tokens"], 3000)
        self.assertEqual(args.kwargs["temperature"], 0.4)


if __name__ == "__main__":
    unittest.main()
