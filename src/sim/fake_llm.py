import json


class FakeLLMClient:
    """Deterministic LLM stub for scenario tests.

    Provide a dict of matchers to fixed outputs.
    """

    def __init__(self, *, router_rules: list[dict], cheap_rules: list[dict] | None = None, heavy_rules: list[dict] | None = None):
        self.router_rules = router_rules
        self.cheap_rules = cheap_rules or []
        self.heavy_rules = heavy_rules or []
        self.cheap_model = "fake/cheap"
        self.heavy_model = "fake/heavy"

    def _tail(self, user_message: str) -> str:
        # Agent formats messages with "Новое сообщение:" when thread_context exists.
        if "Новое сообщение:" in user_message:
            return user_message.split("Новое сообщение:", 1)[1]
        return user_message

    def call_cheap(self, system_prompt: str, user_message: str, max_tokens: int = 500) -> str:
        # Router prompt is used in src/router.py; detect by keyword.
        if "Классифицируй сообщение" in system_prompt or "Верни только JSON" in system_prompt:
            for rule in self.router_rules:
                hay = self._tail(user_message) if rule.get("match_in") == "tail" else user_message
                if rule.get("match") in hay:
                    return json.dumps(rule["response"], ensure_ascii=False)
            # default router fallback
            return json.dumps({"type": "general_question", "entity": None, "load_files": [], "model": "heavy"}, ensure_ascii=False)

        for rule in self.cheap_rules:
            hay = self._tail(user_message) if rule.get("match_in") == "tail" else user_message
            if rule.get("match") in hay:
                return rule["response"]
        return "(fake cheap)"

    def call_heavy(self, system_prompt: str, user_message: str, max_tokens: int = 2000) -> str:
        for rule in self.heavy_rules:
            hay = self._tail(user_message) if rule.get("match_in") == "tail" else user_message
            if rule.get("match") in hay:
                return rule["response"]
        return "(fake heavy)"
