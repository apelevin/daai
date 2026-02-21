import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from src.agent import Agent
from src.listener import Listener
from src.memory import Memory
from src.sim.fake_llm import FakeLLMClient
from src.sim.fake_mattermost import FakeMattermostClient


def write_file(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def load_scenario(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", help="Path to scenario JSON")
    ap.add_argument("--keep", action="store_true", help="Keep temp DATA_DIR")
    args = ap.parse_args()

    scenario_path = Path(args.scenario)
    sc = load_scenario(scenario_path)

    data_dir = Path(tempfile.mkdtemp(prefix="daai-sim-"))
    os.environ["DATA_DIR"] = str(data_dir)

    # Seed minimal prompts/router prompt
    write_file(data_dir, "prompts/router.md", "Классифицируй сообщение. Верни только JSON, ничего больше.")
    write_file(data_dir, "prompts/system_short.md", "Ты — AI-архитектор метрик. Отвечай кратко.")
    write_file(data_dir, "prompts/system_full.md", "Ты — AI-архитектор метрик. Следуй правилам. Не больше 3 вопросов.")

    # Seed any files from scenario
    for f in sc.get("seed_files", []):
        write_file(data_dir, f["path"], f["content"])

    mm = FakeMattermostClient(channel_id="data-contracts", bot_user_id="bot")

    # Register users
    for u in sc.get("users", []):
        mm.register_user(user_id=u["user_id"], username=u["username"], display_name=u.get("display_name", ""))

    llm = FakeLLMClient(
        router_rules=sc.get("router_rules", []),
        cheap_rules=sc.get("cheap_rules", []),
        heavy_rules=sc.get("heavy_rules", []),
    )

    memory = Memory()
    agent = Agent(llm, memory, mm)
    listener = Listener(agent, mm)

    ids: dict[str, str] = {}

    def inject_post(*, user_id: str, message: str, root_id: str | None = None, step_id: str | None = None):
        # Build event structure like mattermost WS "posted"
        post_id = os.urandom(8).hex()
        post = {
            "id": post_id,
            "user_id": user_id,
            "channel_id": mm.channel_id,
            "message": message,
            "root_id": root_id or "",
        }

        # Record inbound message into fake store for a full transcript
        mm.record_user_post(
            user_id=user_id,
            channel_id=mm.channel_id,
            message=message,
            root_id=root_id or "",
        )

        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post),
                "channel_type": "O",
            },
            "broadcast": {
                "channel_id": mm.channel_id,
            },
        }
        listener._handle_event(event)
        if step_id:
            ids[step_id] = post_id
        return post_id

    def inject_user_added(*, user_id: str):
        event = {
            "event": "user_added",
            "data": {"user_id": user_id},
            "broadcast": {"channel_id": mm.channel_id},
        }
        listener._handle_event(event)

    # Run steps
    for step in sc.get("steps", []):
        t = step["type"]
        if t == "user_added":
            inject_user_added(user_id=step["user_id"])
        elif t == "user_removed":
            event = {
                "event": "user_removed",
                "data": {"user_id": step["user_id"]},
                "broadcast": {"channel_id": mm.channel_id},
            }
            listener._handle_event(event)
        elif t == "posted":
            root_id = step.get("root_id")
            if not root_id and step.get("root_ref"):
                root_id = ids.get(step["root_ref"])
            inject_post(
                user_id=step["user_id"],
                message=step["message"],
                root_id=root_id,
                step_id=step.get("id"),
            )
        else:
            raise ValueError(f"Unknown step type: {t}")

    # Dump results
    # Build a human-readable transcript (username, channel_id, root_id, message)
    posts = mm.all_posts()
    users = {u.user_id: u for u in mm._users.values()}  # type: ignore
    transcript = []
    for p in posts:
        uname = users.get(p["user_id"]).username if p["user_id"] in users else p["user_id"]
        transcript.append({
            "user": uname,
            "channel": p["channel_id"],
            "root_id": p.get("root_id", ""),
            "message": p["message"],
            "id": p["id"],
        })

    out = {
        "data_dir": str(data_dir),
        "posts": posts,
        "transcript": transcript,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
