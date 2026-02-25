import json
import logging
import re

logger = logging.getLogger(__name__)

CHEAP_TYPES = {"contract_request", "status_request", "irrelevant"}
HEAVY_TYPES = {
    "contract_discussion", "problem_report", "new_contract_init",
    "general_question", "profile_intro",
}


def route(llm_client, memory, username: str, message: str,
          channel_type: str, thread_context: str | None = None) -> dict:
    """Classify an incoming message using the cheap model.

    Returns dict with keys: type, entity, load_files, model.
    """
    # Local fast-path commands (no LLM router needed)
    m = re.search(r"\bистори[яи]\s+контракт[а]?\s+([a-z0-9_]+)\b", message, re.IGNORECASE)
    if m:
        cid = m.group(1)
        return {"type": "contract_history", "entity": cid, "load_files": [], "model": "cheap"}

    m = re.search(r"\bпокажи\s+верс(ию|ию|ии)\s+([a-z0-9_]+)\s+([0-9]{8}T[0-9]{6}\.[0-9]{6}Z(?:_prev)?)\b", message, re.IGNORECASE)
    if m:
        cid = m.group(2)
        ts = m.group(3)
        return {"type": "contract_version", "entity": f"{cid}:{ts}", "load_files": [], "model": "cheap"}

    m = re.search(r"\b(аудит|проверь)\s+конфликт(ы|ов)?\b", message, re.IGNORECASE)
    if m:
        return {"type": "conflicts_audit", "entity": None, "load_files": ["contracts/index.json"], "model": "cheap"}

    m = re.search(r"\bпокажи\s+связи\s+([a-z0-9_\-]+)\b", message, re.IGNORECASE)
    if m:
        cid = m.group(1).lower()
        return {"type": "relationships_show", "entity": cid, "load_files": ["contracts/relationships.json", "contracts/index.json"], "model": "cheap"}

    m = re.search(r"\b(контракты\s+на\s+пересмотр|аудит\s+пересмотра|проверь\s+пересмотр)\b", message, re.IGNORECASE)
    if m:
        return {"type": "governance_review_audit", "entity": None, "load_files": ["contracts/index.json"], "model": "cheap"}

    m = re.search(r"\bпокажи\s+политику\s+(tier_[123])\b", message, re.IGNORECASE)
    if m:
        return {"type": "governance_policy_show", "entity": m.group(1).lower(), "load_files": ["context/governance.json", "context/roles.json"], "model": "cheap"}

    m = re.search(r"\bкакие\s+роли\s+нужны\s+для\s+([a-z0-9_\-]+)\b", message, re.IGNORECASE)
    if m:
        return {"type": "governance_requirements_for", "entity": m.group(1).lower(), "load_files": ["context/governance.json", "context/roles.json", "contracts/index.json"], "model": "cheap"}

    m = re.search(r"\b(переведи|поставь)\s+статус\s+([a-z0-9_\-]+)\s+(draft|in_review|approved|active|deprecated|archived)\b", message, re.IGNORECASE)
    if m:
        cid = m.group(2).lower()
        st = m.group(3).lower()
        return {"type": "lifecycle_set_status", "entity": f"{cid}:{st}", "load_files": ["contracts/index.json"], "model": "cheap"}

    m = re.search(r"\bкакой\s+статус\s+([a-z0-9_\-]+)\b", message, re.IGNORECASE)
    if m:
        cid = m.group(1).lower()
        return {"type": "lifecycle_get_status", "entity": cid, "load_files": ["contracts/index.json"], "model": "cheap"}

    router_prompt = memory.read_file("prompts/router.md") or ""

    user_input = (
        f'Сообщение от @{username} в {channel_type}:\n'
        f'"{message}"\n'
    )
    if thread_context:
        user_input += f"\nКонтекст треда:\n{thread_context}\n"

    def _extract_json(text: str) -> dict:
        """Best-effort extract JSON object from model output.

        The cheap router sometimes returns valid JSON with trailing text.
        We take the first '{' and last '}' and try to parse that slice.
        """
        t = (text or "").strip()
        if t.startswith("```"):
            # Strip markdown code block
            lines = t.split("\n")
            t = "\n".join(lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:])
            t = t.strip()

        # Fast path
        try:
            return json.loads(t)
        except Exception:
            pass

        i = t.find("{")
        j = t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i : j + 1])
        return json.loads(t)  # will raise

    try:
        raw = llm_client.call_cheap(router_prompt, user_input)
        data = _extract_json(raw)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Router failed to parse JSON: %s — raw: %s", e, (raw or "")[:200])
        data = {
            "type": "general_question",
            "entity": None,
            "load_files": [],
            "model": "heavy",
        }

    # Ensure required keys
    result = {
        "type": data.get("type", "general_question"),
        "entity": data.get("entity"),
        "load_files": data.get("load_files", []),
        "model": data.get("model", "heavy"),
    }

    # Validate model choice
    if result["type"] in CHEAP_TYPES:
        result["model"] = "cheap"
    elif result["type"] in HEAVY_TYPES:
        result["model"] = "heavy"

    logger.info("Router: type=%s entity=%s model=%s", result["type"], result["entity"], result["model"])
    return result
