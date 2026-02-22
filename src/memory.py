import json
import logging
import os
import hashlib
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class Memory:
    def __init__(self):
        self.base_dir = os.environ.get("DATA_DIR", ".")

    def _utc_ts(self) -> str:
        # include microseconds to avoid collisions on rapid successive saves
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    def _sha256(self, content: str) -> str:
        return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

    def _path(self, *parts: str) -> str:
        return os.path.join(self.base_dir, *parts)

    # ── Generic file operations ─────────────────────────────────────

    def read_file(self, path: str) -> str | None:
        """Read a file relative to base_dir. Returns None if not found."""
        full = self._path(path)
        try:
            with open(full, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            logger.debug("File not found: %s", full)
            return None

    def write_file(self, path: str, content: str) -> None:
        """Write content to a file relative to base_dir."""
        full = self._path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        logger.debug("Written: %s", full)

    def append_jsonl(self, path: str, data: dict) -> None:
        """Append a JSON line to a JSONL file."""
        full = self._path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def read_json(self, path: str) -> dict | list | None:
        """Read and parse a JSON file. Returns None if not found."""
        content = self.read_file(path)
        if content is None:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in %s", path)
            return None

    def write_json(self, path: str, data) -> None:
        """Write data as formatted JSON."""
        full = self._path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── Contracts ───────────────────────────────────────────────────

    def list_contracts(self) -> list[dict]:
        """Get all contracts from index."""
        data = self.read_json("contracts/index.json")
        if data and "contracts" in data:
            return data["contracts"]
        return []

    def get_contract(self, contract_id: str) -> str | None:
        """Read a contract markdown file."""
        return self.read_file(f"contracts/{contract_id}.md")

    def save_contract(self, contract_id: str, content: str) -> None:
        """Save a finalized contract.

        Versioning (simple):
        - If a contract already exists, snapshot the previous content.
        - Always snapshot the new content.

        Files:
        - contracts/<id>.md (current)
        - contracts/versions/<id>/<ts>.md (snapshots)
        - contracts/versions/<id>/history.jsonl (metadata)
        """
        current_path = f"contracts/{contract_id}.md"
        prev = self.read_file(current_path)
        ts = self._utc_ts()

        versions_dir = f"contracts/versions/{contract_id}"
        history_path = f"{versions_dir}/history.jsonl"

        if prev is not None:
            prev_ts = f"{ts}_prev"
            self.write_file(f"{versions_dir}/{prev_ts}.md", prev)
            self.append_jsonl(history_path, {
                "ts": prev_ts,
                "kind": "previous",
                "sha256": self._sha256(prev),
                "bytes": len(prev.encode("utf-8")),
            })

        # Write new current
        self.write_file(current_path, content)

        # Snapshot new
        self.write_file(f"{versions_dir}/{ts}.md", content)
        self.append_jsonl(history_path, {
            "ts": ts,
            "kind": "current",
            "sha256": self._sha256(content),
            "bytes": len((content or "").encode("utf-8")),
        })

    def get_contract_history(self, contract_id: str) -> list[dict]:
        """Return version history metadata for a contract."""
        history_path = f"contracts/versions/{contract_id}/history.jsonl"
        return self.read_jsonl(history_path)

    def get_contract_version(self, contract_id: str, ts: str) -> str | None:
        """Return a specific version snapshot by timestamp string."""
        return self.read_file(f"contracts/versions/{contract_id}/{ts}.md")

    def update_contract_index(self, contract_id: str, data: dict) -> None:
        """Update or add a contract in the index.

        Adds simple versioning metadata if the versions history exists.
        """
        index = self.read_json("contracts/index.json") or {"contracts": []}
        contracts = index["contracts"]

        versions_dir = f"contracts/versions/{contract_id}"
        history_path = f"{versions_dir}/history.jsonl"
        if self.read_file(history_path) is not None:
            data = {
                **data,
                "versions_dir": versions_dir,
                "history_file": history_path,
            }

        # Update existing or append
        found = False
        for i, c in enumerate(contracts):
            if c.get("id") == contract_id:
                contracts[i] = {**c, **data}
                found = True
                break
        if not found:
            contracts.append({"id": contract_id, **data})

        self.write_json("contracts/index.json", index)

    def read_jsonl(self, path: str) -> list[dict]:
        """Read JSONL file and return list of dicts. Returns [] if not found."""
        content = self.read_file(path)
        if not content:
            return []
        items = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                logger.error("Invalid JSONL line in %s", path)
        return items

    # ── Drafts ──────────────────────────────────────────────────────

    def save_draft(self, contract_id: str, content: str) -> None:
        """Save a draft contract."""
        self.write_file(f"drafts/{contract_id}.md", content)

    def get_draft(self, contract_id: str) -> str | None:
        """Read a draft contract."""
        return self.read_file(f"drafts/{contract_id}.md")

    def update_discussion(self, contract_id: str, summary: dict) -> None:
        """Update discussion summary for a contract."""
        self.write_json(f"drafts/{contract_id}_discussion.json", summary)

    def get_discussion(self, contract_id: str) -> dict | None:
        """Read discussion summary."""
        return self.read_json(f"drafts/{contract_id}_discussion.json")

    # ── Participants ────────────────────────────────────────────────

    def get_participant(self, username: str) -> str | None:
        """Read participant profile."""
        return self.read_file(f"participants/{username}.md")

    def update_participant(self, username: str, content: str) -> None:
        """Write participant profile."""
        self.write_file(f"participants/{username}.md", content)

    def list_participants(self, *, active_only: bool = True) -> list[str]:
        """List participant usernames.

        If participants/index.json exists, prefer it.
        Otherwise fall back to filenames.
        """
        idx = self.read_json("participants/index.json")
        if idx and isinstance(idx, dict) and "participants" in idx:
            users = []
            for p in idx.get("participants", []):
                if not isinstance(p, dict):
                    continue
                if active_only and p.get("active") is False:
                    continue
                if p.get("username"):
                    users.append(p["username"])
            return users

        pdir = self._path("participants")
        if not os.path.isdir(pdir):
            return []
        return [
            f.replace(".md", "")
            for f in os.listdir(pdir)
            if f.endswith(".md")
        ]

    def upsert_participant_index(self, username: str, data: dict) -> None:
        idx = self.read_json("participants/index.json") or {"participants": []}
        items = idx.get("participants") or []
        found = False
        for i, p in enumerate(items):
            if p.get("username") == username:
                items[i] = {**p, **data, "username": username}
                found = True
                break
        if not found:
            items.append({"username": username, **data})
        idx["participants"] = items
        self.write_json("participants/index.json", idx)

    def set_participant_active(self, username: str, active: bool) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        patch = {"active": active}
        if active:
            patch.setdefault("joined_at", now)
            patch["left_at"] = None
        else:
            patch.setdefault("left_at", now)
        self.upsert_participant_index(username, patch)

    def is_participant_active(self, username: str) -> bool:
        """Return whether a participant is active in the channel.

        If no index exists, default to True.
        """
        idx = self.read_json("participants/index.json")
        if not idx or not isinstance(idx, dict) or "participants" not in idx:
            return True
        for p in idx.get("participants", []):
            if isinstance(p, dict) and p.get("username") == username:
                return p.get("active") is not False
        return True

    def is_participant_onboarded(self, username: str) -> bool:
        """Return whether a participant was already onboarded.

        If no index exists, default to False.
        """
        idx = self.read_json("participants/index.json")
        if not idx or not isinstance(idx, dict) or "participants" not in idx:
            return False
        for p in idx.get("participants", []):
            if isinstance(p, dict) and p.get("username") == username:
                return p.get("onboarded") is True
        return False

    def set_participant_onboarded(self, username: str, onboarded: bool = True) -> None:
        patch = {"onboarded": onboarded}
        self.upsert_participant_index(username, patch)

    # ── Decisions ───────────────────────────────────────────────────

    def save_decision(self, data: dict) -> None:
        """Append a decision to the journal."""
        if "date" not in data:
            data["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.append_jsonl("memory/decisions.jsonl", data)

    # ── Reminders ───────────────────────────────────────────────────

    def get_reminders(self) -> list[dict]:
        """Get all active reminders."""
        data = self.read_json("tasks/reminders.json")
        if data and "reminders" in data:
            return data["reminders"]
        return []

    def save_reminders(self, reminders: list[dict]) -> None:
        """Save reminders list."""
        self.write_json("tasks/reminders.json", {"reminders": reminders})

    # ── Queue ───────────────────────────────────────────────────────

    def get_queue(self) -> list[dict]:
        """Get contract queue."""
        data = self.read_json("tasks/queue.json")
        if data and "queue" in data:
            return data["queue"]
        return []

    def save_queue(self, queue: list[dict]) -> None:
        """Save contract queue."""
        self.write_json("tasks/queue.json", {"queue": queue})

    # ── Load multiple files for context ─────────────────────────────

    def load_files(self, paths: list[str]) -> str:
        """Load multiple files and concatenate as context block."""
        parts = []
        for p in paths:
            content = self.read_file(p)
            if content:
                parts.append(f"--- {p} ---\n{content}")
        return "\n\n".join(parts)
