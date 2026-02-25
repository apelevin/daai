#!/usr/bin/env python3
"""Live test: tool-use agent with a real LLM.

Usage:
  OPENROUTER_API_KEY=sk-... python3 scripts/test_tools_live.py

Tests the full tool-use flow:
1. Sets up temp data dir with draft + governance
2. Sends "зафиксируй контракт lead_conversion"
3. LLM should call save_contract, get governance error, report it
4. Then we assign roles and retry — LLM should succeed
"""

import json
import os
import sys
import tempfile

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.llm_client import LLMClient
from src.memory import Memory
from src.tools import ToolExecutor
from src.tool_definitions import get_read_tools, get_write_tools


DRAFT_MD = """# Data Contract: Конверсия лида

## Статус
На согласовании

## Определение
Конверсия лида — соотношение количества лидов (заявок с сайта) к оплаченным заказам за месяц.

## Формула
Человеческая: Конверсия = Количество оплаченных заказов / Количество лидов × 100%

Псевдо-SQL: SELECT COUNT(CASE WHEN status='paid' THEN 1 END) / COUNT(*) * 100 FROM crm.leads WHERE period = current_month;

## Источник данных
CRM. Лиды приходят автоматически с сайта, оплаты фиксируются автоматически.

## Включает
Все заявки с сайта считаются лидами. Оплаченные лиды = фактические заказы.

## Исключения
Нет исключений.

## Гранулярность
Ежемесячно. Пересчитывается раз в неделю.

## Ответственный за данные
Круг Sales Operations

## Ответственный за расчёт
Круг Sales Operations

## Связь с Extra Time
Конверсия лида → Эффективность воронки → Revenue → Extra Time

## Потребители
Sales, Marketing

## Состояние данных
Данные есть, качество подтверждено: CRM автоматическая фиксация.

## Согласовано
@pelevin — 2026-02-25

## История изменений
2026-02-25 — создан черновик по итогам обсуждения
"""


def separator(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: Set OPENROUTER_API_KEY environment variable")
        print("  OPENROUTER_API_KEY=sk-... python3 scripts/test_tools_live.py")
        sys.exit(1)

    tmpdir = tempfile.mkdtemp(prefix="daai-live-test-")
    os.environ["DATA_DIR"] = tmpdir
    print(f"Data dir: {tmpdir}")

    mem = Memory()

    # Seed files
    mem.write_file("prompts/system_full.md", open("prompts/system_full.md").read())
    mem.write_json("contracts/index.json", {
        "contracts": [{"id": "lead_conversion", "name": "Конверсия лида", "status": "in_review", "tier": "tier_2"}]
    })
    mem.write_file("drafts/lead_conversion.md", DRAFT_MD)
    mem.write_json("drafts/lead_conversion_discussion.json", {
        "entity": "lead_conversion",
        "status": "consensus_reached",
        "positions": {"pelevin": "согласен"},
    })
    mem.write_json("context/governance.json", {
        "tiers": {
            "tier_2": {
                "approval_required": ["data_lead", "circle_lead"],
                "consensus_threshold": 1.0,
                "description": "Операционные метрики — нужны data_lead и circle_lead",
            }
        }
    })
    # NO roles assigned yet — governance should fail

    llm = LLMClient()
    executor = ToolExecutor(mem, llm_client=llm)
    tools = get_read_tools() + get_write_tools()

    system_prompt = mem.read_file("prompts/system_full.md") or "Ты — AI-архитектор метрик."

    # ── Test 1: Save without roles → should fail ─────────────────────

    separator("TEST 1: зафиксируй контракт (без ролей → должна быть ошибка)")

    user_msg = "@pelevin: зафиксируй контракт lead_conversion"

    print(f"User: {user_msg}")
    print(f"Tools: {[t['function']['name'] for t in tools]}")
    print()

    reply = llm.call_with_tools(
        system_prompt=system_prompt,
        user_message=user_msg,
        tools=tools,
        tool_executor=executor.execute,
        max_turns=5,
    )

    print(f"Agent reply:\n{reply}")

    # Check: contract should NOT be saved
    contract = mem.get_contract("lead_conversion")
    if contract is None:
        print("\n✅ Contract NOT saved (correct — governance failed)")
    else:
        print("\n❌ Contract was saved despite missing roles!")

    # ── Test 2: Assign roles and retry → should succeed ──────────────

    separator("TEST 2: назначаем роли и повторяем")

    mem.write_json("tasks/roles.json", {
        "roles": {"data_lead": ["pavelpetrin"], "circle_lead": ["korabovtsev"]}
    })
    print("Assigned: data_lead=pavelpetrin, circle_lead=korabovtsev")

    user_msg2 = "@pelevin: зафиксируй контракт lead_conversion"
    print(f"User: {user_msg2}\n")

    reply2 = llm.call_with_tools(
        system_prompt=system_prompt,
        user_message=user_msg2,
        tools=tools,
        tool_executor=executor.execute,
        max_turns=5,
    )

    print(f"Agent reply:\n{reply2}")

    contract2 = mem.get_contract("lead_conversion")
    if contract2 is not None:
        print("\n✅ Contract SAVED successfully!")
        idx = mem.read_json("contracts/index.json")
        rec = [c for c in idx["contracts"] if c["id"] == "lead_conversion"][0]
        print(f"   Status: {rec.get('status')}")
        print(f"   Agreed date: {rec.get('agreed_date')}")
    else:
        print("\n❌ Contract was NOT saved (unexpected)")

    # ── Test 3: Simple read question ─────────────────────────────────

    separator("TEST 3: покажи черновик (read-only tool)")

    user_msg3 = "@pelevin: что сейчас в черновике lead_conversion?"

    reply3 = llm.call_with_tools(
        system_prompt=system_prompt,
        user_message=user_msg3,
        tools=get_read_tools(),  # only read tools
        tool_executor=executor.execute,
        max_turns=3,
    )

    print(f"Agent reply:\n{reply3}")

    separator("DONE")
    print(f"Data dir (for inspection): {tmpdir}")


if __name__ == "__main__":
    main()
