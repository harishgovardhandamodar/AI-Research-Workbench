"use strict";

// Fox · Experiment Tracking — webview UI.
// All HTTP goes through the extension host (postMessage), so no CORS issues.

const vscode = acquireVsCodeApi();

let seq = 0;
const pending = {};

function api(path, method, body) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending[id] = { resolve, reject };
    vscode.postMessage({ kind: "api", id, path, method, body });
  });
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return String(Math.round(Number(n) * 1e4) / 1e4);
}

let toastTimer = null;
function toast(msg, isErr) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = (isErr ? "err " : "") + "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "hidden"), isErr ? 6000 : 3000);
}

let state = { project: "", experiments: [], campaigns: [], evals: [], learnings: [], ranking: {} };

window.addEventListener("message", (e) => {
  const m = e.data;
  if (!m) return;
  if (m.kind === "apiResult") {
    const p = pending[m.id];
    if (p) { delete pending[m.id]; p.resolve(m.data); }
  } else if (m.kind === "error") {
    toast(m.message, true);
  } else if (m.kind === "refresh") {
    loadAll();
  } else if (m.kind === "toast") {
    toast(m.message);
  }
});

function post(kind, body) { vscode.postMessage({ kind, ...(body || {}) }); }
function doc(kind, title) { post("doc", { kind, title }); }

// --------------------------------------------------------------------- loading

async function loadAll() {
  const name = state.project;
  if (!name) return;
  const proj = (p) => `/api/projects/${encodeURIComponent(name)}${p}`;
  try {
    const [exps, camps, evals, learnings] = await Promise.all([
      api(proj("/experiments")),
      api(proj("/campaigns")),
      api(proj("/evals")),
      api(proj("/learnings"))
    ]);
    state.experiments = (exps && exps.experiments) || [];
    state.campaigns = (camps && camps.campaigns) || [];
    state.evals = (evals && evals.evals) || [];
    state.learnings = (learnings && learnings.learnings) || [];
  } catch (e) {
    toast("Failed to load: " + e.message, true);
    return;
  }
  renderExperiments();
  renderCampaigns();
  renderEvals();
  renderLearnings();
}

function statusBadge(s) {
  const map = { active: "det", planned: "det", running: "warn", done: "ok", completed: "ok", failed: "warn", cancelled: "warn" };
  return `<span class="badge ${map[s] || "det"}">${esc(s)}</span>`;
}

// ------------------------------------------------------------------ experiments

async function loadRanking(eid) {
  const name = state.project;
  if (!name || state.ranking[eid]) return;
  try {
    const r = await api(`/api/projects/${encodeURIComponent(name)}/experiments/${eid}/ranking`);
    state.ranking[eid] = (r && r.ranking) || null;
    renderExperiments();
  } catch (_) {}
}

function expBest(e) {
  const r = state.ranking[e.id];
  return r && r.best != null ? r.best : null;
}

function renderExperiments() {
  const el = document.getElementById("exp-list");
  if (!state.experiments.length) { el.innerHTML = '<div class="empty">No experiments yet — create one below.</div>'; return; }
  el.innerHTML = state.experiments.map((e) => {
    const best = expBest(e);
    const target = e.goal_target;
    const reached = best != null && target != null && (e.higher_better !== false ? best >= target : best <= target);
    return `<div class="card exp-card" data-id="${e.id}">
      <div class="row">
        <b class="name" data-id="${e.id}" title="Open detail">${esc(e.name)}</b>
        ${statusBadge(e.status)}
        <span class="muted">${e.runs} run(s)</span>
        <span class="spacer"></span>
        <button class="exp-improve" data-name="${esc(e.name)}" title="Run the improve loop in the workbench chat">🔁 Improve</button>
        <button class="exp-focus" data-id="${e.id}" title="Steer the agent toward this objective">★</button>
      </div>
      <div class="goal">${e.goal_metric ? `goal ${esc(e.goal_metric)} ${e.higher_better !== false ? "↑" : "↓"}` : ""}
        ${best != null ? ` · best <b>${fmt(best)}</b>` : ""}
        ${target != null ? ` / target ${fmt(target)}` : ""}
        ${reached ? ' <span class="ok">✓ reached</span>' : ""}</div>
    </div>`;
  }).join("");
  el.querySelectorAll(".name").forEach((b) => b.addEventListener("click", () => openExpDetail(Number(b.dataset.id))));
  el.querySelectorAll(".exp-focus").forEach((b) => b.addEventListener("click", async () => {
    try {
      const name = state.project;
      await api(`/api/projects/${encodeURIComponent(name)}/experiments/focus`, "POST", { id: Number(b.dataset.id) });
      toast("Experiment focused.");
    } catch (e) { toast(e.message, true); }
  }));
  el.querySelectorAll(".exp-improve").forEach((b) => b.addEventListener("click", () =>
    toast("Improve loop is chat-driven — run “Improve <name>” in the workbench chat.")));
  // eager ranking loads
  state.experiments.forEach((e) => loadRanking(e.id));
}

async function openExpDetail(eid) {
  const name = state.project;
  const el = document.getElementById("exp-detail");
  el.classList.remove("hidden");
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const r = await api(`/api/projects/${encodeURIComponent(name)}/experiments/${eid}`);
    const exp = (r && r.experiment) || {};
    const runs = exp.runs || [];
    const rank = state.ranking[eid];
    let h = `<div class="row"><b>${esc(exp.name)}</b>${statusBadge(exp.status)}<span class="spacer"></span>
      <button class="detail-close">✕</button></div>`;
    if (exp.hypothesis) h += `<p class="muted">${esc(exp.hypothesis)}</p>`;
    if (exp.plan) h += `<p class="muted">Plan: ${esc(exp.plan)}</p>`;
    if (rank && rank.rows && rank.rows.length) {
      h += `<h4>Leaderboard (${esc(rank.metric || "metric")})</h4><table><tr><th>#</th><th>run</th><th>${esc(rank.metric || "metric")}</th><th>Δ best</th></tr>`;
      rank.rows.forEach((row) => {
        h += `<tr${row.rank === 1 ? ' class="top"' : ""}><td>${row.rank}</td><td>${esc(row.label || "#" + row.run_id)}</td><td>${fmt(row.metric)}</td><td class="muted">${row.rank === 1 ? "—" : fmt(row.delta_best)}</td></tr>`;
      });
      h += `</table>`;
    }
    if (runs.length) {
      h += `<h4>Runs (${runs.length})</h4>`;
      h += runs.slice(0, 40).map((ru) => {
        const m = ru.metrics || {};
        const mstr = Object.keys(m).length ? Object.keys(m).map((k) => `${esc(k)}=${fmt(m[k])}`).join(", ") : "—";
        return `<div class="run-line"><span class="run-id">#${ru.id}</span><span class="muted">${esc((ru.label || "").slice(0, 24))} · ${mstr.slice(0, 120)}</span></div>`;
      }).join("");
    } else {
      h += '<div class="empty">No runs yet.</div>';
    }
    el.innerHTML = h;
    el.querySelector(".detail-close").addEventListener("click", () => el.classList.add("hidden"));
  } catch (e) {
    el.innerHTML = '<div class="empty">Failed to load: ' + esc(e.message) + "</div>";
  }
}

// ------------------------------------------------------------------ campaigns

function renderCampaigns() {
  const el = document.getElementById("campaign-list");
  if (!state.campaigns.length) { el.innerHTML = '<div class="empty">No campaigns yet.</div>'; return; }
  el.innerHTML = state.campaigns.map((c) => `
    <div class="card exp-card">
      <div class="row"><b>${esc(c.name)}</b>${statusBadge(c.status)}<span class="muted">${c.steps} step(s)</span>
        <span class="spacer"></span>
        ${c.status === "running"
          ? `<button class="camp-stop" data-id="${c.id}">⏹ Stop</button>`
          : c.status !== "done" ? `<button class="camp-run" data-id="${c.id}">▶ ${c.steps > 0 ? "Resume" : "Run"}</button>` : ""}
      </div>
      ${c.research_question ? `<div class="goal muted">${esc(c.research_question)}</div>` : ""}
    </div>`).join("");
  el.querySelectorAll(".camp-run").forEach((b) => b.addEventListener("click", async () => {
    const name = state.project;
    try { await api(`/api/projects/${encodeURIComponent(name)}/campaigns/${b.dataset.id}/run`, "POST", {}); toast("Campaign started."); loadAll(); }
    catch (e) { toast(e.message, true); }
  }));
  el.querySelectorAll(".camp-stop").forEach((b) => b.addEventListener("click", async () => {
    const name = state.project;
    try { await api(`/api/projects/${encodeURIComponent(name)}/campaigns/${b.dataset.id}/stop`, "POST"); toast("Stop requested."); loadAll(); }
    catch (e) { toast(e.message, true); }
  }));
}

// ------------------------------------------------------------------- benchmarks

function renderEvals() {
  const el = document.getElementById("eval-list");
  if (!state.evals.length) { el.innerHTML = '<div class="empty">No benchmarks yet.</div>'; return; }
  el.innerHTML = state.evals.map((ev) => `
    <div class="card exp-card">
      <div class="row"><b>${esc(ev.name)}</b>${statusBadge(ev.status)}<span class="muted">${ev.models.length} model(s)</span>
        <span class="spacer"></span>
        ${ev.status === "running"
          ? `<button class="eval-stop" data-id="${ev.id}">⏹ Stop</button>`
          : `<button class="eval-run" data-id="${ev.id}">▶ ${ev.status === "done" ? "Rerun" : "Run"}</button>`}
      </div>
      ${ev.report ? `<div class="goal muted">${esc(ev.report.replace(/\s+/g, " ").slice(0, 200))}</div>` : ""}
    </div>`).join("");
  el.querySelectorAll(".eval-run").forEach((b) => b.addEventListener("click", async () => {
    const name = state.project;
    try { await api(`/api/projects/${encodeURIComponent(name)}/evals/${b.dataset.id}/run`, "POST"); toast("Benchmark started."); loadAll(); }
    catch (e) { toast(e.message, true); }
  }));
  el.querySelectorAll(".eval-stop").forEach((b) => b.addEventListener("click", async () => {
    const name = state.project;
    try { await api(`/api/projects/${encodeURIComponent(name)}/evals/${b.dataset.id}/stop`, "POST"); toast("Stop requested."); loadAll(); }
    catch (e) { toast(e.message, true); }
  }));
}

// ------------------------------------------------------------------- learnings

function renderLearnings() {
  const el = document.getElementById("learning-list");
  if (!state.learnings.length) { el.innerHTML = '<div class="empty">No learnings recorded yet.</div>'; return; }
  el.innerHTML = state.learnings.map((l) => {
    const badge = l.improved === 1 ? '<span class="badge ok">✓ improved</span>'
      : (l.improved === 0 ? '<span class="badge warn">✗ no gain</span>' : "");
    return `<div class="card learning-row">${badge}<span>${esc(l.summary)}</span></div>`;
  }).join("");
}

// --------------------------------------------------------------------- wiring

function showTab(tab) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + tab));
}

async function init() {
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  const sel = document.getElementById("project-select");
  try {
    const r = await api("/api/projects");
    const projects = (r && r.projects) || [];
    sel.innerHTML = projects.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
    state.project = projects.length ? projects[0].name : "";
    sel.value = state.project;
    if (projects.length) loadAll();
    else toast("No Fox projects found — is the workbench running?", true);
  } catch (e) { toast("Cannot reach workbench: " + e.message, true); }
  sel.addEventListener("change", () => {
    state.project = sel.value;
    post("setProject", { name: sel.value });
    state.ranking = {};
    loadAll();
  });
  document.getElementById("btn-refresh").addEventListener("click", loadAll);

  document.getElementById("btn-new-exp").addEventListener("click", () => toggle("exp-form"));
  document.getElementById("exp-cancel").addEventListener("click", () => toggle("exp-form"));
  document.getElementById("exp-create").addEventListener("click", async () => {
    const body = {
      name: val("exp-name"), hypothesis: val("exp-hyp"), goal_metric: val("exp-metric"),
      goal_target: parseFloat(val("exp-target")) || null,
      higher_better: document.getElementById("exp-hb").checked
    };
    if (!body.name) { toast("Name required", true); return; }
    const name = state.project;
    try { await api(`/api/projects/${encodeURIComponent(name)}/experiments`, "POST", body); toast("Experiment created."); toggle("exp-form"); loadAll(); }
    catch (e) { toast(e.message, true); }
  });

  document.getElementById("btn-new-campaign").addEventListener("click", () => toggle("campaign-form"));
  document.getElementById("campaign-cancel").addEventListener("click", () => toggle("campaign-form"));
  document.getElementById("campaign-create").addEventListener("click", async () => {
    const name = state.project;
    try {
      const body = { name: val("campaign-name") || "Campaign", research_question: val("campaign-question"), goal_metric: val("campaign-metric"), higher_better: true };
      const r = await api(`/api/projects/${encodeURIComponent(name)}/campaigns`, "POST", body);
      await api(`/api/projects/${encodeURIComponent(name)}/campaigns/${r.campaign.id}/run`, "POST", {});
      toast("Campaign started in the background."); toggle("campaign-form"); loadAll();
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("btn-new-eval").addEventListener("click", () => toggle("eval-form"));
  document.getElementById("eval-cancel").addEventListener("click", () => toggle("eval-form"));
  document.getElementById("eval-create").addEventListener("click", async () => {
    const name = state.project;
    const models = val("eval-models").split(",").map((s) => s.trim()).filter(Boolean);
    if (!models.length) { toast("Enter at least one model", true); return; }
    try {
      const body = { name: val("eval-name") || "Eval", prompt: val("eval-prompt"), models, goal_metric: val("eval-metric"), higher_better: true };
      const r = await api(`/api/projects/${encodeURIComponent(name)}/evals`, "POST", body);
      await api(`/api/projects/${encodeURIComponent(name)}/evals/${r.eval.id}/run`, "POST");
      toast("Benchmark started in the background."); toggle("eval-form"); loadAll();
    } catch (e) { toast(e.message, true); }
  });

  [["btn-doc-report", "report", "Experimentation report"], ["btn-doc-next", "next", "Next research"],
   ["btn-doc-summary", "summary", "Summary of findings"],
   ["btn-doc-report2", "report", "Experimentation report"], ["btn-doc-next2", "next", "Next research"],
   ["btn-doc-summary2", "summary", "Summary of findings"]].forEach(([id, kind, title]) => {
    document.getElementById(id).addEventListener("click", () => doc(kind, title));
  });

  document.getElementById("cfg-base").textContent = "see fox.baseUrl in settings";
}

function val(id) { return document.getElementById(id).value.trim(); }
function toggle(id) { document.getElementById(id).classList.toggle("hidden"); }

init();
