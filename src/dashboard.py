"""Dashboard — FastAPI web UI for monitoring the DAAI agent.

Runs as a daemon thread alongside the main listener, scheduler, and planner.
Serves a single-page app with JSON API endpoints.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import DASHBOARD_HOST, DASHBOARD_PORT
from src.memory import Memory

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "dashboard_static"

_memory: Memory | None = None


def _get_memory() -> Memory:
    global _memory
    if _memory is None:
        _memory = Memory()
    return _memory


def create_app(memory: Memory | None = None) -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="DAAI Dashboard", root_path="/dashboard")

    if memory is not None:
        global _memory
        _memory = memory

    # ── Static files ─────────────────────────────────────────────────────
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>Dashboard static files not found</h1>", status_code=500)
        return FileResponse(str(index_file), media_type="text/html")

    # ── API: Overview ────────────────────────────────────────────────────
    @app.get("/api/overview")
    def api_overview():
        mem = _get_memory()
        contracts = mem.list_contracts()
        status_counts = {}
        for c in contracts:
            s = c.get("status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1

        # Conflicts
        conflicts = _detect_conflicts_safe(mem)

        # Planner initiatives
        planner = mem.get_planner_state()
        active_initiatives = [
            i for i in planner.get("initiatives", [])
            if i.get("status") in ("active", "waiting_response", "planned")
        ]

        # Tree coverage
        tree_stats = _tree_coverage(mem)

        return {
            "total_contracts": len(contracts),
            "by_status": status_counts,
            "active_conflicts": len(conflicts),
            "active_initiatives": len(active_initiatives),
            "tree_coverage": tree_stats,
        }

    # ── API: Contracts ───────────────────────────────────────────────────
    @app.get("/api/contracts")
    def api_contracts():
        mem = _get_memory()
        return {"contracts": mem.list_contracts()}

    @app.get("/api/contracts/{contract_id}")
    def api_contract_detail(contract_id: str):
        mem = _get_memory()
        content = mem.get_contract(contract_id)
        if content is None:
            content = mem.get_draft(contract_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Contract not found")
        return {"id": contract_id, "markdown": content}

    # ── API: Metrics tree ────────────────────────────────────────────────
    @app.get("/api/tree")
    def api_tree():
        mem = _get_memory()
        tree_md = mem.read_file("context/metrics_tree.md")
        if not tree_md:
            return {"tree": None}
        from src.metrics_tree import parse_tree
        root = parse_tree(tree_md)
        if root is None:
            return {"tree": None}
        return {"tree": _serialize_tree_node(root)}

    # ── API: Conflicts ───────────────────────────────────────────────────
    @app.get("/api/conflicts")
    def api_conflicts():
        mem = _get_memory()
        conflicts = _detect_conflicts_safe(mem)
        return {"conflicts": conflicts}

    # ── API: Planner ─────────────────────────────────────────────────────
    @app.get("/api/planner")
    def api_planner():
        mem = _get_memory()
        return mem.get_planner_state()

    # ── API: Scheduler ───────────────────────────────────────────────────
    @app.get("/api/scheduler")
    def api_scheduler():
        mem = _get_memory()
        reminders = mem.get_reminders()
        queue = mem.get_queue()
        return {
            "reminders": reminders,
            "queue": queue,
        }

    # ── API: Activity ────────────────────────────────────────────────────
    @app.get("/api/activity")
    def api_activity():
        mem = _get_memory()
        audit = mem.read_jsonl("memory/audit.jsonl")
        planner_log = mem.read_jsonl("tasks/planner_log.jsonl")

        # Merge, sort by ts desc, take last 50
        all_entries = []
        for e in audit:
            e["_source"] = "audit"
            all_entries.append(e)
        for e in planner_log:
            e["_source"] = "planner"
            all_entries.append(e)

        all_entries.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return {"activity": all_entries[:50]}

    # ── API: Participants ────────────────────────────────────────────────
    @app.get("/api/participants")
    def api_participants():
        mem = _get_memory()
        idx = mem.read_json("participants/index.json")
        if idx and isinstance(idx, dict) and "participants" in idx:
            return {"participants": idx["participants"]}
        return {"participants": []}

    return app


# ── Helpers ──────────────────────────────────────────────────────────────────


def _detect_conflicts_safe(mem: Memory) -> list[dict]:
    """Run conflict detection, return list of dicts. Never raises."""
    try:
        from src.analyzer import MetricsAnalyzer
        analyzer = MetricsAnalyzer(mem)
        conflicts = analyzer.detect_conflicts()
        return [
            {
                "type": c.type,
                "severity": c.severity,
                "title": c.title,
                "details": c.details,
                "contracts": c.contracts,
            }
            for c in conflicts
        ]
    except Exception as e:
        logger.warning("Conflict detection failed: %s", e)
        return []


def _tree_coverage(mem: Memory) -> dict:
    """Compute tree coverage stats."""
    try:
        from src.metrics_tree import parse_tree, get_uncovered_nodes
        tree_md = mem.read_file("context/metrics_tree.md")
        if not tree_md:
            return {"total_markers": 0, "agreed": 0, "uncovered": 0}
        root = parse_tree(tree_md)
        if root is None:
            return {"total_markers": 0, "agreed": 0, "uncovered": 0}

        markers = _count_markers(root)
        uncovered = len(get_uncovered_nodes(root))
        return {
            "total_markers": markers,
            "agreed": markers - uncovered,
            "uncovered": uncovered,
        }
    except Exception as e:
        logger.warning("Tree coverage failed: %s", e)
        return {"total_markers": 0, "agreed": 0, "uncovered": 0}


def _count_markers(node) -> int:
    """Count nodes with has_contract_marker=True."""
    count = 1 if node.has_contract_marker else 0
    for child in node.children:
        count += _count_markers(child)
    return count


def _serialize_tree_node(node) -> dict:
    """Convert TreeNode to JSON-serializable dict."""
    return {
        "name": node.name,
        "short_name": node.short_name,
        "has_contract": node.has_contract_marker,
        "is_agreed": node.is_agreed,
        "depth": node.depth,
        "children": [_serialize_tree_node(c) for c in node.children],
    }


# ── Startup ──────────────────────────────────────────────────────────────────


def start_dashboard(memory: Memory) -> threading.Thread:
    """Start the dashboard in a daemon thread. Returns the thread."""
    app = create_app(memory)

    def _run():
        uvicorn.run(
            app,
            host=DASHBOARD_HOST,
            port=DASHBOARD_PORT,
            log_level="warning",
        )

    thread = threading.Thread(target=_run, name="dashboard", daemon=True)
    thread.start()
    logger.info("Dashboard started on %s:%d", DASHBOARD_HOST, DASHBOARD_PORT)
    return thread
