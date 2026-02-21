import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="Keep temp DATA_DIRs created by runners")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    scenarios = [
        ("01_smoke", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/01_smoke.json")] + (["--keep"] if args.keep else [])),
        ("02_contract_request_missing", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/02_contract_request_missing.json")] + (["--keep"] if args.keep else [])),
        ("03_contract_request_present", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/03_contract_request_present.json")] + (["--keep"] if args.keep else [])),
        ("04_win_ni_discussion_ab", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/04_win_ni_discussion_ab.json")] + (["--keep"] if args.keep else [])),
        ("05_win_ni_save_contract", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/05_win_ni_save_contract.json")] + (["--keep"] if args.keep else [])),
        ("06_win_ni_full_e2e", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/06_win_ni_full_e2e.json")] + (["--keep"] if args.keep else [])),
        ("07_reminders_steps_1_4", [sys.executable, str(root / "run_reminders_steps.py"), str(root / "scenarios/07_reminders_steps_1_4.json")] + (["--keep"] if args.keep else [])),
        ("08_onboarding_dm_profile_update", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/08_onboarding_dm_profile_update.json")] + (["--keep"] if args.keep else [])),
        ("09_contract_versioning_history", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/09_contract_versioning_history.json")] + (["--keep"] if args.keep else [])),
        ("10_user_added_removed_participants_index", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/10_user_added_removed_participants_index.json")] + (["--keep"] if args.keep else [])),
    ]

    results = []
    for name, cmd in scenarios:
        code, out = run(cmd)
        ok = code == 0
        results.append({"name": name, "ok": ok, "code": code})
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        if not ok:
            print(out)

    all_ok = all(r["ok"] for r in results)
    print("\nSummary:")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
