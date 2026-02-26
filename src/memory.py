from __future__ import annotations

import json
import logging
import os
import hashlib
import time
from datetime import datetime, timedelta, timezone

from src.config import WRITE_MAX_RETRIES, WRITE_BACKOFF_BASE, THREAD_TTL_DAYS

logger = logging.getLogger(__name__)


class Memory:
    _ACTIVE_THREADS_FILE = "tasks/active_threads.json"

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

    @staticmethod
    def _retry_io(fn, description: str) -> None:
        """Retry a write operation with exponential backoff on OSError."""
        for attempt in range(1, WRITE_MAX_RETRIES + 1):
            try:
                fn()
                return
            except OSError as e:
                if attempt < WRITE_MAX_RETRIES:
                    delay = WRITE_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "I/O error on %s (attempt %d/%d), retrying in %.1fs: %s",
                        description, attempt, WRITE_MAX_RETRIES, delay, e,
                    )
                    time.sleep(delay)
                else:
                    logger.error("I/O error on %s after %d attempts: %s", description, WRITE_MAX_RETRIES, e)
                    raise

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
        """Write content to a file relative to base_dir (with retry)."""
        full = self._path(path)

        def _do():
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

        self._retry_io(_do, f"write_file({path})")
        logger.debug("Written: %s", full)

    def append_jsonl(self, path: str, data: dict) -> None:
        """Append a JSON line to a JSONL file (with retry)."""
        full = self._path(path)
        line = json.dumps(data, ensure_ascii=False) + "\n"

        def _do():
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "a", encoding="utf-8") as f:
                f.write(line)

        self._retry_io(_do, f"append_jsonl({path})")

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
        """Write data as formatted JSON (with retry)."""
        full = self._path(path)
        serialized = json.dumps(data, ensure_ascii=False, indent=2)

        def _do():
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(serialized)

        self._retry_io(_do, f"write_json({path})")

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

        Files written atomically via write_batch:
        - contracts/<id>.md (current)
        - contracts/versions/<id>/<ts>.md (snapshots)
        Then history appended:
        - contracts/versions/<id>/history.jsonl (metadata)
        """
        current_path = f"contracts/{contract_id}.md"
        prev = self.read_file(current_path)
        ts = self._utc_ts()

        versions_dir = f"contracts/versions/{contract_id}"
        history_path = f"{versions_dir}/history.jsonl"

        # Collect all file writes
        writes: list[tuple[str, str]] = []
        history_entries: list[dict] = []

        if prev is not None:
            prev_ts = f"{ts}_prev"
            writes.append((f"{versions_dir}/{prev_ts}.md", prev))
            history_entries.append({
                "ts": prev_ts,
                "kind": "previous",
                "sha256": self._sha256(prev),
                "bytes": len(prev.encode("utf-8")),
            })

        # Current + snapshot
        writes.append((current_path, content))
        writes.append((f"{versions_dir}/{ts}.md", content))
        history_entries.append({
            "ts": ts,
            "kind": "current",
            "sha256": self._sha256(content),
            "bytes": len((content or "").encode("utf-8")),
        })

        # Atomic write of all .md files
        self.write_batch(writes)

        # Append history entries (sequential, after files are in place)
        for entry in history_entries:
            self.append_jsonl(history_path, entry)

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

    # ── Suggestions ─────────────────────────────────────────────────

    def get_suggestions(self) -> list[dict]:
        """Get all suggestions from tasks/suggestions.json."""
        data = self.read_json("tasks/suggestions.json")
        if data and "suggestions" in data:
            return data["suggestions"]
        return []

    def save_suggestions(self, suggestions: list[dict]) -> None:
        """Save suggestions list."""
        self.write_json("tasks/suggestions.json", {"suggestions": suggestions})

    # ── Contract summaries ─────────────────────────────────────────

    def get_summaries(self) -> dict:
        data = self.read_json("contracts/summaries.json")
        return data if isinstance(data, dict) else {}

    def save_summaries(self, data: dict) -> None:
        self.write_json("contracts/summaries.json", data)

    def update_summary(self, contract_id: str, summary_data: dict) -> None:
        summaries = self.get_summaries()
        summaries[contract_id] = summary_data
        self.save_summaries(summaries)

    # ── Active threads ────────────────────────────────────────────────

    def get_active_thread(self, contract_id: str) -> str | None:
        """Return root_post_id of active thread for contract, or None if expired/missing."""
        data = self.read_json(self._ACTIVE_THREADS_FILE)
        if not isinstance(data, dict):
            return None
        threads = data.get("threads")
        if not isinstance(threads, dict):
            return None
        entry = threads.get(contract_id)
        if not isinstance(entry, dict):
            return None
        updated_at = entry.get("updated_at")
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at)
                if datetime.now(timezone.utc) - dt > timedelta(days=THREAD_TTL_DAYS):
                    return None
            except (ValueError, TypeError):
                pass
        return entry.get("root_post_id") or None

    def set_active_thread(self, contract_id: str, root_post_id: str) -> None:
        """Register or update the active thread for a contract."""
        data = self.read_json(self._ACTIVE_THREADS_FILE)
        if not isinstance(data, dict):
            data = {"threads": {}}
        threads = data.get("threads")
        if not isinstance(threads, dict):
            threads = {}
            data["threads"] = threads
        threads[contract_id] = {
            "root_post_id": root_post_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.write_json(self._ACTIVE_THREADS_FILE, data)

    def cleanup_expired_threads(self) -> int:
        """Remove expired entries from active_threads.json. Returns count removed."""
        data = self.read_json(self._ACTIVE_THREADS_FILE)
        if not isinstance(data, dict):
            return 0
        threads = data.get("threads")
        if not isinstance(threads, dict) or not threads:
            return 0

        now = datetime.now(timezone.utc)
        expired = []
        for cid, entry in threads.items():
            if not isinstance(entry, dict):
                expired.append(cid)
                continue
            updated_at = entry.get("updated_at")
            if not updated_at:
                expired.append(cid)
                continue
            try:
                dt = datetime.fromisoformat(updated_at)
                if now - dt > timedelta(days=THREAD_TTL_DAYS):
                    expired.append(cid)
            except (ValueError, TypeError):
                expired.append(cid)

        if not expired:
            return 0

        for cid in expired:
            del threads[cid]

        self.write_json(self._ACTIVE_THREADS_FILE, data)
        logger.debug("Cleaned up %d expired threads", len(expired))
        return len(expired)

    # ── Atomic write batch ────────────────────────────────────────────

    def write_batch(self, writes: list[tuple[str, str]]) -> None:
        """Write multiple files atomically using temp + rename.

        Args:
            writes: list of (relative_path, content) tuples.

        All files are written to temp paths first, then renamed into place.
        If any rename fails, already-renamed files remain (best-effort).
        """
        import tempfile

        staged: list[tuple[str, str]] = []  # (temp_path, final_path)

        try:
            for rel_path, content in writes:
                final = self._path(rel_path)
                os.makedirs(os.path.dirname(final), exist_ok=True)
                fd, tmp = tempfile.mkstemp(
                    dir=os.path.dirname(final),
                    prefix=".tmp_",
                    suffix=".md" if rel_path.endswith(".md") else ".json",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception:
                    os.close(fd)
                    raise
                staged.append((tmp, final))
        except Exception:
            # Clean up any temp files
            for tmp, _ in staged:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

        # Rename all temp files into place
        for tmp, final in staged:
            os.replace(tmp, final)

    # ── Audit log ─────────────────────────────────────────────────────

    def audit_log(self, action: str, **kwargs) -> None:
        """Append an audit entry to memory/audit.jsonl."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **kwargs,
        }
        try:
            self.append_jsonl("memory/audit.jsonl", entry)
        except Exception as e:
            logger.warning("Audit log write failed: %s", e)

    # ── Load multiple files for context ─────────────────────────────

    def load_files(self, paths: list[str]) -> str:
        """Load multiple files and concatenate as context block."""
        parts = []
        for p in paths:
            content = self.read_file(p)
            if content:
                parts.append(f"--- {p} ---\n{content}")
        return "\n\n".join(parts)
