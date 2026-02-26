"""Tests for interactive approval workflow — request_approval + approve_contract."""

import json
from unittest.mock import MagicMock

import pytest

from src.memory import Memory
from src.tools import ToolExecutor
from src.governance import ApprovalState, ApprovalVote


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "index.json").write_text(
        json.dumps({"contracts": [
            {"id": "test_metric", "name": "Test Metric", "status": "draft", "tier": "tier_2"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "test_metric.md").write_text(
        "# Data Contract: Test Metric\n## Определение\nОписание.\n## Формула\ncount(x)\n",
        encoding="utf-8",
    )
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "governance.json").write_text(
        json.dumps({
            "tiers": {
                "tier_1": {
                    "approval_required": ["ceo", "cfo", "circle_lead"],
                    "consensus_threshold": 1.0,
                },
                "tier_2": {
                    "approval_required": ["circle_lead", "data_lead"],
                    "consensus_threshold": 0.8,
                },
                "tier_3": {
                    "approval_required": ["data_lead"],
                    "consensus_threshold": 0.6,
                },
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "context" / "roles.json").write_text(
        json.dumps({"roles": {
            "circle_lead": ["korabovtsev"],
            "data_lead": ["pavelpetrin"],
        }}),
        encoding="utf-8",
    )
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "roles.json").write_text(
        json.dumps({"roles": {}}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def memory(data_dir):
    m = Memory()
    m.base_dir = str(data_dir)
    return m


@pytest.fixture
def mm():
    mock = MagicMock()
    mock.channel_id = "chan123"
    mock.send_to_channel.return_value = {"id": "post_id"}
    return mock


@pytest.fixture
def executor(memory, mm):
    return ToolExecutor(memory=memory, mattermost_client=mm)


# ── ApprovalState unit tests ───────────────────────────────────────────────

class TestApprovalState:
    def test_empty_state(self):
        state = ApprovalState(tier="tier_2", required_roles=["circle_lead", "data_lead"], threshold=0.8)
        assert not state.is_quorum_met()
        assert state.missing_roles() == ["circle_lead", "data_lead"]

    def test_partial_approval(self):
        state = ApprovalState(
            tier="tier_2",
            required_roles=["circle_lead", "data_lead"],
            threshold=0.8,
            approvals=[ApprovalVote(username="korabovtsev", role="circle_lead", approved_at="2026-01-01")],
        )
        # 1/2 = 50% < 80% → not met
        assert not state.is_quorum_met()
        assert state.missing_roles() == ["data_lead"]

    def test_full_approval(self):
        state = ApprovalState(
            tier="tier_2",
            required_roles=["circle_lead", "data_lead"],
            threshold=0.8,
            approvals=[
                ApprovalVote(username="korabovtsev", role="circle_lead", approved_at="2026-01-01"),
                ApprovalVote(username="pavelpetrin", role="data_lead", approved_at="2026-01-01"),
            ],
        )
        assert state.is_quorum_met()
        assert state.missing_roles() == []

    def test_tier1_requires_all(self):
        state = ApprovalState(
            tier="tier_1",
            required_roles=["ceo", "cfo", "circle_lead"],
            threshold=1.0,
            approvals=[
                ApprovalVote(username="boss", role="ceo", approved_at="2026-01-01"),
                ApprovalVote(username="cfoguy", role="cfo", approved_at="2026-01-01"),
            ],
        )
        # 2/3 = 67% but threshold=1.0 requires ALL
        assert not state.is_quorum_met()
        assert state.missing_roles() == ["circle_lead"]

    def test_no_required_roles(self):
        state = ApprovalState(tier="tier_3", required_roles=[], threshold=0.6)
        assert state.is_quorum_met()

    def test_roundtrip_serialization(self):
        state = ApprovalState(
            tier="tier_2",
            required_roles=["circle_lead"],
            threshold=0.8,
            requested_at="2026-02-26T10:00:00",
            approvals=[ApprovalVote(username="testuser", role="circle_lead", approved_at="2026-02-26T11:00:00")],
        )
        d = state.to_dict()
        restored = ApprovalState.from_dict(d)
        assert restored.tier == "tier_2"
        assert len(restored.approvals) == 1
        assert restored.approvals[0].username == "testuser"
        assert restored.is_quorum_met()

    def test_from_dict_none(self):
        state = ApprovalState.from_dict(None)
        assert state.tier == ""
        assert state.required_roles == []


# ── request_approval tool ──────────────────────────────────────────────────

class TestRequestApproval:
    def test_starts_approval(self, executor, mm):
        result = executor.execute("request_approval", {"contract_id": "test_metric"})
        assert result["success"] is True
        assert result["tier"] == "tier_2"
        assert "circle_lead" in result["required_roles"]
        assert "data_lead" in result["required_roles"]
        assert result["quorum_met"] is False

        # Notification sent
        mm.send_to_channel.assert_called_once()
        msg = mm.send_to_channel.call_args[0][0]
        assert "test_metric" in msg
        assert "@korabovtsev" in msg
        assert "@pavelpetrin" in msg

    def test_saves_approval_state(self, executor, memory):
        executor.execute("request_approval", {"contract_id": "test_metric"})
        discussion = memory.get_discussion("test_metric")
        assert "approval_state" in discussion
        state = ApprovalState.from_dict(discussion["approval_state"])
        assert state.tier == "tier_2"
        assert state.requested_at is not None

    def test_missing_contract(self, executor):
        result = executor.execute("request_approval", {"contract_id": "nonexistent"})
        assert "error" in result

    def test_preserves_existing_approvals(self, executor, memory):
        """If re-requesting, existing approvals are kept."""
        # First request
        executor.execute("request_approval", {"contract_id": "test_metric"})
        # Simulate an approval
        discussion = memory.get_discussion("test_metric")
        state = ApprovalState.from_dict(discussion["approval_state"])
        state.approvals.append(ApprovalVote(username="korabovtsev", role="circle_lead", approved_at="2026-01-01"))
        discussion["approval_state"] = state.to_dict()
        memory.update_discussion("test_metric", discussion)
        # Re-request
        result = executor.execute("request_approval", {"contract_id": "test_metric"})
        assert "korabovtsev" in result["existing_approvals"]


# ── approve_contract tool ──────────────────────────────────────────────────

class TestApproveContract:
    def _start_approval(self, executor):
        executor.execute("request_approval", {"contract_id": "test_metric"})

    def test_records_approval(self, executor, memory):
        self._start_approval(executor)
        result = executor.execute("approve_contract", {
            "contract_id": "test_metric",
            "username": "korabovtsev",
        })
        assert result["success"] is True
        assert result["approved_by"] == "korabovtsev"
        assert result["role"] == "circle_lead"
        assert result["quorum_met"] is False  # still missing data_lead

    def test_quorum_reached(self, executor, memory):
        self._start_approval(executor)
        executor.execute("approve_contract", {"contract_id": "test_metric", "username": "korabovtsev"})
        result = executor.execute("approve_contract", {"contract_id": "test_metric", "username": "pavelpetrin"})
        assert result["success"] is True
        assert result["quorum_met"] is True
        assert "финализировать" in result["message"].lower() or "кворум" in result["message"].lower()

    def test_duplicate_vote_ignored(self, executor, memory):
        self._start_approval(executor)
        executor.execute("approve_contract", {"contract_id": "test_metric", "username": "korabovtsev"})
        result = executor.execute("approve_contract", {"contract_id": "test_metric", "username": "korabovtsev"})
        assert result["success"] is True
        assert result["already_approved"] is True

    def test_wrong_role_rejected(self, executor, memory):
        self._start_approval(executor)
        result = executor.execute("approve_contract", {"contract_id": "test_metric", "username": "unknown_user"})
        assert "error" in result
        assert "роли" in result["error"]

    def test_no_approval_state_error(self, executor):
        result = executor.execute("approve_contract", {"contract_id": "test_metric", "username": "korabovtsev"})
        assert "error" in result
        assert "не запущено" in result["error"]

    def test_username_normalized(self, executor, memory):
        """@prefix is stripped, case normalized."""
        self._start_approval(executor)
        result = executor.execute("approve_contract", {
            "contract_id": "test_metric",
            "username": "@Korabovtsev",
        })
        assert result["success"] is True
        assert result["approved_by"] == "korabovtsev"

    def test_persisted_to_discussion(self, executor, memory):
        self._start_approval(executor)
        executor.execute("approve_contract", {"contract_id": "test_metric", "username": "korabovtsev"})
        discussion = memory.get_discussion("test_metric")
        state = ApprovalState.from_dict(discussion["approval_state"])
        assert len(state.approvals) == 1
        assert state.approvals[0].username == "korabovtsev"
