#!/usr/bin/env python3
"""One-time cleanup: fix stale initiatives, duplicate contracts, dead threads.

Run inside the container:
  docker compose exec agent python scripts/cleanup_20260304.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.environ.get("DATA_DIR", ".")


def _read_json(path):
    full = os.path.join(DATA_DIR, path)
    if not os.path.exists(full):
        return None
    with open(full) as f:
        return json.load(f)


def _write_json(path, data):
    full = os.path.join(DATA_DIR, path)
    with open(full, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  [WRITTEN] {path}")


def main():
    print("=== Cleanup 2026-03-04 ===\n")

    # 1. Remove 'rec' duplicate from index.json
    print("1. Remove 'rec' duplicate from contracts/index.json")
    index = _read_json("contracts/index.json")
    if index:
        before = len(index.get("contracts", []))
        index["contracts"] = [c for c in index["contracts"] if c.get("id") != "rec"]
        after = len(index["contracts"])
        if before != after:
            _write_json("contracts/index.json", index)
            print(f"  Removed 'rec' ({before} -> {after} contracts)")
        else:
            print("  'rec' not found, skipping")

    # 2. Fix planner initiatives
    print("\n2. Fix planner initiatives")
    state = _read_json("tasks/planner_state.json")
    if state:
        for init in state.get("initiatives", []):
            iid = init.get("id", "")
            cid = init.get("contract_id", "")
            status = init.get("status", "")

            # client_segmentation: contract is agreed, initiative should be completed
            if cid == "client_segmentation" and status == "active":
                init["status"] = "completed"
                print(f"  {iid} ({cid}): active -> completed (contract agreed)")

            # recurring_income: waiting for fake user username_cs
            if cid == "recurring_income" and status == "waiting_response":
                init["status"] = "active"
                init["waiting_for"] = []
                init["stakeholders"] = []
                print(f"  {iid} ({cid}): cleared fake username_cs, reset to active")

        _write_json("tasks/planner_state.json", state)

    # 3. Clean up active threads
    print("\n3. Clean up stale active threads")
    threads = _read_json("tasks/active_threads.json")
    if threads and "threads" in threads:
        stale = ["dau", "wow", "roi", "new_income", "rec"]
        for key in stale:
            if key in threads["threads"]:
                del threads["threads"][key]
                print(f"  Removed stale thread: {key}")
        _write_json("tasks/active_threads.json", threads)

    # 4. Clean up agreed contract discussions
    print("\n4. Clean up discussions for agreed contracts")

    # client_segmentation — agreed, clear blocker
    disc_path = "drafts/client_segmentation_discussion.json"
    disc = _read_json(disc_path)
    if disc:
        disc["status"] = "agreed"
        disc["blocker"] = None
        disc["next_action"] = None
        _write_json(disc_path, disc)
        print(f"  {disc_path}: cleared blocker, set status=agreed")

    # sla_leads — agreed, clear next_action
    disc_path = "drafts/sla_leads_discussion.json"
    disc = _read_json(disc_path)
    if disc:
        disc["status"] = "agreed"
        disc["blocker"] = None
        disc["next_action"] = None
        _write_json(disc_path, disc)
        print(f"  {disc_path}: set status=agreed")

    # win_ni — approved
    disc_path = "drafts/win_ni_discussion.json"
    disc = _read_json(disc_path)
    if disc:
        disc["status"] = "agreed"
        disc["blocker"] = None
        disc["next_action"] = None
        _write_json(disc_path, disc)
        print(f"  {disc_path}: set status=agreed")

    print("\n=== Done ===")
    print("\nRemaining active work:")
    print("  - win_rec: ready for finalization (needs @pavelpetrin)")
    print("  - recurring_income: needs responsible person for calculation")
    print("  - extra_time_dashboards: waiting for product team answers")


if __name__ == "__main__":
    main()
