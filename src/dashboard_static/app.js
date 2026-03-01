/* DAAI Dashboard — Vanilla JS client */

const API = "";  // relative to current origin + root_path
let autoRefreshTimer = null;

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    refreshAll();
    setupAutoRefresh();
});

function setupAutoRefresh() {
    const toggle = document.getElementById("auto-refresh-toggle");
    toggle.addEventListener("change", () => {
        if (toggle.checked) {
            startAutoRefresh();
        } else {
            stopAutoRefresh();
        }
    });
    startAutoRefresh();
}

function startAutoRefresh() {
    stopAutoRefresh();
    autoRefreshTimer = setInterval(refreshAll, 60000);
}

function stopAutoRefresh() {
    if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
    }
}

async function refreshAll() {
    document.getElementById("last-update").textContent =
        "Updated: " + new Date().toLocaleTimeString();

    await Promise.allSettled([
        fetchOverview(),
        fetchContracts(),
        fetchTree(),
        fetchConflicts(),
        fetchPlanner(),
        fetchScheduler(),
        fetchActivity(),
        fetchParticipants(),
    ]);
}

// ── Fetch helpers ────────────────────────────────────────────────────────────

async function apiFetch(path) {
    const resp = await fetch(API + path);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return resp.json();
}

// ── Overview ─────────────────────────────────────────────────────────────────

async function fetchOverview() {
    try {
        const data = await apiFetch("/api/overview");
        setText("card-total", ".card-value", data.total_contracts);
        setText("card-agreed", ".card-value", data.by_status.agreed || 0);
        setText("card-review", ".card-value", (data.by_status.in_review || 0) + (data.by_status.draft || 0));
        setText("card-conflicts", ".card-value", data.active_conflicts);
        setText("card-initiatives", ".card-value", data.active_initiatives);
    } catch (e) {
        console.error("Overview fetch failed:", e);
    }
}

// ── Contracts ────────────────────────────────────────────────────────────────

async function fetchContracts() {
    try {
        const data = await apiFetch("/api/contracts");
        const tbody = document.querySelector("#contracts-table tbody");
        tbody.innerHTML = "";

        if (!data.contracts.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No contracts</td></tr>';
            return;
        }

        // Detect stale in_review
        const staleItems = [];
        const now = Date.now();

        for (const c of data.contracts) {
            const tr = document.createElement("tr");
            tr.className = "clickable";
            tr.onclick = () => showContract(c.id);

            const statusClass = "badge badge-" + (c.status || "draft").replace(/\s+/g, "-");
            tr.innerHTML = `
                <td><code>${esc(c.id)}</code></td>
                <td>${esc(c.name || c.id)}</td>
                <td><span class="${statusClass}">${esc(c.status || "draft")}</span></td>
                <td>${c.agreed_date ? esc(c.agreed_date) : "-"}</td>
            `;
            tbody.appendChild(tr);

            // Check staleness
            if (c.status === "in_review" && c.agreed_date) {
                const age = (now - new Date(c.agreed_date).getTime()) / 86400000;
                if (age > 7) {
                    staleItems.push(`Contract <code>${esc(c.id)}</code> in review for ${Math.floor(age)} days`);
                }
            }
        }

        updateStaleSection(staleItems);
    } catch (e) {
        console.error("Contracts fetch failed:", e);
    }
}

async function showContract(contractId) {
    try {
        const data = await apiFetch("/api/contracts/" + encodeURIComponent(contractId));
        document.getElementById("modal-body").innerHTML =
            `<h2>${esc(contractId)}</h2><pre>${esc(data.markdown)}</pre>`;
        document.getElementById("modal-overlay").style.display = "flex";
    } catch (e) {
        console.error("Contract detail failed:", e);
    }
}

function closeModal(event) {
    if (!event || event.target === document.getElementById("modal-overlay")) {
        document.getElementById("modal-overlay").style.display = "none";
    }
}

// ── Metrics tree ─────────────────────────────────────────────────────────────

async function fetchTree() {
    try {
        const data = await apiFetch("/api/tree");
        const container = document.getElementById("metrics-tree");
        const stats = document.getElementById("tree-coverage");

        if (!data.tree) {
            container.innerHTML = '<div class="empty-state">No metrics tree</div>';
            stats.innerHTML = "";
            return;
        }

        // Also fetch coverage from overview
        try {
            const overview = await apiFetch("/api/overview");
            const cov = overview.tree_coverage || {};
            stats.innerHTML = `Markers: ${cov.total_markers || 0} | Agreed: ${cov.agreed || 0} | Uncovered: ${cov.uncovered || 0}`;
        } catch (_) {}

        container.innerHTML = renderTreeNode(data.tree, true);
    } catch (e) {
        console.error("Tree fetch failed:", e);
    }
}

function renderTreeNode(node, isRoot) {
    let cls = "no-contract";
    if (node.has_contract && node.is_agreed) cls = "agreed";
    else if (node.has_contract) cls = "uncovered";

    const marker = node.has_contract ? (node.is_agreed ? " [ok]" : " [!]") : "";
    const label = `<span class="tree-label ${cls} ${node.has_contract ? 'has-contract' : ''}">${esc(node.short_name || node.name)}${marker}</span>`;

    let childrenHtml = "";
    if (node.children && node.children.length) {
        childrenHtml = node.children.map(c => renderTreeNode(c, false)).join("");
    }

    return `<div class="tree-node ${isRoot ? 'tree-node-root' : ''}">${label}${childrenHtml}</div>`;
}

// ── Conflicts ────────────────────────────────────────────────────────────────

async function fetchConflicts() {
    try {
        const data = await apiFetch("/api/conflicts");
        const tbody = document.querySelector("#conflicts-table tbody");
        tbody.innerHTML = "";

        if (!data.conflicts.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No conflicts detected</td></tr>';
            return;
        }

        // Sort by severity: high > medium > low
        const order = { high: 0, medium: 1, low: 2 };
        data.conflicts.sort((a, b) => (order[a.severity] || 9) - (order[b.severity] || 9));

        for (const c of data.conflicts) {
            const tr = document.createElement("tr");
            const sevClass = "badge badge-" + c.severity;
            tr.innerHTML = `
                <td><span class="${sevClass}">${esc(c.severity)}</span></td>
                <td>${esc(c.type)}</td>
                <td>${esc(c.title)}</td>
                <td>${c.contracts.map(id => `<code>${esc(id)}</code>`).join(", ")}</td>
            `;
            tbody.appendChild(tr);
        }
    } catch (e) {
        console.error("Conflicts fetch failed:", e);
    }
}

// ── Planner ──────────────────────────────────────────────────────────────────

async function fetchPlanner() {
    try {
        const data = await apiFetch("/api/planner");
        const container = document.getElementById("planner-content");

        const initiatives = data.initiatives || [];
        if (!initiatives.length) {
            container.innerHTML = '<div class="empty-state">No initiatives</div>';
            return;
        }

        // Show active/waiting first, then completed/abandoned
        const active = initiatives.filter(i => ["active", "waiting_response", "planned"].includes(i.status));
        const rest = initiatives.filter(i => !["active", "waiting_response", "planned"].includes(i.status));

        container.innerHTML = [...active, ...rest].map(i => {
            const statusClass = "badge badge-" + (i.status || "").replace(/_/g, "-");
            const actions = (i.actions_taken || []).slice(-3).map(a =>
                `<div class="action-item">${esc(a.at || "")} - ${esc(a.action || "")}</div>`
            ).join("");

            const waiting = i.waiting_for && i.waiting_for.length
                ? `<div class="initiative-meta">Waiting: ${i.waiting_for.map(u => "@" + esc(u)).join(", ")}</div>`
                : "";

            return `
                <div class="initiative-card">
                    <div class="initiative-header">
                        <span><strong>${esc(i.type || "")}</strong> &mdash; <code>${esc(i.contract_id || "")}</code></span>
                        <span class="${statusClass}">${esc(i.status || "")}</span>
                    </div>
                    <div class="initiative-meta">Score: ${(i.priority_score || 0).toFixed(2)} | Created: ${formatTs(i.created_at)}</div>
                    ${waiting}
                    ${actions ? `<div class="initiative-actions">${actions}</div>` : ""}
                </div>
            `;
        }).join("");
    } catch (e) {
        console.error("Planner fetch failed:", e);
    }
}

// ── Scheduler ────────────────────────────────────────────────────────────────

async function fetchScheduler() {
    try {
        const data = await apiFetch("/api/scheduler");
        const container = document.getElementById("scheduler-content");

        let html = "";

        // Reminders
        const reminders = data.reminders || [];
        if (reminders.length) {
            html += "<h3 style='font-size:13px;margin-bottom:8px;color:var(--text-muted)'>Pending Reminders</h3>";
            html += reminders.map(r => `
                <div class="reminder-card">
                    <strong>@${esc(r.target_user || "?")}</strong> &mdash; <code>${esc(r.contract_id || "")}</code>
                    <br>Step: ${r.escalation_step || 0} | Next: ${formatTs(r.next_reminder)}
                    ${r.question_summary ? `<br><em>${esc(r.question_summary)}</em>` : ""}
                </div>
            `).join("");
        }

        // Queue
        const queue = data.queue || [];
        if (queue.length) {
            html += "<h3 style='font-size:13px;margin:12px 0 8px;color:var(--text-muted)'>Queue</h3>";
            html += queue.map(q =>
                `<div class="queue-item">#${q.priority || "-"} <code>${esc(q.id || "")}</code> — ${esc(q.reason || "")} <span class="badge badge-${q.status || 'queued'}">${esc(q.status || "queued")}</span></div>`
            ).join("");
        }

        if (!html) html = '<div class="empty-state">No pending reminders or queue items</div>';
        container.innerHTML = html;
    } catch (e) {
        console.error("Scheduler fetch failed:", e);
    }
}

// ── Activity ─────────────────────────────────────────────────────────────────

async function fetchActivity() {
    try {
        const data = await apiFetch("/api/activity");
        const container = document.getElementById("activity-feed");

        if (!data.activity.length) {
            container.innerHTML = '<div class="empty-state">No activity</div>';
            return;
        }

        container.innerHTML = data.activity.map(e => {
            const detail = Object.keys(e)
                .filter(k => !["ts", "action", "event", "_source"].includes(k))
                .map(k => `${k}=${typeof e[k] === "object" ? JSON.stringify(e[k]) : e[k]}`)
                .join(" ");

            return `
                <div class="activity-item">
                    <span class="activity-ts">${formatTs(e.ts)}</span>
                    <span class="activity-source badge badge-${e._source === "planner" ? "blue" : "draft"}">${esc(e._source || "")}</span>
                    <span class="activity-action">${esc(e.action || e.event || "")}</span>
                    <span class="activity-detail">${esc(detail)}</span>
                </div>
            `;
        }).join("");
    } catch (e) {
        console.error("Activity fetch failed:", e);
    }
}

// ── Participants ─────────────────────────────────────────────────────────────

async function fetchParticipants() {
    try {
        const data = await apiFetch("/api/participants");
        const container = document.getElementById("participants-content");

        if (!data.participants.length) {
            container.innerHTML = '<div class="empty-state">No participants</div>';
            return;
        }

        container.innerHTML = data.participants.map(p => {
            const cls = p.active !== false ? "active" : "inactive";
            return `<span class="participant-chip ${cls}">@${esc(p.username || "?")}${p.onboarded ? " [ok]" : ""}</span>`;
        }).join("");
    } catch (e) {
        console.error("Participants fetch failed:", e);
    }
}

// ── Stale detection ──────────────────────────────────────────────────────────

function updateStaleSection(items) {
    const section = document.getElementById("stale-section");
    const container = document.getElementById("stale-items");
    if (!items.length) {
        section.style.display = "none";
        return;
    }
    section.style.display = "block";
    container.innerHTML = items.map(i => `<div class="stale-item">${i}</div>`).join("");
}

// ── Utilities ────────────────────────────────────────────────────────────────

function setText(parentId, selector, value) {
    const el = document.getElementById(parentId);
    if (el) {
        const target = el.querySelector(selector);
        if (target) target.textContent = value;
    }
}

function esc(s) {
    if (s == null) return "";
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function formatTs(ts) {
    if (!ts) return "-";
    try {
        const d = new Date(ts);
        return d.toLocaleString("ru-RU", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch (_) {
        return ts;
    }
}
