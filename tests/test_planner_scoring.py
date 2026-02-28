"""Tests for planner_scoring.py — priority formula and ranking."""

import pytest

from src.planner_scoring import (
    tree_depth_score,
    queue_priority_score,
    blocker_age_score,
    stakeholder_availability_score,
    conflict_score,
    in_progress_boost_score,
    compute_priority_score,
    rank_candidates,
    ScoredCandidate,
)


class TestTreeDepthScore:
    def test_root_node(self):
        assert tree_depth_score(0) == 1.0

    def test_max_depth(self):
        assert tree_depth_score(6) == 0.0

    def test_mid_depth(self):
        assert tree_depth_score(3) == 0.5

    def test_none_depth(self):
        assert tree_depth_score(None) == 0.0

    def test_beyond_max(self):
        assert tree_depth_score(10) == 0.0


class TestQueuePriorityScore:
    def test_top_priority(self):
        assert queue_priority_score(1) == 1.0

    def test_max_priority(self):
        assert queue_priority_score(20) == 0.0

    def test_mid_priority(self):
        s = queue_priority_score(10)
        assert 0.4 < s < 0.6

    def test_none_priority(self):
        assert queue_priority_score(None) == 0.0


class TestBlockerAgeScore:
    def test_zero_days(self):
        assert blocker_age_score(0) == 0.0

    def test_fourteen_days(self):
        assert blocker_age_score(14) == 1.0

    def test_seven_days(self):
        assert blocker_age_score(7) == 0.5

    def test_over_fourteen(self):
        assert blocker_age_score(30) == 1.0


class TestBooleanScores:
    def test_stakeholder_available(self):
        assert stakeholder_availability_score(True) == 1.0
        assert stakeholder_availability_score(False) == 0.0

    def test_conflict(self):
        assert conflict_score(True) == 1.0
        assert conflict_score(False) == 0.0

    def test_in_progress(self):
        assert in_progress_boost_score(True) == 1.0
        assert in_progress_boost_score(False) == 0.0


class TestComputePriorityScore:
    def test_all_zeros(self):
        score, breakdown = compute_priority_score(
            depth=None, priority=None, days_blocked=0,
            stakeholder_available=False, has_conflicts=False,
            is_in_progress=False,
        )
        assert score == 0.0
        assert all(v == 0.0 for v in breakdown.values())

    def test_all_max(self):
        score, breakdown = compute_priority_score(
            depth=0, priority=1, days_blocked=14,
            stakeholder_available=True, has_conflicts=True,
            is_in_progress=True,
        )
        assert score == 1.0

    def test_only_tree_depth(self):
        score, _ = compute_priority_score(depth=0)
        assert score == pytest.approx(0.30 + 0.15, abs=0.01)  # tree_depth=1.0*0.30 + stakeholder=1.0*0.15

    def test_score_range(self):
        score, _ = compute_priority_score(
            depth=2, priority=5, days_blocked=3,
            stakeholder_available=True, has_conflicts=True,
        )
        assert 0.0 <= score <= 1.0

    def test_breakdown_keys(self):
        _, breakdown = compute_priority_score()
        assert set(breakdown.keys()) == {
            "tree_depth", "queue_priority", "blocker_age",
            "stakeholder_avail", "has_conflicts", "in_progress",
        }


class TestRankCandidates:
    def test_ranking_order(self):
        candidates = [
            ScoredCandidate("a", "A", 0.3, {}, "new_contract"),
            ScoredCandidate("b", "B", 0.8, {}, "conflict_resolution"),
            ScoredCandidate("c", "C", 0.5, {}, "stale_review"),
        ]
        ranked = rank_candidates(candidates)
        assert ranked[0].contract_id == "b"
        assert ranked[1].contract_id == "c"
        assert ranked[2].contract_id == "a"

    def test_empty_list(self):
        assert rank_candidates([]) == []

    def test_single_candidate(self):
        c = ScoredCandidate("x", "X", 0.5, {}, "new_contract")
        assert rank_candidates([c]) == [c]

    def test_equal_scores(self):
        candidates = [
            ScoredCandidate("a", "A", 0.5, {}, "new_contract"),
            ScoredCandidate("b", "B", 0.5, {}, "new_contract"),
        ]
        ranked = rank_candidates(candidates)
        assert len(ranked) == 2
