"""OpenAI-compatible tool definitions for the Data Contract agent.

Each tool is defined as a dict matching the OpenAI SDK `tools` parameter format.
Split into read-only (informational) and write (state-changing) groups.
"""

from __future__ import annotations


def _tool(name: str, description: str, parameters: dict) -> dict:
    """Helper to build a tool definition in OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": parameters.get("properties", {}),
                "required": parameters.get("required", []),
            },
        },
    }


# ── Read-only tools ──────────────────────────────────────────────────────────

READ_TOOLS: list[dict] = [
    _tool(
        "read_contract",
        "Читает финальный контракт contracts/{contract_id}.md. Возвращает markdown текст или ошибку если не найден.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта (например client_tier_segmentation)"},
            },
            "required": ["contract_id"],
        },
    ),
    _tool(
        "read_draft",
        "Читает черновик drafts/{contract_id}.md. Возвращает markdown текст или ошибку если не найден.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
            },
            "required": ["contract_id"],
        },
    ),
    _tool(
        "read_discussion",
        "Читает обсуждение drafts/{contract_id}_discussion.json. Возвращает JSON объект с позициями участников.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
            },
            "required": ["contract_id"],
        },
    ),
    _tool(
        "read_governance_policy",
        "Читает политику согласования для указанного tier (tier_1, tier_2, tier_3). "
        "Возвращает требуемые роли, порог консенсуса и текущие назначения.",
        {
            "properties": {
                "tier": {"type": "string", "description": "Tier политики: tier_1, tier_2 или tier_3"},
            },
            "required": ["tier"],
        },
    ),
    _tool(
        "read_roles",
        "Читает назначенные роли из tasks/roles.json + context/roles.json. "
        "Возвращает объединённый словарь ролей.",
        {"properties": {}, "required": []},
    ),
    _tool(
        "validate_contract",
        "Запускает детерминистическую валидацию markdown контракта. "
        "Возвращает {ok: bool, issues: [...], warnings: [...]}.",
        {
            "properties": {
                "contract_md": {"type": "string", "description": "Полный markdown текст контракта для валидации"},
            },
            "required": ["contract_md"],
        },
    ),
    _tool(
        "check_approval",
        "Проверяет governance policy + glossary для контракта. "
        "Возвращает {ok: bool, missing_roles: [...], glossary_issues: [...]}.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта (для определения tier)"},
                "contract_md": {"type": "string", "description": "Полный markdown текст контракта"},
            },
            "required": ["contract_id", "contract_md"],
        },
    ),
    _tool(
        "diff_contract",
        "Показывает diff между текущей и предыдущей версией контракта в unified diff формате.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
            },
            "required": ["contract_id"],
        },
    ),
    _tool(
        "generate_contract_template",
        "Генерирует предзаполненный шаблон нового контракта на основе дерева метрик, "
        "кругов ответственности и governance policy. Используй при начале нового контракта.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта (snake_case, латиница)"},
            },
            "required": ["contract_id"],
        },
    ),
    _tool(
        "participant_stats",
        "Возвращает аналитику участников: количество согласований, сохранённых контрактов, "
        "назначений ролей. Без параметра — все участники, с username — конкретный.",
        {
            "properties": {
                "username": {"type": "string", "description": "Username участника (опционально)"},
            },
            "required": [],
        },
    ),
    _tool(
        "list_contracts",
        "Возвращает список всех контрактов из contracts/index.json с id, name, status, tier.",
        {"properties": {}, "required": []},
    ),
]


# ── Write tools ──────────────────────────────────────────────────────────────

WRITE_TOOLS: list[dict] = [
    _tool(
        "save_contract",
        "Валидирует контракт (структура + governance + glossary) и сохраняет если всё ок. "
        "Возвращает {success: bool, contract_id: str, errors: [...], warnings: [...]}. "
        "При ошибках контракт НЕ сохраняется — объясни пользователю все проблемы. "
        "force=true: glossary issues становятся warnings (не блокируют сохранение).",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
                "content": {"type": "string", "description": "Полный markdown текст контракта"},
                "force": {"type": "boolean", "description": "Если true — glossary issues не блокируют сохранение (становятся warnings)", "default": False},
            },
            "required": ["contract_id", "content"],
        },
    ),
    _tool(
        "save_draft",
        "Сохраняет черновик контракта в drafts/{contract_id}.md и обновляет index.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
                "content": {"type": "string", "description": "Markdown текст черновика"},
            },
            "required": ["contract_id", "content"],
        },
    ),
    _tool(
        "update_discussion",
        "Обновляет JSON обсуждения drafts/{contract_id}_discussion.json.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
                "discussion": {
                    "type": "object",
                    "description": "JSON объект обсуждения с полями: entity, status, positions, proposed_resolution, blocker, next_action",
                },
            },
            "required": ["contract_id", "discussion"],
        },
    ),
    _tool(
        "add_reminder",
        "Добавляет напоминание в tasks/reminders.json.",
        {
            "properties": {
                "reminder": {
                    "type": "object",
                    "description": "JSON напоминания с полями: id, contract_id, target_user, question_summary, next_reminder и др.",
                },
            },
            "required": ["reminder"],
        },
    ),
    _tool(
        "update_participant",
        "Обновляет профиль участника в participants/{username}.md.",
        {
            "properties": {
                "username": {"type": "string", "description": "Username участника (латиницей)"},
                "content": {"type": "string", "description": "Markdown текст профиля"},
            },
            "required": ["username", "content"],
        },
    ),
    _tool(
        "save_decision",
        "Записывает решение в memory/decisions.jsonl.",
        {
            "properties": {
                "decision": {
                    "type": "object",
                    "description": "JSON решения с полями: contract, decision, agreed_by, method",
                },
            },
            "required": ["decision"],
        },
    ),
    _tool(
        "assign_role",
        "Назначает пользователя на роль в tasks/roles.json.",
        {
            "properties": {
                "role": {"type": "string", "description": "Роль: data_lead, circle_lead, ceo, cfo"},
                "username": {"type": "string", "description": "Username пользователя (латиницей)"},
            },
            "required": ["role", "username"],
        },
    ),
    _tool(
        "set_contract_status",
        "Меняет статус контракта в contracts/index.json. "
        "Допустимые статусы: draft, in_review, agreed (согласован), approved (утверждён), active, deprecated, archived.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
                "status": {
                    "type": "string",
                    "description": "Новый статус",
                    "enum": ["draft", "in_review", "agreed", "approved", "active", "deprecated", "archived"],
                },
            },
            "required": ["contract_id", "status"],
        },
    ),
    _tool(
        "request_approval",
        "Запускает процесс согласования контракта: определяет необходимые роли по governance policy, "
        "отправляет уведомления ответственным, сохраняет состояние согласования. "
        "Возвращает {success, tier, required_roles, role_users, quorum_met}.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта для согласования"},
            },
            "required": ["contract_id"],
        },
    ),
    _tool(
        "approve_contract",
        "Записывает голос согласования от пользователя. Проверяет роль, дедуплицирует голоса. "
        "Если кворум достигнут — сообщает что контракт можно финализировать через save_contract. "
        "Возвращает {success, quorum_met, missing_roles}.",
        {
            "properties": {
                "contract_id": {"type": "string", "description": "ID контракта"},
                "username": {"type": "string", "description": "Username согласующего (латиницей)"},
            },
            "required": ["contract_id", "username"],
        },
    ),
    _tool(
        "create_poll",
        "Создаёт опрос через Matterpoll. Используй когда нужно голосование: "
        "приоритизация контрактов, развилки в обсуждении, подтверждение консенсуса.",
        {
            "properties": {
                "question": {"type": "string", "description": "Вопрос для голосования"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Варианты ответа (2-5 штук)",
                },
                "channel_id": {
                    "type": "string",
                    "description": "ID канала (по умолчанию — Data Contracts канал)",
                },
            },
            "required": ["question", "options"],
        },
    ),
]


def get_read_tools() -> list[dict]:
    """Return read-only tool definitions."""
    return list(READ_TOOLS)


def get_write_tools() -> list[dict]:
    """Return write (state-changing) tool definitions."""
    return list(WRITE_TOOLS)


def get_all_tools() -> list[dict]:
    """Return all tool definitions."""
    return get_read_tools() + get_write_tools()


_GENERAL_TOOL_NAMES = {"read_contract", "read_draft", "list_contracts"}


def get_tools_for_route(route_type: str, is_channel: bool) -> list[dict]:
    """Return only relevant tools for a given route type."""
    if route_type == "profile_intro":
        return [t for t in WRITE_TOOLS if t["function"]["name"] == "update_participant"]

    if route_type == "general_question":
        return [t for t in READ_TOOLS if t["function"]["name"] in _GENERAL_TOOL_NAMES]

    # contract_discussion, new_contract_init, problem_report — full set
    tools = list(READ_TOOLS)
    if is_channel:
        tools += list(WRITE_TOOLS)
    return tools
