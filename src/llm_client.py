import logging
import os
import time

import openai

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self):
        api_key = os.environ["OPENROUTER_API_KEY"]
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.cheap_model = os.environ.get("CHEAP_MODEL", "anthropic/claude-3-5-haiku-20241022")
        self.heavy_model = os.environ.get("HEAVY_MODEL", "anthropic/claude-sonnet-4-5-20250929")

        self.client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
        )
        self.log_costs = os.environ.get("LOG_LLM_COSTS", "true").lower() == "true"
        logger.info("LLM client initialized: cheap=%s, heavy=%s", self.cheap_model, self.heavy_model)

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
