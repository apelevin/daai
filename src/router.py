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

    router_prompt = memory.read_file("prompts/router.md") or ""

    user_input = (
        f'Сообщение от @{username} в {channel_type}:\n'
        f'"{message}"\n'
    )
    if thread_context:
        user_input += f"\nКонтекст треда:\n{thread_context}\n"

    try:
        raw = llm_client.call_cheap(router_prompt, user_input)
        # Extract JSON from response (model may wrap it in markdown)
        raw = raw.strip()
        if raw.startswith("```"):
            # Strip markdown code block
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Router failed to parse JSON: %s — raw: %s", e, raw[:200] if 'raw' in dir() else "N/A")
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
