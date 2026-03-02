import json
import logging
import os
import re
import time
from typing import Callable

import openai

from src.config import LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)">\s*(.*?)</invoke>', re.DOTALL)
_PARAM_RE = re.compile(r'<parameter\s+name="([^"]+)">(.*?)</parameter>', re.DOTALL)


def _parse_xml_tool_calls(content: str) -> list[tuple[str, dict]]:
    """Parse XML <invoke> tool calls from text. Returns [(tool_name, args_dict)]."""
    results = []
    for m in _INVOKE_RE.finditer(content):
        tool_name = m.group(1)
        args = {}
        for pm in _PARAM_RE.finditer(m.group(2)):
            args[pm.group(1)] = pm.group(2).strip()
        results.append((tool_name, args))
    return results


def _strip_xml_invokes(content: str) -> str:
    """Remove <invoke>...</invoke> blocks from content."""
    return _INVOKE_RE.sub('', content).strip()


class LLMClient:
    def __init__(self):
        api_key = os.environ["OPENROUTER_API_KEY"]
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.cheap_model = os.environ.get("CHEAP_MODEL", "anthropic/claude-3.5-haiku")
        self.heavy_model = os.environ.get("HEAVY_MODEL", "anthropic/claude-sonnet-4")
        self.fallback_model = os.environ.get("FALLBACK_MODEL", "google/gemini-3-flash-preview")

        self.client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        self.log_costs = os.environ.get("LOG_LLM_COSTS", "true").lower() == "true"
        logger.info("LLM client initialized: cheap=%s, heavy=%s, fallback=%s, timeout=%ds", self.cheap_model, self.heavy_model, self.fallback_model, LLM_TIMEOUT_SECONDS)

    def call_cheap(self, system_prompt: str, user_message: str) -> str:
        """Fast cheap call for routing and simple responses."""
        return self._call(
            model=self.cheap_model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=500,
            temperature=0.0,
            label="cheap",
        )

    def call_heavy(self, system_prompt: str, user_message: str, max_tokens: int = 2000) -> str:
        """Heavy call for analytics and generation."""
        return self._call(
            model=self.heavy_model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=0.3,
            label="heavy",
        )

    def call_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], dict],
        *,
        max_turns: int = 5,
        max_tokens: int = 4000,
    ) -> str:
        """Agentic loop: LLM calls tools, gets results, generates final text.

        Args:
            system_prompt: System prompt for the LLM.
            user_message: User message to process.
            tools: List of tool definitions in OpenAI format.
            tool_executor: Callable(tool_name, args) -> dict result.
            max_turns: Maximum number of tool-calling rounds.
            max_tokens: Max tokens per LLM call.

        Returns:
            Final text response from the LLM.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        for turn in range(max_turns):
            try:
                response = self.client.chat.completions.create(
                    model=self.heavy_model,
                    messages=messages,
                    tools=tools if tools else openai.NOT_GIVEN,
                    max_tokens=max_tokens,
                    temperature=0.3,
                    frequency_penalty=0.3,
                )
            except openai.APITimeoutError as e:
                logger.warning("LLM timeout in tool loop (turn %d): %s", turn, e)
                time.sleep(2 * (turn + 1))
                continue
            except openai.RateLimitError as e:
                logger.warning("LLM rate limit in tool loop (turn %d): %s", turn, e)
                time.sleep(2 * (turn + 1))
                continue
            except openai.APIStatusError as e:
                if e.status_code >= 500:
                    logger.warning("LLM server error in tool loop (turn %d): %s", turn, e)
                    time.sleep(2 * (turn + 1))
                    continue
                raise

            choice = response.choices[0]
            msg = choice.message

            if self.log_costs and response.usage:
                logger.info(
                    "LLM [tools turn=%d] model=%s prompt_tokens=%d completion_tokens=%d",
                    turn,
                    self.heavy_model,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )

            # If no tool calls — check for XML fallback, otherwise return text
            if not msg.tool_calls:
                xml_calls = _parse_xml_tool_calls(msg.content or "")
                if not xml_calls:
                    reply = msg.content or ""
                    if not reply.strip() and turn > 0:
                        reply = self._generate_fallback_reply(messages)
                    return reply

                logger.info("Fallback: parsed %d XML tool call(s) from text", len(xml_calls))
                clean_content = _strip_xml_invokes(msg.content or "")

                # Build synthetic tool_calls for message history
                synth_calls = []
                for idx, (tool_name, args) in enumerate(xml_calls):
                    synth_calls.append({
                        "id": f"xmlfb_{turn}_{idx}",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        }
                    })

                messages.append({
                    "role": "assistant",
                    "content": clean_content or None,
                    "tool_calls": synth_calls,
                })

                for idx, (tool_name, args) in enumerate(xml_calls):
                    result = tool_executor(tool_name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": synth_calls[idx]["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                continue

            # Append assistant message with tool calls
            messages.append(msg)

            # Execute each tool and append results
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                    logger.warning("Failed to parse tool args for %s: %s", tc.function.name, tc.function.arguments)

                result = tool_executor(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        # Max turns exceeded — return whatever text we have
        result = ""
        for m in reversed(messages):
            if hasattr(m, "content") and m.content:
                result = m.content
                break
            if isinstance(m, dict) and m.get("content") and m.get("role") == "assistant":
                result = m["content"]
                break
        if not result.strip():
            result = self._generate_fallback_reply(messages)
        return result

    def _generate_fallback_reply(self, messages: list) -> str:
        """Generate a reply via fallback model when tool loop produced empty response."""
        try:
            # Extract user message
            user_text = ""
            for m in messages:
                role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
                if role == "user":
                    user_text = m.get("content") if isinstance(m, dict) else (m.content or "")
                    break

            # Collect tool results
            tool_results = []
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "tool":
                    tool_results.append(m.get("content", ""))

            context_parts = [f"Пользователь написал: {user_text}"]
            if tool_results:
                context_parts.append(f"Были выполнены операции: {tool_results}")

            fallback_system = (
                "Ты AI-архитектор Data Contracts. Ты выполнил операции с инструментами, "
                "но забыл ответить пользователю. Напиши краткий ответ на русском: "
                "что было сделано, что зафиксировано, какие открытые вопросы, какие следующие шаги."
            )

            reply = self._call(
                model=self.fallback_model,
                system_prompt=fallback_system,
                user_message="\n".join(context_parts),
                max_tokens=800,
                temperature=0.3,
                label="fallback",
            )
            logger.info("LLM [fallback] generated reply via %s", self.fallback_model)
            return reply
        except Exception:
            logger.exception("LLM [fallback] failed to generate reply")
            return ""

    def _call(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        temperature: float,
        label: str,
    ) -> str:
        """Call LLM with retry on 429 and 5xx."""
        max_retries = 3
        backoff = 2

        for attempt in range(1, max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                content = response.choices[0].message.content or ""

                if self.log_costs:
                    usage = response.usage
                    if usage:
                        logger.info(
                            "LLM [%s] model=%s prompt_tokens=%d completion_tokens=%d",
                            label,
                            model,
                            usage.prompt_tokens,
                            usage.completion_tokens,
                        )

                return content

            except openai.APITimeoutError as e:
                logger.warning("LLM timeout (attempt %d/%d): %s", attempt, max_retries, e)
                if attempt < max_retries:
                    time.sleep(backoff * attempt)
                else:
                    raise

            except openai.RateLimitError as e:
                logger.warning("LLM rate limit (attempt %d/%d): %s", attempt, max_retries, e)
                if attempt < max_retries:
                    time.sleep(backoff * attempt)
                else:
                    raise

            except openai.APIStatusError as e:
                if e.status_code >= 500:
                    logger.warning("LLM server error %d (attempt %d/%d): %s", e.status_code, attempt, max_retries, e)
                    if attempt < max_retries:
                        time.sleep(backoff * attempt)
                    else:
                        raise
                else:
                    raise

        return ""
