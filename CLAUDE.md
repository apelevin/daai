# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run

```bash
# Run tests (all)
python -m pytest

# Run a single test file
python -m pytest tests/test_router_new_contract.py -v

# Run a single test class or method
python -m pytest tests/test_approval_workflow.py::TestApprovalWorkflow::test_approve -v

# Run the agent locally (requires .env with all vars)
python -m src.main

# Deploy to server
git push origin main
ssh do-daai "cd /opt/apps/daai && bash deploy.sh"
```

No linter or formatter is configured. No CI/CD pipeline — deploy is manual via `deploy.sh` (git pull + docker compose rebuild).

## Architecture

**DAAI** — AI agent ("Финист Ясный") that lives in Mattermost and facilitates Data Contract agreements between teams. It helps define metrics, reach consensus on formulas/data sources, and track approval workflows. All communication is in Russian.

### Request Flow

```
Mattermost WebSocket → Listener (dedup, context) → Router (fast-path regex or LLM classification)
→ Agent (load files, build prompt) → LLM with tools (agentic loop) → Reply in thread
```

### Core Modules (`src/`)

- **`main.py`** — Entry point. Spawns 4 threads: listener (main), scheduler, planner, dashboard.
- **`listener.py`** — WebSocket event handler. Post dedup, thread tracking, response dedup (MD5 hash within time window), user onboarding on channel join.
- **`router.py`** — Two-phase routing: fast-path regex patterns for common commands (Russian), then LLM fallback. Route types: `contract_discussion`, `new_contract_init`, `expert_opinion`, `data_query`, `general_question`, etc. Expert opinion triggers on `@bot_mention` + opinion keywords.
- **`agent.py`** — Central processor. Builds system prompt from templates, loads context files per route, runs LLM tool-calling loop, handles participant profiling. `_FULL_PROMPT_TYPES` get the full system prompt; others get the short version.
- **`llm_client.py`** — OpenRouter API wrapper. Three model tiers: `cheap` (routing/extraction), `heavy` (analysis/generation), `expert` (advisory via separate model). Agentic `call_with_tools()` loop with XML fallback for non-JSON-tool models. Retry with exponential backoff.
- **`tools.py`** — `ToolExecutor` with 24+ tool implementations. Contract CRUD, approval workflows, validation, MCP data queries, datamart spec generation. Tools are defined in `tool_definitions.py` and selected per route type.
- **`memory.py`** — File-based persistence. Contracts (versioned with JSONL history), drafts, discussions, reminders, queues, participants. Atomic writes with retry.
- **`planner.py`** — `ContinuousPlanner` runs daily. Scores candidates (new contracts, conflicts, datamart needs, stale reviews), selects actions via LLM, dispatches to `ActionDispatcher` in `planner_actions.py`. Tracks initiatives with status lifecycle.
- **`scheduler.py`** — Background jobs: reminder escalation (4 levels), weekly digest (Fri), coverage scan (Tue), thread cleanup (daily 3AM), open questions digest (daily configurable).
- **`mcp_client.py`** — Synchronous MCP-over-SSE client for PostgreSQL DWH schema `ai_bi`. Used for data availability checks and datamart spec generation.
- **`dashboard.py`** — FastAPI web UI on port 8050. Contract status, conflicts, open questions.

### Key Domain Modules

- **`analyzer.py`** — Conflict detection via Jaccard similarity between metric definitions.
- **`governance.py`** — Approval tiers (tier_1/2/3) with different thresholds.
- **`lifecycle.py`** — Contract status state machine: `draft → in_review → agreed → approved → active → deprecated → archived`.
- **`validator.py`** — Contract completeness checks.
- **`suggestion_engine.py`** — Data-driven suggestions with cooldowns.

### Data Layout (mounted as Docker volumes)

```
contracts/index.json        # Contract registry (id, name, status)
contracts/*.md              # Agreed contracts (markdown)
contracts/versions/<id>/    # Version history (JSONL + timestamped .md snapshots)
drafts/*.md                 # Draft contracts
drafts/*_discussion.json    # Discussion state (positions, blockers, open_questions)
tasks/roles.json            # Runtime role assignments
tasks/active_threads.json   # Thread-to-contract mapping
prompts/*.md                # System prompts and templates (mounted read-only)
context/*.md, *.json        # Company structure, metrics tree, governance (read-only)
participants/*.md           # User profiles
```

## Testing

Tests use **pytest** + **unittest**. No conftest.py or pytest.ini. Most tests use `FakeLLM`, `FakeMemory`, and `MagicMock` for Mattermost.

**Important**: All `FakeLLM` classes must have `expert_model = "fake/expert"` attribute (agent code accesses `self.llm.expert_model`). If adding a new FakeLLM, include this.

`src/test_connections.py` requires live env vars (MATTERMOST_URL, OPENROUTER_API_KEY) — always fails in local test runs, this is expected.

## Deployment

Server: `ssh do-daai`, project at `/opt/apps/daai/`. Docker Compose with volume mounts for data persistence. Runtime data lives on the server filesystem, not in git.

Config via `.env` file (see `.env.example`). Key vars: `OPENROUTER_API_KEY`, `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`, `MATTERMOST_CHANNEL_ID`, `DATA_DIR=/app`.

## Conventions

- All user-facing text and system prompts are in **Russian**.
- Contract IDs are ASCII snake_case slugs (e.g., `contract_churn`). Router has `_slugify()` for Cyrillic-to-ASCII conversion.
- Router fast-paths use Russian regex patterns — when adding new commands, add Russian keywords.
- Tool definitions in `tool_definitions.py` must stay in sync with handler methods in `tools.py` (`_tool_{name}`).
- `get_tools_for_route()` controls which tools are available per route type — write tools are restricted in read-only routes like `expert_opinion`.
