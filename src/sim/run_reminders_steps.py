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
    ap.add_argument("scenario", help="Scenario JSON with seed_files")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    sc = json.loads(Path(args.scenario).read_text(encoding="utf-8"))

    data_dir = Path(tempfile.mkdtemp(prefix="daai-remsteps-"))
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["ESCALATION_USER"] = "controller"

    for f in sc.get("seed_files", []):
        write_file(data_dir, f["path"], f["content"])

    mm = FakeMattermostClient(channel_id="data-contracts", bot_user_id="bot")
    for u in sc.get("users", []):
        mm.register_user(user_id=u["user_id"], username=u["username"], display_name=u.get("display_name", ""))

    llm = FakeLLMClient(router_rules=[], cheap_rules=sc.get('cheap_rules', []), heavy_rules=[])
    memory = Memory()
    agent = Agent(llm, memory, mm)
    scheduler = Scheduler(agent, memory, mm, llm)

    # step 1
    scheduler._check_reminders()
    # make due again
    rems = memory.get_reminders()
    for r in rems:
        r['next_reminder'] = '2000-01-01T00:00:00+00:00'
    memory.save_reminders(rems)

    # step 2
    scheduler._check_reminders()
    rems = memory.get_reminders()
    for r in rems:
        r['next_reminder'] = '2000-01-01T00:00:00+00:00'
    memory.save_reminders(rems)

    # step 3
    scheduler._check_reminders()
    rems = memory.get_reminders()
    for r in rems:
        r['next_reminder'] = '2000-01-01T00:00:00+00:00'
    memory.save_reminders(rems)

    # step 4+
    scheduler._check_reminders()

    out = {
        'data_dir': str(data_dir),
        'posts': mm.all_posts(),
        'reminders': memory.get_reminders(),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
