"""ContinuousPlanner: background strategic planning cycle.

Runs once per workday at a configured time. Gathers state, scores candidates,
calls LLM for strategic decisions, checks rate limits, executes actions.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime, timezone, timedelta

from src.config import (
    PLANNER_RUN_TIME,
    PLANNER_WORKDAYS,
    PLANNER_MAX_ACTIVE_INITIATIVES,
    PLANNER_MAX_NEW_THREADS_PER_DAY,
    PLANNER_MAX_MESSAGES_PER_DAY,
    PLANNER_MAX_ACTIONS_PER_INITIATIVE_PER_DAY,
    PLANNER_COOLDOWN_HOURS,
    PLANNER_WAIT_BEFORE_FOLLOWUP_HOURS,
    PLANNER_STALE_INITIATIVE_DAYS,
)
from src.planner_scoring import (
    ScoredCandidate,
    compute_priority_score,
    rank_candidates,
)
from src.planner_actions import ActionDispatcher

logger = logging.getLogger(__name__)


class ContinuousPlanner:
    def __init__(self, memory, mattermost_client, llm_client):
        self.memory = memory
        self.mm = mattermost_client
        self.llm = llm_client
        self.dispatcher = ActionDispatcher(memory, mattermost_client, llm_client)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────

    def start(self):
        """Run the planner loop. Blocks indefinitely (run in a daemon thread)."""
        logger.info("ContinuousPlanner started: run_time=%s, workdays=%s", PLANNER_RUN_TIME, PLANNER_WORKDAYS)

        while True:
            try:
                self._wait_for_next_run()
                self._run_cycle()
            except Exception as e:
                logger.error("Planner cycle error: %s", e, exc_info=True)
            time.sleep(60)  # prevent tight loop on error

    def notify_thread_activity(self, root_id: str, username: str):
        """Called by Listener when someone replies in a tracked thread.

        Updates initiative state: removes user from waiting_for,
        transitions waiting_response → active.
        """
        with self._lock:
            try:
                state = self.memory.get_planner_state()
                now = datetime.now(timezone.utc)
                changed = False

                for init in state.get("initiatives", []):
                    if init.get("thread_id") != root_id:
                        continue
                    if init.get("status") not in ("active", "waiting_response"):
                        continue

                    # Remove user from waiting_for
                    waiting = init.get("waiting_for", [])
                    if username in waiting:
                        waiting.remove(username)
                        init["waiting_for"] = waiting
                        changed = True

                    # Transition waiting_response → active
                    if init.get("status") == "waiting_response":
                        init["status"] = "active"
                        changed = True

                    init["last_external_activity_at"] = now.isoformat()
                    init["updated_at"] = now.isoformat()
                    changed = True

                if changed:
                    self.memory.save_planner_state(state)
                    logger.info("Planner: thread activity from @%s in %s", username, root_id)

            except Exception as e:
                logger.error("Planner notify_thread_activity error: %s", e, exc_info=True)

    # ── Scheduling ────────────────────────────────────────────────────

    def _wait_for_next_run(self):
        """Sleep until next scheduled run time on a workday."""
        if PLANNER_RUN_TIME == "now":
            return

        while True:
            now = datetime.now(timezone.utc)

            # Check if today is a workday
            if now.weekday() not in PLANNER_WORKDAYS:
                # Sleep until midnight, then recheck
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                sleep_seconds = (tomorrow - now).total_seconds()
                logger.debug("Planner: not a workday, sleeping %.0fs", sleep_seconds)
                time.sleep(min(sleep_seconds + 1, 3600))
                continue

            # Parse target time
            try:
                hour, minute = map(int, PLANNER_RUN_TIME.split(":"))
            except (ValueError, AttributeError):
                hour, minute = 9, 0

            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if now >= target:
                # Check if we already ran today
                state = self.memory.get_planner_state()
                last_plan = state.get("last_plan_at")
                if last_plan:
                    try:
                        last_dt = datetime.fromisoformat(last_plan)
                        if last_dt.date() == now.date():
                            # Already ran today, sleep until tomorrow
                            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                            sleep_seconds = (tomorrow - now).total_seconds()
                            logger.debug("Planner: already ran today, sleeping %.0fs", sleep_seconds)
                            time.sleep(min(sleep_seconds + 1, 3600))
                            continue
                    except (ValueError, TypeError):
                        pass
                # Target time has passed today and we haven't run yet — run now
                return

            # Sleep until target time
            sleep_seconds = (target - now).total_seconds()
            logger.debug("Planner: sleeping %.0fs until %s", sleep_seconds, PLANNER_RUN_TIME)
            time.sleep(min(sleep_seconds + 1, 3600))

    # ── Main cycle ────────────────────────────────────────────────────

    def _run_cycle(self):
        """Execute one planning cycle: gather → score → plan → check → execute → persist."""
        with self._lock:
            logger.info("Planner: starting cycle")
            now = datetime.now(timezone.utc)

            # 1. GATHER
            gathered = self._gather()

            # 2. Housekeeping — abandon stale initiatives
            state = self.memory.get_planner_state()
            self._abandon_stale_initiatives(state, now)

            # 3. SCORE
            candidates = self._score(gathered, state)

            if not candidates:
                logger.info("Planner: no candidates to act on")
                state["last_plan_at"] = now.isoformat()
                self.memory.save_planner_state(state)
                self.memory.append_planner_log({
                    "event": "cycle_complete",
                    "candidates": 0,
                    "actions": 0,
                })
                return

            # 4. PLAN — LLM strategic call
            actions = self._plan(candidates, gathered, state)

            if not actions:
                logger.info("Planner: LLM returned no actions")
                state["last_plan_at"] = now.isoformat()
                self.memory.save_planner_state(state)
                self.memory.append_planner_log({
                    "event": "cycle_complete",
                    "candidates": len(candidates),
                    "actions": 0,
                })
                return

            # 5. CHECK + EXECUTE
            executed = 0
            today_str = now.strftime("%Y-%m-%d")
            daily = state.get("daily_stats", {}).get(today_str, {"threads_started": 0, "messages_sent": 0})

            for action in actions:
                # Rate limit checks
                if not self._check_limits(action, state, daily, now):
                    logger.info("Planner: rate limit hit, skipping action: %s", action.get("type"))
                    continue

                # Find or create initiative
                initiative = self._get_or_create_initiative(action, state, candidates, now)

                # Execute
                result = self.dispatcher.execute(action, initiative)
                if result:
                    # Update initiative
                    initiative.setdefault("actions_taken", []).append(result)
                    initiative["updated_at"] = now.isoformat()
                    initiative["actions_today"] = initiative.get("actions_today", 0) + 1

                    if action["type"] == "start_thread" and result.get("post_id"):
                        initiative["thread_id"] = result["post_id"]

                    if action["type"] in ("ask_question", "follow_up"):
                        initiative["status"] = "waiting_response"
                        target = action.get("target_user", "")
                        if target and target.startswith("@"):
                            target = target[1:]
                        if target:
                            waiting = initiative.get("waiting_for", [])
                            if target not in waiting:
                                waiting.append(target)
                            initiative["waiting_for"] = waiting
                        initiative["next_action_after"] = (
                            now + timedelta(hours=PLANNER_WAIT_BEFORE_FOLLOWUP_HOURS)
                        ).isoformat()

                    # Update daily stats
                    if action["type"] == "start_thread":
                        daily["threads_started"] = daily.get("threads_started", 0) + 1
                    daily["messages_sent"] = daily.get("messages_sent", 0) + 1

                    # Update cooldown for conflict-related actions
                    if action["type"] in ("propose_resolution", "follow_up"):
                        cooldown_key = f"{action['type']}:{action.get('contract_id', '')}"
                        cooldowns = state.setdefault("cooldowns", {})
                        cooldowns[cooldown_key] = (
                            now + timedelta(hours=PLANNER_COOLDOWN_HOURS)
                        ).isoformat()

                    executed += 1

                    self.memory.append_planner_log({
                        "event": "action_executed",
                        "action": action,
                        "result": result,
                        "initiative_id": initiative.get("id"),
                    })

            # 6. PERSIST
            state.setdefault("daily_stats", {})[today_str] = daily
            state["last_plan_at"] = now.isoformat()
            self.memory.save_planner_state(state)

            self.memory.append_planner_log({
                "event": "cycle_complete",
                "candidates": len(candidates),
                "actions": executed,
            })
            logger.info("Planner: cycle complete, executed %d actions", executed)

    # ── 1. GATHER ────────────────────────────────────────────────────

    def _gather(self) -> dict:
        """Collect all state needed for scoring and planning (no LLM)."""
        from src.analyzer import MetricsAnalyzer
        from src.suggestion_engine import SuggestionEngine

        contracts = self.memory.list_contracts() or []
        tree_md = self.memory.read_file("context/metrics_tree.md") or ""
        queue = self.memory.get_queue()
        reminders = self.memory.get_reminders()

        # Detect conflicts
        analyzer = MetricsAnalyzer(self.memory)
        try:
            conflicts = analyzer.detect_conflicts()
        except Exception as e:
            logger.warning("Planner: conflict detection failed: %s", e)
            conflicts = []

        # Coverage scan
        engine = SuggestionEngine(self.memory)
        try:
            uncovered = engine.coverage_scan()
        except Exception as e:
            logger.warning("Planner: coverage scan failed: %s", e)
            uncovered = []

        # Discussion states
        discussions = {}
        for c in contracts:
            cid = c.get("id", "")
            if cid:
                disc = self.memory.get_discussion(cid)
                if disc:
                    discussions[cid] = disc

        return {
            "contracts": contracts,
            "tree_md": tree_md,
            "queue": queue,
            "reminders": reminders,
            "conflicts": conflicts,
            "uncovered": uncovered,
            "discussions": discussions,
        }

    # ── 2. SCORE ─────────────────────────────────────────────────────

    def _score(self, gathered: dict, state: dict) -> list[ScoredCandidate]:
        """Score and rank candidates for action (no LLM)."""
        candidates: list[ScoredCandidate] = []
        now = datetime.now(timezone.utc)
        queue = gathered.get("queue", [])
        queue_map = {item["id"]: item.get("priority") for item in queue if isinstance(item, dict)}
        contracts = gathered.get("contracts", [])
        contract_map = {c["id"]: c for c in contracts if isinstance(c, dict) and c.get("id")}
        active_initiative_ids = {
            init["contract_id"]
            for init in state.get("initiatives", [])
            if init.get("status") in ("active", "waiting_response", "planned")
        }

        # Uncovered metrics → new_contract candidates
        for uc in gathered.get("uncovered", []):
            cid = uc.contract_id
            if cid in active_initiative_ids:
                continue

            score, breakdown = compute_priority_score(
                depth=uc.tree_path.count("→") if uc.tree_path else None,
                priority=queue_map.get(cid),
                stakeholder_available=True,
                has_conflicts=False,
                is_in_progress=False,
            )
            candidates.append(ScoredCandidate(
                contract_id=cid,
                metric_name=uc.metric_name,
                score=score,
                breakdown=breakdown,
                candidate_type="new_contract",
                tree_depth=uc.tree_path.count("→") if uc.tree_path else None,
                stakeholders=uc.stakeholders,
            ))

        # Conflicts → conflict_resolution candidates
        seen_conflict_contracts: set[str] = set()
        for conflict in gathered.get("conflicts", []):
            for cid in conflict.contracts:
                if cid in seen_conflict_contracts:
                    continue
                seen_conflict_contracts.add(cid)

                # Check cooldown
                cooldown_key = f"propose_resolution:{cid}"
                cooldown_until = state.get("cooldowns", {}).get(cooldown_key)
                if cooldown_until:
                    try:
                        if datetime.fromisoformat(cooldown_until) > now:
                            continue
                    except (ValueError, TypeError):
                        pass

                c_info = contract_map.get(cid, {})
                score, breakdown = compute_priority_score(
                    priority=queue_map.get(cid),
                    stakeholder_available=True,
                    has_conflicts=True,
                    is_in_progress=cid in active_initiative_ids,
                )
                candidates.append(ScoredCandidate(
                    contract_id=cid,
                    metric_name=c_info.get("name", cid),
                    score=score,
                    breakdown=breakdown,
                    candidate_type="conflict_resolution",
                    conflict_ids=[c.type for c in [conflict]],
                ))

        # Stale reviews → stale_review candidates
        for c in contracts:
            cid = c.get("id", "")
            if c.get("status") != "in_review":
                continue
            if cid in active_initiative_ids:
                continue

            updated = c.get("updated_at") or c.get("created_at", "")
            days_blocked = 0.0
            if updated:
                try:
                    dt = datetime.fromisoformat(updated)
                    days_blocked = (now - dt).total_seconds() / 86400
                except (ValueError, TypeError):
                    pass

            if days_blocked < 7:
                continue

            score, breakdown = compute_priority_score(
                priority=queue_map.get(cid),
                days_blocked=days_blocked,
                stakeholder_available=True,
                is_in_progress=False,
            )
            candidates.append(ScoredCandidate(
                contract_id=cid,
                metric_name=c.get("name", cid),
                score=score,
                breakdown=breakdown,
                candidate_type="stale_review",
            ))

        return rank_candidates(candidates)

    # ── 3. PLAN ──────────────────────────────────────────────────────

    def _plan(self, candidates: list[ScoredCandidate], gathered: dict, state: dict) -> list[dict]:
        """Call LLM once to select 0-3 actions from scored candidates."""
        system_prompt = self.memory.read_file("prompts/planner_system.md") or ""

        # Build summary for LLM
        top = candidates[:10]
        candidates_summary = []
        for c in top:
            candidates_summary.append({
                "contract_id": c.contract_id,
                "metric_name": c.metric_name,
                "type": c.candidate_type,
                "score": c.score,
                "stakeholders": c.stakeholders or [],
            })

        active_initiatives = [
            {
                "id": init.get("id"),
                "contract_id": init.get("contract_id"),
                "status": init.get("status"),
                "waiting_for": init.get("waiting_for", []),
                "actions_today": init.get("actions_today", 0),
            }
            for init in state.get("initiatives", [])
            if init.get("status") in ("active", "waiting_response", "planned")
        ]

        conflicts_summary = []
        for conflict in gathered.get("conflicts", [])[:10]:
            conflicts_summary.append({
                "type": conflict.type,
                "severity": conflict.severity,
                "title": conflict.title,
                "contracts": conflict.contracts,
            })

        user_message = json.dumps({
            "candidates": candidates_summary,
            "active_initiatives": active_initiatives,
            "conflicts": conflicts_summary,
            "total_contracts": len(gathered.get("contracts", [])),
            "uncovered_metrics": len(gathered.get("uncovered", [])),
            "pending_reminders": len(gathered.get("reminders", [])),
        }, ensure_ascii=False, indent=2)

        try:
            response = self.llm.call_heavy(system_prompt, user_message, max_tokens=1000)
        except Exception as e:
            logger.error("Planner LLM call failed: %s", e, exc_info=True)
            return []

        # Parse JSON response
        try:
            # Handle markdown code blocks
            text = response.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                # Remove first and last lines (``` markers)
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines)
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            import re
            m = re.search(r'\{.*\}', response, re.DOTALL)
            if m:
                try:
                    result = json.loads(m.group())
                except json.JSONDecodeError:
                    logger.warning("Planner: failed to parse LLM response: %s", response[:200])
                    return []
            else:
                logger.warning("Planner: no JSON in LLM response: %s", response[:200])
                return []

        analysis = result.get("analysis", "")
        if analysis:
            logger.info("Planner analysis: %s", analysis)

        actions = result.get("actions", [])
        if not isinstance(actions, list):
            return []

        return actions[:3]

    # ── 4. CHECK ─────────────────────────────────────────────────────

    def _check_limits(self, action: dict, state: dict, daily: dict, now: datetime) -> bool:
        """Check rate limits before executing an action. Returns True if allowed."""
        # Daily message cap
        if daily.get("messages_sent", 0) >= PLANNER_MAX_MESSAGES_PER_DAY:
            return False

        # New threads per day
        if action.get("type") == "start_thread":
            if daily.get("threads_started", 0) >= PLANNER_MAX_NEW_THREADS_PER_DAY:
                return False

        # Max active initiatives
        if action.get("type") == "start_thread":
            active_count = sum(
                1 for init in state.get("initiatives", [])
                if init.get("status") in ("active", "waiting_response", "planned")
            )
            if active_count >= PLANNER_MAX_ACTIVE_INITIATIVES:
                return False

        # Cooldown check
        contract_id = action.get("contract_id", "")
        cooldown_key = f"{action.get('type', '')}:{contract_id}"
        cooldown_until = state.get("cooldowns", {}).get(cooldown_key)
        if cooldown_until:
            try:
                if datetime.fromisoformat(cooldown_until) > now:
                    return False
            except (ValueError, TypeError):
                pass

        # Per-initiative daily action limit
        for init in state.get("initiatives", []):
            if init.get("contract_id") == contract_id:
                if init.get("actions_today", 0) >= PLANNER_MAX_ACTIONS_PER_INITIATIVE_PER_DAY:
                    return False

        # Follow-up: check wait time
        if action.get("type") == "follow_up":
            for init in state.get("initiatives", []):
                if init.get("contract_id") == contract_id:
                    next_action = init.get("next_action_after")
                    if next_action:
                        try:
                            if datetime.fromisoformat(next_action) > now:
                                return False
                        except (ValueError, TypeError):
                            pass

        return True

    # ── Initiative management ────────────────────────────────────────

    def _get_or_create_initiative(
        self, action: dict, state: dict, candidates: list[ScoredCandidate], now: datetime
    ) -> dict:
        """Find existing initiative for contract_id or create a new one."""
        contract_id = action.get("contract_id", "")

        # Search existing
        for init in state.get("initiatives", []):
            if init.get("contract_id") == contract_id and init.get("status") not in ("completed", "abandoned"):
                return init

        # Map candidate type
        candidate_type = "new_contract"
        score = 0.0
        stakeholders = []
        for c in candidates:
            if c.contract_id == contract_id:
                candidate_type = c.candidate_type
                score = c.score
                stakeholders = c.stakeholders or []
                break

        today = now.strftime("%Y%m%d")
        existing_today = sum(
            1 for init in state.get("initiatives", [])
            if init.get("id", "").startswith(f"init_{today}")
        )

        initiative = {
            "id": f"init_{today}_{existing_today + 1:03d}",
            "type": candidate_type,
            "contract_id": contract_id,
            "priority_score": score,
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "thread_id": None,
            "stakeholders": stakeholders,
            "waiting_for": [],
            "actions_taken": [],
            "last_external_activity_at": None,
            "next_action_after": None,
            "actions_today": 0,
        }

        state.setdefault("initiatives", []).append(initiative)
        return initiative

    def _abandon_stale_initiatives(self, state: dict, now: datetime):
        """Mark initiatives as abandoned if no progress for STALE_INITIATIVE_DAYS."""
        for init in state.get("initiatives", []):
            if init.get("status") in ("completed", "abandoned"):
                continue

            updated = init.get("updated_at") or init.get("created_at", "")
            if not updated:
                continue

            try:
                dt = datetime.fromisoformat(updated)
                if (now - dt).days >= PLANNER_STALE_INITIATIVE_DAYS:
                    init["status"] = "abandoned"
                    init["updated_at"] = now.isoformat()
                    logger.info("Planner: abandoned stale initiative %s", init.get("id"))
            except (ValueError, TypeError):
                pass

        # Reset actions_today for all initiatives (new day)
        for init in state.get("initiatives", []):
            init["actions_today"] = 0
