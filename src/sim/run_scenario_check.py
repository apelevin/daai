import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", help="Path to scenario JSON")
    ap.add_argument("--check", help="Relative file path inside DATA_DIR to check")
    ap.add_argument("--contains", help="Substring that must be present")
    ap.add_argument("--transcript-contains", help="Substring that must be present in transcript messages")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    run_scenarios = root / "run_scenarios.py"

    env = os.environ.copy()
    # Run scenario and capture JSON output
    p = subprocess.run(
        ["python", str(run_scenarios), args.scenario, "--keep"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if p.returncode != 0:
        print(p.stderr)
        raise SystemExit(p.returncode)

    out = json.loads(p.stdout)
    data_dir = Path(out["data_dir"])

    if args.check:
        target = data_dir / args.check
        if not target.exists():
            raise SystemExit(f"CHECK_FAIL: missing file: {args.check}")

        content = target.read_text(encoding="utf-8")
        if (args.contains or "") not in content:
            raise SystemExit(f"CHECK_FAIL: substring not found in {args.check}: {args.contains}")

    if args.transcript_contains:
        needle = args.transcript_contains
        found = any(needle in (m.get("message") or "") for m in out.get("transcript", []))
        if not found:
            raise SystemExit(f"CHECK_FAIL: transcript does not contain: {needle}")

    # Cleanup temp dir created by run_scenarios.py
    import shutil
    shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
