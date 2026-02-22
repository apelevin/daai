import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", help="Scenario JSON for run_reminders_steps.py")
    ap.add_argument("--contains", action="append", default=[], help="Substring that must appear in posts (repeatable)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    runner = root / "run_reminders_steps.py"

    p = subprocess.run(
        ["python", str(runner), args.scenario, "--keep"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": "."},
    )
    if p.returncode != 0:
        print(p.stderr)
        raise SystemExit(p.returncode)

    out = json.loads(p.stdout)
    needles = args.contains or []
    posts = out.get("posts", [])

    for needle in needles:
        found = False
        for post in posts:
            msg = post.get("message") or ""
            if needle in msg:
                found = True
                break
        if not found:
            raise SystemExit(f"CHECK_FAIL: reminders output does not contain: {needle}")


if __name__ == "__main__":
    main()
