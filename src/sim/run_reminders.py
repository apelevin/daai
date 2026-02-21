import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from src.agent import Agent
from src.memory import Memory
from src.scheduler import Scheduler
from src.sim.fake_llm import FakeLLMClient
from src.sim.fake_mattermost import FakeMattermostClient


def write_file(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    data_dir = Path(tempfile.mkdtemp(prefix="daai-rem-"))
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["ESCALATION_USER"] = "controller"

    # prompts
    write_file(data_dir, "prompts/system_short.md", "Ты — AI-архитектор метрик. Кратко.")
    write_file(data_dir, "prompts/reminder_templates.md", "(templates placeholder)")

    # Seed a reminder due now
    write_file(
        data_dir,
        "tasks/reminders.json",
        json.dumps(
            {
                "reminders": [
                    {
                        "id": "rem_001",
                        "contract_id": "win_ni",
                        "target_user": "dd_lead",
                        "target_mm_user_id": "u2",
                        "thread_id": "thread_123",
                        "question_summary": "Выбираем A или B?",
                        "first_asked": "2026-02-18T09:00:00+00:00",
                        "last_reminder": "2026-02-19T09:00:00+00:00",
                        "escalation_step": 1,
                        "next_reminder": "2000-01-01T00:00:00+00:00"
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
    )

    # Fake MM + users
    mm = FakeMattermostClient(channel_id="data-contracts", bot_user_id="bot")
    mm.register_user(user_id="u2", username="dd_lead", display_name="DD Lead")

    # Fake LLM (only used for step==2 if discussion missing)
    llm = FakeLLMClient(
        router_rules=[],
        cheap_rules=[{"match": "Сформулируй два простых варианта", "response": "A — CRM signed\nB — DDP invoices"}],
        heavy_rules=[],
    )

    memory = Memory()
    agent = Agent(llm, memory, mm)
    scheduler = Scheduler(agent, memory, mm, llm)

    # Run one check cycle
    scheduler._check_reminders()

    out = {
        "data_dir": str(data_dir),
        "posts": mm.all_posts(),
        "reminders": memory.get_reminders(),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
