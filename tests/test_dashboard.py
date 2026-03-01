"""Tests for dashboard.py — API endpoints via FastAPI TestClient."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.memory import Memory
from src.dashboard import create_app


@pytest.fixture
def data_dir(tmp_path):
    """Set up a minimal data directory for dashboard tests."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text(json.dumps({
        "contracts": [
            {"id": "churn", "name": "Churn Rate", "status": "agreed", "file": "contracts/churn.md", "agreed_date": "2026-02-21"},
            {"id": "revenue", "name": "Revenue", "status": "in_review", "file": "contracts/revenue.md"},
            {"id": "mau", "name": "MAU", "status": "draft", "file": "contracts/mau.md"},
        ]
    }), encoding="utf-8")
    (tmp_path / "contracts" / "churn.md").write_text(
        "# Data Contract: Churn Rate\n\n## Статус\nСогласован\n\n## Определение\nChurn = lost / total\n\n## Формула\nChurn = Users_lost / Users_total * 100%\n\n## Источник данных\nBigQuery\n\n## Связь с Extra Time\nChurn → Retention → MAU → Extra Time",
        encoding="utf-8",
    )
    (tmp_path / "contracts" / "revenue.md").write_text(
        "# Data Contract: Revenue\n\n## Статус\nНа рассмотрении\n\n## Определение\nTotal revenue\n\n## Формула\nRevenue = sum(transactions)",
        encoding="utf-8",
    )
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "mau.md").write_text(
        "# Data Contract: MAU\n\n## Статус\nЧерновик\n\n## Определение\nMonthly Active Users",
        encoding="utf-8",
    )

    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "planner_state.json").write_text(json.dumps({
        "initiatives": [
            {
                "id": "init_001",
                "type": "new_contract",
                "contract_id": "mau",
                "priority_score": 0.75,
                "status": "active",
                "created_at": "2026-02-28T09:00:00Z",
                "updated_at": "2026-02-28T10:00:00Z",
                "thread_id": "post123",
                "stakeholders": ["alice"],
                "waiting_for": [],
                "actions_taken": [{"action": "start_thread", "at": "2026-02-28T09:00:00Z"}],
                "actions_today": 1,
            },
            {
                "id": "init_002",
                "type": "conflict_resolution",
                "contract_id": "revenue",
                "priority_score": 0.60,
                "status": "completed",
                "created_at": "2026-02-20T09:00:00Z",
                "updated_at": "2026-02-25T10:00:00Z",
                "thread_id": None,
                "stakeholders": [],
                "waiting_for": [],
                "actions_taken": [],
                "actions_today": 0,
            },
        ],
        "daily_stats": {"2026-02-28": {"threads_started": 1, "messages_sent": 2}},
        "cooldowns": {},
        "last_plan_at": "2026-02-28T09:00:00Z",
    }), encoding="utf-8")

    (tmp_path / "tasks" / "reminders.json").write_text(json.dumps({
        "reminders": [
            {
                "contract_id": "revenue",
                "target_user": "bob",
                "escalation_step": 1,
                "next_reminder": "2026-03-02T10:00:00Z",
                "question_summary": "agree on formula?",
            }
        ]
    }), encoding="utf-8")

    (tmp_path / "tasks" / "queue.json").write_text(json.dumps({
        "queue": [
            {"id": "revenue", "priority": 1, "reason": "blocked", "status": "in_progress"}
        ]
    }), encoding="utf-8")

    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "metrics_tree.md").write_text(
        "## Дерево\n```\nExtra Time\n├── MAU ← DATA CONTRACT\n│   ├── Activation ← DATA CONTRACT\n│   └── Retention ← DATA CONTRACT ✅\n└── Revenue ← DATA CONTRACT ✅\n```",
        encoding="utf-8",
    )

    (tmp_path / "participants").mkdir()
    (tmp_path / "participants" / "index.json").write_text(json.dumps({
        "participants": [
            {"username": "alice", "active": True, "onboarded": True},
            {"username": "bob", "active": True, "onboarded": False},
            {"username": "charlie", "active": False, "onboarded": True},
        ]
    }), encoding="utf-8")

    (tmp_path / "memory").mkdir()
    audit_lines = [
        json.dumps({"ts": "2026-02-28T10:00:00Z", "action": "contract_finalized", "contract_id": "churn"}),
        json.dumps({"ts": "2026-02-28T11:00:00Z", "action": "reminder_sent", "contract_id": "revenue"}),
    ]
    (tmp_path / "memory" / "audit.jsonl").write_text("\n".join(audit_lines) + "\n", encoding="utf-8")

    planner_lines = [
        json.dumps({"ts": "2026-02-28T09:00:00Z", "event": "cycle_start"}),
        json.dumps({"ts": "2026-02-28T09:05:00Z", "event": "cycle_complete", "candidates": 5, "actions": 1}),
    ]
    (tmp_path / "tasks" / "planner_log.jsonl").write_text("\n".join(planner_lines) + "\n", encoding="utf-8")

    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


@pytest.fixture
def client(memory):
    app = create_app(memory)
    return TestClient(app, root_path="/dashboard")


# ── Tests ────────────────────────────────────────────────────────────────────


class TestOverview:
    def test_returns_contract_counts(self, client):
        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_contracts"] == 3
        assert data["by_status"]["agreed"] == 1
        assert data["by_status"]["in_review"] == 1
        assert data["by_status"]["draft"] == 1

    def test_returns_active_initiatives_count(self, client):
        data = client.get("/api/overview").json()
        assert data["active_initiatives"] == 1  # only init_001 is active

    def test_returns_tree_coverage(self, client):
        data = client.get("/api/overview").json()
        cov = data["tree_coverage"]
        assert cov["total_markers"] == 4  # MAU, Activation, Retention, Revenue
        assert cov["agreed"] == 2  # Retention + Revenue
        assert cov["uncovered"] == 2  # MAU, Activation


class TestContracts:
    def test_list(self, client):
        resp = client.get("/api/contracts")
        assert resp.status_code == 200
        contracts = resp.json()["contracts"]
        assert len(contracts) == 3
        ids = [c["id"] for c in contracts]
        assert "churn" in ids

    def test_detail_agreed(self, client):
        resp = client.get("/api/contracts/churn")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "churn"
        assert "Churn Rate" in data["markdown"]

    def test_detail_draft_fallback(self, client):
        resp = client.get("/api/contracts/mau")
        assert resp.status_code == 200
        assert "MAU" in resp.json()["markdown"]

    def test_detail_not_found(self, client):
        resp = client.get("/api/contracts/nonexistent")
        assert resp.status_code == 404


class TestTree:
    def test_returns_tree(self, client):
        resp = client.get("/api/tree")
        assert resp.status_code == 200
        tree = resp.json()["tree"]
        assert tree is not None
        assert tree["name"] == "Extra Time"
        assert len(tree["children"]) == 2  # MAU, Revenue

    def test_tree_node_fields(self, client):
        tree = client.get("/api/tree").json()["tree"]
        mau = tree["children"][0]
        assert mau["short_name"] == "MAU"
        assert mau["has_contract"] is True
        assert mau["is_agreed"] is False

    def test_agreed_node(self, client):
        tree = client.get("/api/tree").json()["tree"]
        revenue = tree["children"][1]
        assert revenue["has_contract"] is True
        assert revenue["is_agreed"] is True


class TestConflicts:
    def test_returns_conflicts(self, client):
        resp = client.get("/api/conflicts")
        assert resp.status_code == 200
        conflicts = resp.json()["conflicts"]
        assert isinstance(conflicts, list)
        # revenue is missing formula/definition sections, should generate conflicts
        types = [c["type"] for c in conflicts]
        assert any("missing" in t for t in types)

    def test_conflict_fields(self, client):
        conflicts = client.get("/api/conflicts").json()["conflicts"]
        for c in conflicts:
            assert "type" in c
            assert "severity" in c
            assert "title" in c
            assert "contracts" in c


class TestPlanner:
    def test_returns_state(self, client):
        resp = client.get("/api/planner")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["initiatives"]) == 2
        assert data["last_plan_at"] == "2026-02-28T09:00:00Z"

    def test_initiative_fields(self, client):
        data = client.get("/api/planner").json()
        active = [i for i in data["initiatives"] if i["status"] == "active"]
        assert len(active) == 1
        assert active[0]["contract_id"] == "mau"


class TestScheduler:
    def test_returns_reminders_and_queue(self, client):
        resp = client.get("/api/scheduler")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["reminders"]) == 1
        assert data["reminders"][0]["target_user"] == "bob"
        assert len(data["queue"]) == 1
        assert data["queue"][0]["id"] == "revenue"


class TestActivity:
    def test_returns_merged_activity(self, client):
        resp = client.get("/api/activity")
        assert resp.status_code == 200
        activity = resp.json()["activity"]
        assert len(activity) == 4  # 2 audit + 2 planner
        # Sorted by ts desc
        assert activity[0]["ts"] >= activity[-1]["ts"]

    def test_source_annotation(self, client):
        activity = client.get("/api/activity").json()["activity"]
        sources = {e["_source"] for e in activity}
        assert "audit" in sources
        assert "planner" in sources


class TestParticipants:
    def test_returns_participants(self, client):
        resp = client.get("/api/participants")
        assert resp.status_code == 200
        participants = resp.json()["participants"]
        assert len(participants) == 3
        usernames = [p["username"] for p in participants]
        assert "alice" in usernames

    def test_participant_fields(self, client):
        participants = client.get("/api/participants").json()["participants"]
        alice = next(p for p in participants if p["username"] == "alice")
        assert alice["active"] is True
        assert alice["onboarded"] is True


class TestIndexPage:
    def test_serves_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "DAAI" in resp.text
