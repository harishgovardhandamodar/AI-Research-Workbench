/* Fox - Experiment workbench frontend */

const $ = (id) => document.getElementById(id);
const FOX_BASE = window.FOX_BASE || "";
const B = (path) => (FOX_BASE ? FOX_BASE + path : path);
const state = {
  projects: [],
  project: "",
  config: null,
  models: [],
  ws: null,
  busy: false,
  pendingApproval: null,
  streaming: false,
  nbTag: "all",
  workflow: null,
};

/* ============================== helpers ================================= */

async function api(path, opts = {}) {
  const res = await fetch(B(path), {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && data.error) throw new Error(data.error);
  return data;
}

function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function truncate(s, n = 2000) {
  return s.length > n ? s.slice(0, n) + "\n…[truncated]" : s;
}

/* ============================ markdown ================================== */

function renderMarkdown(src) {
  if (!src) return "";
  let text = esc(String(src));
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, body) => {
    const idx = codeBlocks.length;
    codeBlocks.push(
      `<pre><code${lang ? ` class="lang-${esc(lang)}"` : ""}>${body.trim()}</code></pre>`
    );
    return `\u0000CB${idx}\u0000`;
  });
  const inlinePlaceholders = {};
  text = text.replace(/`([^`\n]+)`/g, (m, c) => {
    const key = "\u0000IC" + (Object.keys(inlinePlaceholders).length) + "\u0000";
    inlinePlaceholders[key] = `<code>${c}</code>`;
    return key;
  });

  text = text.replace(/^######\s+(.+)$/gm, "<h6>$1</h6>");
  text = text.replace(/^#####\s+(.+)$/gm, "<h5>$1</h5>");
  text = text.replace(/^####\s+(.+)$/gm, "<h4>$1</h4>");
  text = text.replace(/^###\s+(.+)$/gm, "<h3>$1</h3>");
  text = text.replace(/^##\s+(.+)$/gm, "<h2>$1</h2>");
  text = text.replace(/^#\s+(.+)$/gm, "<h1>$1</h1>");
  text = text.replace(/^&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>");

  // tables
  const tableRe = /^(\|.*\|)\s*\n\|[\s:|-]+\|\s*\n((?:\|.*\|\s*\n?)*)/gm;
  text = text.replace(tableRe, (m, head, rows) => {
    const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    let h = "<table><thead><tr>" + cells(head).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
    for (const r of rows.trim().split("\n")) {
      h += "<tr>" + cells(r).map((c) => `<td>${c}</td>`).join("") + "</tr>";
    }
    return h + "</tbody></table>";
  });

  // lists (simple, non-nested)
  text = text.replace(/((?:^[ \t]*[-*] .*\n?)+)/gm, (m) => {
    const items = m.trim().split("\n").map((l) => `<li>${l.replace(/^[ \t]*[-*] /, "")}</li>`).join("");
    return `<ul>${items}</ul>`;
  });
  text = text.replace(/((?:^[ \t]*\d+\. .*\n?)+)/gm, (m) => {
    const items = m.trim().split("\n").map((l) => `<li>${l.replace(/^[ \t]*\d+\. /, "")}</li>`).join("");
    return `<ol>${items}</ol>`;
  });

  text = text.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // images (workflow figures): ![name](/artifacts/<id>) -> <img> (base-aware)
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => {
    const src = /^\/artifacts\//.test(url) ? B(url) : url;
    const artId = /^\/artifacts\//.test(url) ? url.split("/").pop() : "";
    return `<img src="${esc(src)}" alt="${esc(alt)}" class="chat-fig" data-art-id="${esc(artId)}">`;
  });
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/\u0000IC\d+\u0000/g, (m) => inlinePlaceholders[m] ?? m);
  text = text.replace(/\u0000CB\d+\u0000/g, (m) => codeBlocks[Number(m.slice(3, -1))]);
  text = text.replace(/\n{3,}/g, "\n\n");
  return text;
}

/* ============================ ws events ================================= */

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}${B(`/ws/projects/${encodeURIComponent(state.project)}`)}`;
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onopen = () => setConn("ok");
  ws.onclose = () => { setConn("off"); if (state.project) setTimeout(connect, 1500); };
  ws.onerror = () => setConn("off");
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg.type, msg.payload || {});
  };
}

function setConn(kind) {
  const el = $("conn-status");
  el.classList.remove("ok", "busy");
  if (kind === "ok") el.classList.add("ok");
  else if (kind === "busy") el.classList.add("busy");
  el.title = kind === "ok" ? "Connected" : kind === "busy" ? "Working…" : "Disconnected";
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
}

function handleEvent(type, p) {
  switch (type) {
    case "user_message": renderUserMessage(p.content, p.tags); break;
    case "stream_delta": streamDelta(p.text); break;
    case "assistant_message": finalizeAssistant(p.content, p.tags); break;
    case "tool_start": toolStart(p); break;
    case "tool_result": toolResult(p); break;
    case "artifact": addArtifact(p.artifact); renderArtifacts(); renderArtifactInline(p.artifact); break;
    case "approval_request": showApproval(p); break;
    case "review_start": setReviewStatus("Reviewing latest turn…"); break;
    case "review": renderReview(p.findings || []); break;
    case "status": setBusyStatus(p.message); break;
    case "workflow": renderWorkflow(p); break;
    case "done": onTurnDone(); loadExperiments(); break;
    case "error": onError(p.message); break;
  }
}

/* ============================ chat rendering ============================= */

let curAssistantEl = null;
let pendingInlineFigs = [];

function msgTagsHtml(tags) {
  if (!tags || !tags.length) return "";
  return '<div class="msg-tags">' + tags.map((t) => `<span class="m-tag">${esc(t)}</span>`).join("") + '</div>';
}

function msgContainer(role, tags) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const label = document.createElement("div");
  label.className = "msg-label";
  label.textContent = role === "user" ? "You" : "Fox";
  div.appendChild(label);
  const tagHtml = msgTagsHtml(tags);
  if (tagHtml) div.insertAdjacentHTML("beforeend", tagHtml);
  const body = document.createElement("div");
  body.className = "msg-body";
  div.appendChild(body);
  $("messages").appendChild(div);
  return { div, body };
}

function renderUserMessage(content, tags) {
  const { body } = msgContainer("user", tags);
  body.textContent = content;
  scrollBottom();
}

function ensureAssistant(tags) {
  if (curAssistantEl && document.body.contains(curAssistantEl.div)) return curAssistantEl;
  const el = msgContainer("assistant", tags);
  curAssistantEl = el;
  setConn("busy");
  state.streaming = true;
  return el;
}

function streamDelta(text) {
  const el = ensureAssistant();
  el.raw = (el.raw || "") + text;
  el.body.innerHTML = renderMarkdown(el.raw) + '<span class="cursor"></span>';
  scrollBottom();
}

function finalizeAssistant(content, tags) {
  const el = curAssistantEl;
  if (el) {
    el.raw = content || el.raw || "";
    el.body.innerHTML = renderMarkdown(el.raw);
    curAssistantEl = null;
  }
  state.streaming = false;
  // Attach figures that were emitted before the text (e.g. auto-workflows).
  while (pendingInlineFigs.length) {
    const art = pendingInlineFigs.shift();
    if (el) appendInlineFig(el, art);
  }
}

function scrollBottom() {
  const m = $("messages");
  m.scrollTop = m.scrollHeight;
}

/* -------- workflow progress panel (arXiv ingestion & replication) -------- */

const WF_STATES = {
  pending:          { cls: "pending",  ico: "○", label: "queued" },
  running:          { cls: "running",  ico: "◔", label: "running" },
  waiting_approval: { cls: "approval", ico: "⏸", label: "needs your approval" },
  done:             { cls: "done",     ico: "✓", label: "done" },
  failed:           { cls: "failed",   ico: "✗", label: "failed" },
};

function renderWorkflow(snap) {
  if (!snap) return;
  state.workflow = snap;
  const panel = $("workflow-panel");
  const stages = snap.stages || [];
  panel.classList.remove("hidden");
  if (!stages.length || snap.status === "idle") {
    $("workflow-title").textContent = "Workflow";
    $("workflow-state").textContent = "idle";
    $("workflow-state").className = "wf-state idle";
    $("workflow-status").textContent = "No pipeline is running — start one by asking the agent (e.g. ingest & replicate an arXiv paper, or run the privacy workflow).";
    $("workflow-fill").style.width = "0%";
    const wrap = $("workflow-stages");
    wrap.innerHTML = "";
    const row = document.createElement("div");
    row.className = "wf-stage pending";
    row.innerHTML = `<span class="wf-ico">○</span>
      <div class="wf-stage-body">
        <div class="wf-label">No active workflow</div>
        <div class="wf-detail">Progress will appear here live as the agent works.</div>
        <div class="wf-mini"><div class="wf-mini-fill" style="width:0%"></div></div>
      </div>`;
    wrap.appendChild(row);
    return;
  }
  $("workflow-title").textContent = snap.title || "Workflow";
  $("workflow-state").textContent = snap.status === "running" ? "running"
    : snap.status === "waiting_approval" ? "waiting" : snap.status;
  $("workflow-state").className = "wf-state " + snap.status;
  $("workflow-status").textContent = snap.message || "";
  $("workflow-fill").style.width = (snap.pct || 0) + "%";

  const wrap = $("workflow-stages");
  wrap.innerHTML = "";
  for (const s of stages) {
    const meta = WF_STATES[s.state] || WF_STATES.pending;
    const detail = s.detail || (s.state === "pending" ? "queued" : meta.label);
    const row = document.createElement("div");
    row.className = `wf-stage ${meta.cls}`;
    row.innerHTML = `
      <span class="wf-ico">${meta.ico}</span>
      <div class="wf-stage-body">
        <div class="wf-label">${esc(s.label)}</div>
        <div class="wf-detail">${esc(detail)}</div>
        <div class="wf-mini"><div class="wf-mini-fill" style="width:${Number(s.pct) || 0}%"></div></div>
      </div>`;
    wrap.appendChild(row);
  }
  scrollBottom();
}

async function loadWorkflow() {
  try {
    const r = await api(`/api/projects/${state.project}/workflow`);
    renderWorkflow(r.workflow);
  } catch (e) { /* silent */ }
}

/* -------- tool cards -------- */
function toolStart(p) {
  const el = ensureAssistant();
  const card = document.createElement("div");
  card.className = "toolcard";
  card.dataset.toolId = p.id;
  card.innerHTML = `
    <div class="toolcard-head">
      <span class="caret">▶</span>
      <span class="tname">${esc(p.name)}</span>
      <span class="targs">${esc(JSON.stringify(p.args))}</span>
      <span class="tstatus busy">…running</span>
    </div>
    <div class="toolcard-body"><pre class="tout">Running…</pre></div>`;
  card.querySelector(".toolcard-head").addEventListener("click", () => card.classList.toggle("open"));
  el.div.insertBefore(card, el.body.nextSibling);
  card.classList.add("open");
  scrollBottom();
}

function toolResult(p) {
  const card = document.querySelector(`.toolcard[data-tool-id="${esc(p.id)}"]`);
  if (!card) return;
  const status = card.querySelector(".tstatus");
  status.textContent = p.ok ? "✓ done" : "✗ failed";
  status.className = "tstatus " + (p.ok ? "ok" : "fail");
  card.querySelector(".tout").textContent = truncate(p.output || "", 4000);
}

/* ============================ artifacts ================================= */

let currentArtifact = null;

function addArtifact(art) {
  if (!state.artifacts) state.artifacts = [];
  if (!state.artifacts.some((a) => a.id === art.id)) state.artifacts.unshift(art);
}

async function renderArtifacts() {
  const list = $("artifact-list");
  list.innerHTML = "";
  const arts = state.artifacts || [];
  if (!arts.length) { list.innerHTML = '<div class="empty">No artifacts yet. Figures and saved tables appear here with full provenance.</div>'; return; }
  for (const a of arts) {
    const el = document.createElement("div");
    el.className = "artitem";
    el.innerHTML = `
      ${a.data_type === "png" ? `<img class="thumb" src="${B(`/artifacts/${a.id}`)}" alt="">` : ""}
      <div class="ainfo">
        <div class="aname">${esc(a.name)}</div>
        <div class="adesc">${esc(a.description || "")}</div>
        <span class="akind">${esc(a.kind)}</span>
      </div>
      <span class="adel" title="Delete">🗑</span>`;
    el.querySelector(".adel").addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/projects/${state.project}/artifacts/${a.id}`, { method: "DELETE" });
      state.artifacts = state.artifacts.filter((x) => x.id !== a.id);
      renderArtifacts();
    });
    el.addEventListener("click", () => openArtifact(a));
    list.appendChild(el);
  }
}

function openArtifact(a) {
  currentArtifact = a;
  $("art-title").textContent = `${a.name} — ${a.kind}`;
  const view = $("art-view");
  view.innerHTML = a.data_type === "png"
    ? `<img src="${B(`/artifacts/${a.id}`)}" alt="">`
    : `<pre>${esc(a.description)}\n\n${esc(a.data_type === "html" ? "(html artifact)" : "")}</pre>`;
  $("art-meta").textContent = `${a.kind} · created ${new Date(a.created_at * 1000).toLocaleString()} · ${a.size} bytes`;
  $("art-code").textContent = a.code || "(no code recorded)";
  $("art-env").textContent = a.env && Object.keys(a.env).length ? JSON.stringify(a.env, null, 2) : "(no env snapshot)";
  $("regen-input").value = "";
  $("regen-status").textContent = "";
  $("artifact-modal").classList.remove("hidden");
}

function renderArtifactInline(art) {
  if (art.data_type !== "png") return;
  if (curAssistantEl) {
    appendInlineFig(curAssistantEl, art);
  } else {
    // Figures that arrive before the assistant text (e.g. auto-workflows)
    // are attached to the completed message once it renders.
    pendingInlineFigs.push(art);
  }
}

function appendInlineFig(el, art) {
  // Skip if this figure was already rendered inline from markdown in the message.
  if (el.div.querySelector(`img[data-art-id="${esc(art.id)}"]`)) return;
  const fig = document.createElement("div");
  fig.className = "inline-fig";
  fig.innerHTML = `<img src="${B(`/artifacts/${art.id}`)}" alt="${esc(art.name)}" title="${esc(art.description)}">`;
  fig.querySelector("img").addEventListener("click", () => openArtifact(art));
  el.div.appendChild(fig);
  scrollBottom();
}

/* ============================ kernel / env =============================== */

function renderKernel(vars, env) {
  const v = $("kernel-vars");
  v.innerHTML = "";
  if (!vars || !Object.keys(vars).length) v.innerHTML = '<div class="empty">Kernel has no user variables.</div>';
  else for (const [name, t] of Object.entries(vars)) v.appendChild(kv(name, t));

  const e = $("kernel-env");
  e.innerHTML = "";
  if (env) for (const [name, ver] of Object.entries(env)) e.appendChild(kv(name, ver));
}

function kv(k, val) {
  const d = document.createElement("div");
  d.className = "kv";
  d.innerHTML = `<span class="k">${esc(k)}</span><span class="v">${esc(val)}</span>`;
  return d;
}

/* ============================ review / grants ============================ */

function renderReview(findings) {
  const c = $("review-findings");
  c.innerHTML = "";
  if (!findings.length) {
    c.innerHTML = '<div class="empty">No issues flagged. Reviewer runs after each turn.</div>';
    return;
  }
  for (const f of findings) {
    const d = document.createElement("div");
    d.className = `finding ${esc(f.severity || "info")}`;
    d.innerHTML = `<span class="sev">${esc(f.severity)}</span>${esc(f.message)}`;
    c.appendChild(d);
  }
}

function setReviewStatus(txt) {
  const c = $("review-findings");
  c.innerHTML = `<div class="empty">${esc(txt)}</div>`;
}

function renderGrants(grants) {
  const c = $("grant-list");
  c.innerHTML = "";
  if (!grants.length) { c.innerHTML = '<div class="empty">No permission grants yet. Shell commands prompt for approval.</div>'; return; }
  for (const g of grants) {
    const d = document.createElement("div");
    d.className = "finding info";
    d.innerHTML = `<span class="sev">${esc(g.decision)}</span><code>${esc(g.pattern)}</code> <span style="margin-left:6px">(${esc(g.kind)})</span>`;
    c.appendChild(d);
  }
}

/* ============================ approval =================================== */

function showApproval(p) {
  state.pendingApproval = p;
  $("approval-reason").textContent = p.reason || "A shell command requires your permission.";
  $("approval-command").textContent = p.command;
  $("approval-temp").checked = false;
  const modal = $("approval-modal");
  modal.classList.remove("hidden");
  setBusyStatus("⏸ Waiting for your approval…");
  // Popup alert so the ask is never silently missed.
  try {
    if ("Notification" in window) {
      if (Notification.permission === "granted") {
        new Notification("Fox needs your permission", { body: (p.command || "").slice(0, 120) });
      } else if (Notification.permission !== "denied") {
        Notification.requestPermission().then((perm) => {
          if (perm === "granted")
            new Notification("Fox needs your permission", { body: (p.command || "").slice(0, 120) });
        });
      }
    }
  } catch (e) { /* notifications unavailable */ }
  toast("⚠️ Permission required — approve or deny in the popup", 6000);
  // Keep drawing attention until it is resolved.
  if (state._approvalPulse) clearInterval(state._approvalPulse);
  state._approvalPulse = setInterval(() => modal.classList.toggle("pulse"), 900);
}

function closeApproval() {
  if (state._approvalPulse) { clearInterval(state._approvalPulse); state._approvalPulse = null; }
  $("approval-modal").classList.add("hidden");
}

$("approval-allow").addEventListener("click", () => {
  const temporary = $("approval-temp").checked;
  closeApproval();
  if (state.pendingApproval)
    send({ type: "approval", request_id: state.pendingApproval.request_id,
           decision: true, temporary });
  state.pendingApproval = null;
  setBusyStatus("");
});
$("approval-deny").addEventListener("click", () => {
  closeApproval();
  if (state.pendingApproval)
    send({ type: "approval", request_id: state.pendingApproval.request_id, decision: false });
  state.pendingApproval = null;
  setBusyStatus("");
});

/* ============================ turn lifecycle ============================== */

function onTurnDone() {
  setConn("ok");
  state.busy = false;
  $("send-btn").disabled = false;
  $("input").disabled = false;
  $("input").focus();
  refreshState();
}

function onError(msg) {
  setConn("ok");
  state.busy = false;
  $("send-btn").disabled = false;
  $("input").disabled = false;
  const el = ensureAssistant();
  el.body.innerHTML += `<p style="color:var(--danger)"><strong>Error:</strong> ${esc(msg)}</p>`;
  onTurnDone();
}

async function sendChat() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  autoResize(input);
  state.busy = true;
  $("send-btn").disabled = true;
  input.disabled = true;
  setConn("busy");
  send({ type: "chat", content: text });
}

/* ============================ settings =================================== */

function openSettings() {
  const c = state.config || { llm: {}, agent: {}, mcp: {} };
  $("cfg-base-url").value = c.llm.base_url || "";
  $("cfg-tool-url").value = c.llm.tool_base_url || "";
  $("cfg-model").value = c.llm.model || "";
  $("cfg-temp").value = c.llm.temperature ?? 0.2;
  $("cfg-reviewer").checked = c.agent?.reviewer_enabled !== false;
  const dl = $("model-list");
  dl.innerHTML = (state.models || []).map((m) => `<option value="${esc(m.id)}">`).join("");
  state.mcpServers = (c.mcp?.servers || []).map((s) => ({ ...s }));
  renderMcpList();
  $("settings-modal").classList.remove("hidden");
  refreshMcpStatus();
}

function renderMcpList() {
  const list = $("mcp-server-list");
  list.innerHTML = "";
  if (!(state.mcpServers || []).length) {
    list.innerHTML = '<div class="empty">No MCP servers configured. The built-in "science" server is the default.</div>';
    return;
  }
  for (const s of state.mcpServers) {
    const el = document.createElement("div");
    el.className = "nb-item";
    el.innerHTML = `<span class="nb-icon">🔌</span>
      <div class="nbinfo">
        <div class="nbname">${esc(s.name)} <span class="akind">${esc(s.transport || "stdio")}</span></div>
        <div class="nbmeta" data-mcp-status="${esc(s.name)}">${esc(s.command || s.url || "")}</div>
      </div>
      <span class="nb-badge" data-mcp-count="${esc(s.name)}"></span>
      <span class="adel" data-mcp-del="${esc(s.name)}" title="Remove">🗑</span>`;
    el.querySelector(`[data-mcp-del="${esc(s.name)}"]`).addEventListener("click", () => {
      state.mcpServers = state.mcpServers.filter((x) => x.name !== s.name);
      renderMcpList();
    });
    list.appendChild(el);
  }
}

async function refreshMcpStatus() {
  try {
    const r = await api("/api/mcp");
    for (const s of r.servers || []) {
      const meta = document.querySelector(`[data-mcp-status="${esc(s.name)}"]`);
      const badge = document.querySelector(`[data-mcp-count="${esc(s.name)}"]`);
      if (meta) meta.textContent = s.ok ? "connected" : (s.error || "offline");
      if (badge) badge.textContent = s.ok ? s.tools.length + " tools" : "✗";
    }
  } catch (e) { /* silent */ }
}

async function saveSettings() {
  const cfg = {
    llm: {
      base_url: $("cfg-base-url").value.trim(),
      tool_base_url: $("cfg-tool-url").value.trim(),
      model: $("cfg-model").value.trim(),
      temperature: parseFloat($("cfg-temp").value) || 0.2,
    },
    agent: { reviewer_enabled: $("cfg-reviewer").checked },
    mcp: { servers: state.mcpServers || [] },
  };
  try {
    const r = await api("/api/config", { method: "POST", body: JSON.stringify({ config: cfg }) });
    state.config = r.config;
    $("settings-modal").classList.add("hidden");
    toast("Settings saved. Reconnecting…");
    refreshModels();
  } catch (e) { toast(e.message, 4000); }
}

async function testConnection() {
  $("cfg-status").textContent = "Testing…";
  try {
    const r = await api("/api/models");
    state.models = r.models || [];
    const dl = $("model-list");
    dl.innerHTML = state.models.map((m) => `<option value="${esc(m.id)}">`).join("");
    if (state.models.length) $("cfg-status").textContent = `OK — ${state.models.length} local models`;
    else $("cfg-status").textContent = "Connected, but no models found";
  } catch (e) {
    $("cfg-status").textContent = "Failed: " + e.message;
  }
}

/* ============================ projects =================================== */

async function loadProjects() {
  const r = await api("/api/projects");
  state.projects = r.projects || [];
  const sel = $("project-select");
  sel.innerHTML = state.projects.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  if (!state.project) {
    const saved = localStorage.getItem("fox.project");
    state.project = state.projects.some((p) => p.name === saved) ? saved
      : (state.projects[0]?.name || "");
    if (!state.project) {
      await api("/api/projects", { method: "POST", body: JSON.stringify({ name: "default" }) });
      state.project = "default";
    }
  }
  sel.value = state.project;
}

async function switchProject(name) {
  if (state.ws) state.ws.close();
  state.project = name;
  localStorage.setItem("fox.project", name);
  state.artifacts = [];
  $("messages").innerHTML = "";
  curAssistantEl = null;
  await refreshState();
  connect();
}

async function refreshState() {
  try {
    const r = await api(`/api/projects/${state.project}/state`);
    state.artifacts = r.artifacts || [];
    renderMessages(r.messages || []);
    renderArtifacts();
    renderKernel(r.variables, r.env);
    renderReview(state._lastFindings || []);
    renderGrants(r.grants || []);
  } catch (e) { toast("Failed to load state: " + e.message, 4000); }
  refreshNotebooks();
  loadWorkflow();
}

function renderMessages(msgs) {
  const wrap = $("messages");
  wrap.innerHTML = "";
  for (const m of msgs) {
    const mtags = (m.meta && m.meta.tags) || [];
    if (m.role === "user") {
      const { body } = msgContainer("user", mtags);
      body.textContent = m.content;
    } else if (m.role === "assistant") {
      const { body } = msgContainer("assistant", mtags);
      body.innerHTML = renderMarkdown(m.content);
    } else if (m.role === "tool") {
      // tool results persisted; rendered as compact card
      const meta = m.meta || {};
      const card = document.createElement("div");
      card.className = "toolcard";
      card.innerHTML = `
        <div class="toolcard-head">
          <span class="caret">▶</span><span class="tname">${esc(meta.name || "tool")}</span>
          <span class="tstatus ok">persisted</span>
        </div>
        <div class="toolcard-body"><pre>${esc(truncate(m.content || "", 2000))}</pre></div>`;
      card.querySelector(".toolcard-head").addEventListener("click", () => card.classList.toggle("open"));
      wrap.appendChild(card);
    }
  }
  scrollBottom();
}

/* ============================ model refresh ============================== */

async function refreshModels() {
  const sel = $("model-select");
  try {
    const r = await api("/api/models");
    state.models = r.models || [];
    sel.innerHTML = state.models.map((m) => `<option value="${esc(m.id)}">${esc(m.id)}</option>`).join("");
    if (state.config?.llm?.model && state.models.some((m) => m.id === state.config.llm.model)) {
      sel.value = state.config.llm.model;
    }
  } catch (e) {
    sel.innerHTML = `<option>${esc(state.config?.llm?.model || "")}</option>`;
  }
}

function autoResize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

/* ============================ wire up ==================================== */

$("send-btn").addEventListener("click", sendChat);
// Clicking a figure rendered inside a chat message opens its artifact modal.
$("messages").addEventListener("click", (e) => {
  const img = e.target.closest("img.chat-fig");
  if (!img || !img.dataset.artId) return;
  const art = (state.artifacts || []).find((a) => a.id === img.dataset.artId);
  if (art) openArtifact(art);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$("input").addEventListener("input", (e) => autoResize(e.target));
$("new-project-btn").addEventListener("click", async () => {
  const name = prompt("New project name:");
  if (!name) return;
  try {
    await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects();
    await switchProject(name);
  } catch (e) { toast(e.message); }
});
$("project-select").addEventListener("change", (e) => switchProject(e.target.value));
$("model-select").addEventListener("change", async (e) => {
  const cfg = JSON.parse(JSON.stringify(state.config || {}));
  cfg.llm = cfg.llm || {};
  cfg.llm.model = e.target.value;
  const r = await api("/api/config", { method: "POST", body: JSON.stringify({ config: cfg }) });
  state.config = r.config;
  toast("Model set to " + e.target.value);
  if (state.ws) state.ws.close();
  setTimeout(connect, 200);
});
$("settings-btn").addEventListener("click", openSettings);
$("side-toggle").addEventListener("click", () => {
  const collapsed = document.getElementById("app").classList.toggle("side-collapsed");
  try { localStorage.setItem("fox.sidePanel", collapsed ? "0" : "1"); } catch (e) {}
});
try {
  if (localStorage.getItem("fox.sidePanel") === "0")
    document.getElementById("app").classList.add("side-collapsed");
} catch (e) {}
$("print-btn").addEventListener("click", () => {
  const h = $("print-header");
  h.innerHTML = `<h1>Fox - Experiment workbench · chat transcript</h1>`
    + `<div>Project: <strong>${esc(state.project || "—")}</strong> · Model: `
    + `<strong>${esc(state.config?.llm?.model || "—")}</strong> · Exported `
    + new Date().toLocaleString() + "</div>";
  window.print();
});$("settings-close").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
$("workflow-close").addEventListener("click", () => $("workflow-panel").classList.add("hidden"));
$("cfg-save").addEventListener("click", saveSettings);
$("cfg-test").addEventListener("click", testConnection);

$("mcp-add-btn").addEventListener("click", () => $("mcp-add-form").classList.remove("hidden"));
$("mcp-add-cancel").addEventListener("click", () => $("mcp-add-form").classList.add("hidden"));
$("mcp-add-save").addEventListener("click", () => {
  const name = $("mcp-name").value.trim();
  if (!name) { toast("Name required"); return; }
  const server = {
    name,
    transport: $("mcp-transport").value,
    trusted: $("mcp-trusted").checked,
  };
  if (server.transport === "stdio") {
    server.command = $("mcp-command").value.trim() || "{python}";
    server.args = $("mcp-args").value.split(",").map((s) => s.trim()).filter(Boolean);
    server.env = { PYTHONPATH: "." };
  } else {
    server.url = $("mcp-url").value.trim();
    try { server.headers = JSON.parse($("mcp-headers").value || "{}"); }
    catch { server.headers = {}; }
  }
  state.mcpServers = state.mcpServers || [];
  state.mcpServers.push(server);
  $("mcp-add-form").classList.add("hidden");
  $("mcp-name").value = ""; $("mcp-command").value = ""; $("mcp-args").value = "";
  $("mcp-url").value = ""; $("mcp-headers").value = ""; $("mcp-trusted").checked = false;
  renderMcpList();
});
$("art-close").addEventListener("click", () => $("artifact-modal").classList.add("hidden"));
$("artifact-modal").addEventListener("click", (e) => { if (e.target === $("artifact-modal")) $("artifact-modal").classList.add("hidden"); });

$("regen-btn").addEventListener("click", async () => {
  const instruction = $("regen-input").value.trim();
  if (!instruction || !currentArtifact) return;
  const status = $("regen-status");
  status.textContent = "Regenerating…";
  status.className = "regen-status";
  try {
    const r = await api(`/api/projects/${state.project}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ artifact_id: currentArtifact.id, instruction }),
    });
    if (r.artifact) {
      addArtifact(r.artifact);
      renderArtifacts();
      status.textContent = "New artifact created: " + r.artifact.id;
      currentArtifact = r.artifact;
      openArtifact(r.artifact);
      toast("Regenerated");
    } else if (r.error) {
      status.textContent = "Error: " + r.error;
      status.className = "regen-status err";
    }
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "regen-status err";
  }
});

$("kernel-reset-btn").addEventListener("click", async () => {
  await api(`/api/projects/${state.project}/kernel/reset`, { method: "POST" });
  toast("Kernel reset");
  refreshState();
});
$("review-btn").addEventListener("click", async () => {
  setReviewStatus("Reviewing…");
  try {
    // server runs reviewer automatically per turn; force via config? Use dedicated endpoint.
    toast("Review runs after the next turn.");
    setReviewStatus("Review runs after each assistant turn.");
  } catch (e) { toast(e.message); }
});

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("tab-" + t.dataset.tab).classList.add("active");
  });
});

/* ============================ experiments ================================= */

async function loadExperiments() {
  try {
    const r = await api("/api/experiments");
    state.expRuns = r.experiments || [];
    const g = await api("/api/experiments/graph");
    state.expGraph = g;
    populateExpMetrics();
    renderExperiments();
  } catch (e) { /* silent */ }
  try {
    const rr = await api(`/api/projects/${state.project}/runs`);
    state.agentRuns = rr.runs || [];
  } catch (e) { state.agentRuns = state.agentRuns || []; }
  populateExpCompare();
}

function expMetric() { return state.expMetric || "linkage50"; }
function expNodeValue(node, metric) {
  const v = (node.metrics && node.metrics[metric] != null) ? node.metrics[metric] : node[metric];
  return (v == null || Number.isNaN(Number(v))) ? null : Number(v);
}
function _fmtAxis(v) { return String(Math.round(Number(v) * 1000) / 1000); }

function populateExpMetrics() {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  const keys = new Set();
  nodes.forEach((n) => Object.keys(n.metrics || {}).forEach((k) => keys.add(k)));
  ["linkage50", "plausibility", "unique_pct", "rmse_eps0_1"].forEach((k) => keys.add(k));
  const opts = [...keys].sort();
  if (!opts.includes(state.expMetric)) state.expMetric = opts[0];
  const fill = (sel) => {
    if (!sel) return;
    sel.innerHTML = opts.map((k) =>
      `<option value="${esc(k)}">${esc(k.replace(/_/g, " "))}</option>`).join("");
    sel.value = state.expMetric;
  };
  fill($("exp-metric"));
  fill($("exp-metric-main"));
}
function _fmtNum(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return n >= 100 ? n.toFixed(0) : n >= 1 ? n.toFixed(2) : n.toFixed(3);
}
function _metricColor(v, min, max) {
  const t = (v - min) / ((max - min) || 1);
  const lerp = (a, b, x) => Math.round(a + (b - a) * Math.max(0, Math.min(1, x)));
  return `rgb(${lerp(79, 217, t)},${lerp(140, 164, t)},${lerp(255, 65, t)})`;
}

// --------------------------------------------------------- run comparison ----

function populateExpCompare() {
  const runs = (state.agentRuns || []).map((r) => ({
    value: String(r.id),
    label: `#${r.id} ${(r.prompt || "").replace(/\s+/g, " ").slice(0, 70)}`,
  })).reverse();
  const exps = ((state.expGraph && state.expGraph.nodes) || []).map((n) => ({
    value: String(n.id),
    label: `${n.label}${n.timestamp ? " · " + new Date(n.timestamp).toLocaleString() : ""}`,
  }));
  const opts = [...runs, ...exps];
  const selA = $("exp-cmp-a"), selB = $("exp-cmp-b"), res = $("exp-cmp-result");
  if (!opts.length) {
    selA.innerHTML = selB.innerHTML = "";
    res.innerHTML = '<div class="empty">No runs yet — run an experiment to compare.</div>';
    return;
  }
  const fill = (sel, idx) => {
    sel.innerHTML = opts.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
    sel.value = opts[Math.max(0, Math.min(idx, opts.length - 1))].value;
  };
  fill(selA, 0);
  fill(selB, Math.min(1, opts.length - 1));
  renderExpCompare();
}

async function renderExpCompare() {
  const el = $("exp-cmp-result");
  const a = $("exp-cmp-a").value, b = $("exp-cmp-b").value;
  if (!a || !b) return;
  el.innerHTML = '<div class="empty">Comparing…</div>';
  try {
    const r = await api(`/api/projects/${state.project}/compare?run_a=${encodeURIComponent(a)}&run_b=${encodeURIComponent(b)}`);
    const c = r.comparison;
    if (!c.rows.length) {
      el.innerHTML = `<div class="empty">No shared numeric metrics between <b>${esc(c.a)}</b> and <b>${esc(c.b)}</b>.</div>`;
      return;
    }
    const sum = c.summary;
    let rows = `<tr><th>metric</th><th>${esc(c.a)}</th><th>${esc(c.b)}</th><th>Δ</th><th>%</th></tr>`;
    for (const row of c.rows) {
      const cls = row.delta > 0 ? "delta-up" : row.delta < 0 ? "delta-down" : "";
      const arrow = row.delta > 0 ? "▲" : row.delta < 0 ? "▼" : "—";
      rows += `<tr><td>${esc(row.metric)}</td><td>${_fmtNum(row.a)}</td><td>${_fmtNum(row.b)}</td>
        <td class="${cls}">${arrow} ${_fmtNum(row.delta)}</td>
        <td class="${cls}">${row.pct > 0 ? "+" : ""}${_fmtNum(row.pct)}%</td></tr>`;
    }
    el.innerHTML = `<table class="cmp-table"><tbody>${rows}</tbody></table>
      <div class="cmp-summary muted">${sum.shared} shared metric(s) · ${sum.increased} up · ${sum.decreased} down · ${sum.unchanged} unchanged</div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty">Comparison failed: ${esc(e.message || e)}</div>`;
  }
}

// --------------------------------------------------------- timeline chart ----

function buildTimelineSvg(metric, W) {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  const H = 330, padL = 48, padR = 16, padT = 28, padB = 30;
  const vals = nodes.map((n) => expNodeValue(n, metric));
  const present = vals.filter((v) => v != null);
  if (!present.length) return '<div class="empty">No numeric values for this metric.</div>';
  const min = Math.min(...present), max = Math.max(...present), span = (max - min) || 1;
  const xs = nodes.map((_, i) => nodes.length > 1
    ? padL + i * (W - padL - padR) / (nodes.length - 1) : W / 2);
  const y = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);

  let out = `<svg viewBox="0 0 ${W} ${H}">`
    + `<defs><linearGradient id="tlfill" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0%" stop-color="#35c4b6" stop-opacity="0.35"/>`
    + `<stop offset="100%" stop-color="#35c4b6" stop-opacity="0"/></linearGradient></defs>`;

  for (let k = 0; k <= 4; k++) {
    const v = min + span * k / 4, yy = y(v);
    out += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="#232b36" stroke-width="0.5"></line>`;
    out += `<text x="${padL - 8}" y="${yy + 3}" text-anchor="end" font-size="10" fill="#8b97a5">${_fmtNum(v)}</text>`;
  }
  nodes.forEach((n, i) => {
    if (vals[i] != null)
      out += `<text x="${xs[i]}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#8b97a5">#${i + 1}</text>`;
  });

  const pts = nodes.map((n, i) => vals[i] == null ? null : `${xs[i]},${y(vals[i])}`).filter(Boolean);
  if (pts.length) {
    const linePts = pts.join(" ");
    const area = `${xs[0]},${y(min)} ${linePts} ${xs[nodes.length - 1]},${y(min)}`;
    out += `<polygon points="${area}" fill="url(#tlfill)"></polygon>`;
    out += `<polyline points="${linePts}" fill="none" stroke="#35c4b6" stroke-width="2"></polyline>`;
  }

  nodes.forEach((n, i) => {
    if (vals[i] == null) return;
    const color = n.fresh ? "#d9a441" : "#4f8cff";
    const sel = state.expSelected === n.id ? " selected" : "";
    const tip = `Run #${i + 1} · seed ${n.seed}${n.fresh ? " (fresh)" : ""}\n${metric}: ${_fmtNum(vals[i])}\n${n.timestamp ? new Date(n.timestamp).toLocaleString() : ""}`;
    out += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${xs[i]},${y(vals[i])})">`
      + `<title>${esc(tip)}</title>`
      + `<circle r="7" fill="${color}" stroke="#0b0f14" stroke-width="2"></circle>`
      + `<text y="-12" text-anchor="middle" font-size="10" font-weight="700" fill="${color}">#${i + 1}</text></g>`;
  });

  out += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="#d7dee7">${metric.replace(/_/g, " ")} — evolution across runs</text>`;
  out += `</svg>`;
  return out;
}

// --------------------------------------------------------- similarity graph --

function _forceLayout(nodes, edges, W, H) {
  const n = nodes.length;
  const byId = {}; nodes.forEach((x, i) => { byId[x.id] = i; });
  const pos = [];
  for (let i = 0; i < n; i++) {
    const a = i / n * 2 * Math.PI;
    pos.push({ x: W / 2 + Math.cos(a) * 120, y: H / 2 + Math.sin(a) * 120, vx: 0, vy: 0 });
  }
  for (let t = 0; t < 200; t++) {
    const cool = 1 - t / 200;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
      const d2 = Math.max(dx * dx + dy * dy, 1), f = 600 / d2 * cool, d = Math.sqrt(d2);
      dx /= d; dy /= d;
      pos[i].vx += dx * f; pos[i].vy += dy * f;
      pos[j].vx -= dx * f; pos[j].vy -= dy * f;
    }
    for (const e of edges) {
      const a = byId[e.source], b = byId[e.target];
      if (a == null || b == null) continue;
      const sim = e.similarity || 0.5;
      let dx = pos[b].x - pos[a].x, dy = pos[b].y - pos[a].y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = (d - (110 - sim * 50)) * 0.05 * (0.3 + sim);
      dx /= d; dy /= d;
      pos[a].vx += dx * f; pos[a].vy += dy * f;
      pos[b].vx -= dx * f; pos[b].vy -= dy * f;
    }
    for (const p of pos) {
      p.vx += (W / 2 - p.x) * 0.01 * cool;
      p.vy += (H / 2 - p.y) * 0.01 * cool;
      p.vx *= 0.85; p.vy *= 0.85;
      p.x = Math.max(46, Math.min(W - 46, p.x + p.vx));
      p.y = Math.max(40, Math.min(H - 40, p.y + p.vy));
    }
  }
  return pos;
}

// Per-experiment sub-nodes: tags, findings and artifact keywords, so the graph
// shows at a glance what each experiment contains.
function expSubNodes(run) {
  const s1 = run.stage1 || [], s2 = run.stage2 || {}, s3 = run.stage3 || [];
  const nodes = [];
  const tag = (label) => nodes.push({ kind: "tag", label, color: "#d9a441" });
  const find = (label) => nodes.push({ kind: "finding", label, color: "#4f8cff" });
  const art = (label) => nodes.push({ kind: "artifact", label, color: "#35c4b6" });

  // notebook / generic experiment run (has a metrics dict, no stage structure)
  if ((run.metrics && Object.keys(run.metrics).length) && !s1.length) {
    tag(run.fresh ? "fresh" : "deterministic");
    tag(run.kind === "notebook" ? "notebook" : "experiment");
    const m = run.metrics;
    if (m.clean_accuracy != null) find("clean " + (m.clean_accuracy * 100).toFixed(0) + "%");
    if (m.robust_accuracy != null) find("robust " + (m.robust_accuracy * 100).toFixed(0) + "%");
    if (m.asr != null) find("ASR " + (m.asr * 100).toFixed(0) + "%");
    if (m.eps != null) find("eps " + _fmtNum(m.eps));
    for (const a of (run.artifacts || []).slice(0, 3)) {
      const name = (typeof a === "object" ? a.name : a) || "";
      art(name.replace(/\.(png|md)$/, "").slice(0, 16));
    }
    return nodes;
  }

  tag(run.fresh ? "fresh" : "deterministic");
  if (last1Val(s1, "plausibility_verdict")) tag("plausibility " + last1Val(s1, "plausibility_verdict").toLowerCase());
  if (s2.reid_risk) tag("re-id " + s2.reid_risk.toLowerCase());
  if (s2.unique_pct != null) find("unique " + s2.unique_pct.toFixed(0) + "%");
  if (s2.extreme_unique_corner_cases != null) find("corner " + s2.extreme_unique_corner_cases);
  if (s3.length && s3[0].protection_index != null) find("DP prot " + (s3[0].protection_index * 100).toFixed(0) + "%");
  if (last1Val(s1, "linkage_success") != null) find("linkage " + (last1Val(s1, "linkage_success") * 100).toFixed(0) + "%");
  for (const a of (run.artifacts || []).slice(0, 3)) {
    const name = (typeof a === "object" ? a.name : a) || "";
    art(name.replace(/\.(png|md)$/, "").slice(0, 16));
  }
  return nodes;
}

function last1Val(s1, key) {
  return s1.length ? s1[s1.length - 1][key] : undefined;
}

function _separate(pos, n, minDist, W, H) {
  for (let iter = 0; iter < 60; iter++) {
    let moved = false;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let dx = pos[j].x - pos[i].x, dy = pos[j].y - pos[i].y;
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d < minDist && d > 0) {
        const push = (minDist - d) / 2;
        dx /= d; dy /= d;
        pos[i].x -= dx * push; pos[i].y -= dy * push;
        pos[j].x += dx * push; pos[j].y += dy * push;
        moved = true;
      }
    }
    if (!moved) break;
  }
  for (const p of pos) {
    p.x = Math.max(120, Math.min(W - 120, p.x));
    p.y = Math.max(110, Math.min(H - 110, p.y));
  }
  return pos;
}

function buildGraphSvg(metric, W) {
  const gnodes = (state.expGraph && state.expGraph.nodes) || [];
  const runs = state.expRuns || [];
  const edges = (state.expGraph && state.expGraph.edges) || [];
  const H = 580;
  if (!gnodes.length) return '<div class="empty">No runs yet.</div>';
  const nodes = gnodes.map((g, i) => ({
    id: g.id, seed: g.seed, fresh: g.fresh, index: i, run: runs[i] || {},
  }));
  const vals = gnodes.map((n) => expNodeValue(n, metric));
  const present = vals.filter((v) => v != null);
  const vmin = present.length ? Math.min(...present) : 0;
  const vmax = present.length ? Math.max(...present) : 1;
  let pos = _forceLayout(nodes, edges, W, H);
  pos = _separate(pos, nodes.length, 230, W, H);
  const byId = {}; nodes.forEach((n, i) => { byId[n.id] = i; });

  let out = `<svg viewBox="0 0 ${W} ${H}">`;

  // similarity edges with relation labels
  for (const e of edges) {
    const a = byId[e.source], b = byId[e.target];
    if (a == null || b == null) continue;
    const sim = e.similarity || 0, ov = e.overlap || 0;
    const x1 = pos[a].x, y1 = pos[a].y, x2 = pos[b].x, y2 = pos[b].y;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    out += `<g class="exp-edge-wrap"><line class="exp-edge" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" `
      + `stroke-width="${(0.7 + sim * 3).toFixed(2)}" opacity="${(0.25 + sim * 0.5).toFixed(2)}"></line>`
      + `<text x="${mx}" y="${my - 4}" text-anchor="middle" font-size="8.5" fill="#8b97a5" paint-order="stroke" stroke="#0b0f14" stroke-width="2.5">sim ${(sim * 100).toFixed(0)}% · ov ${(ov * 100).toFixed(0)}%</text></g>`;
  }

  // sub-node spokes (drawn under experiment nodes)
  for (const n of nodes) {
    const subs = expSubNodes(n.run);
    const R = 92;
    subs.forEach((s, i) => {
      const a = -Math.PI / 2 + i / subs.length * 2 * Math.PI;
      const sx = pos[n.index].x + Math.cos(a) * R;
      const sy = pos[n.index].y + Math.sin(a) * R;
      out += `<line x1="${pos[n.index].x}" y1="${pos[n.index].y}" x2="${sx}" y2="${sy}" `
        + `stroke="#8b97a5" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.9"></line>`;
    });
  }

  // experiment nodes + sub-nodes
  nodes.forEach((n, i) => {
    const v = vals[i];
    const color = v == null ? "#8b97a5" : _metricColor(v, vmin, vmax);
    const sel = state.expSelected === n.id ? " selected" : "";
    const tip = `Run #${i + 1} · seed ${n.seed}${n.fresh ? " (fresh)" : ""}\n${metric}: ${_fmtNum(v)}\nclick for full summary`;
    out += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${pos[i].x},${pos[i].y})">`
      + `<title>${esc(tip)}</title>`
      + `<circle r="16" fill="${color}" stroke="#0b0f14" stroke-width="2.5"></circle>`
      + `<text y="-24" text-anchor="middle" font-size="11" font-weight="700" fill="#d7dee7">Run #${i + 1}</text>`
      + `<text y="30" text-anchor="middle" font-size="9" fill="#8b97a5">seed ${n.seed}</text></g>`;

    const subs = expSubNodes(n.run);
    const R = 92;
    subs.forEach((s, k) => {
      const a = -Math.PI / 2 + k / subs.length * 2 * Math.PI;
      const sx = pos[i].x + Math.cos(a) * R;
      const sy = pos[i].y + Math.sin(a) * R;
      out += `<g class="exp-subnode" data-id="${esc(n.id)}" transform="translate(${sx},${sy})">`
        + `<title>${esc(`${n.seed}: ${s.kind} — ${s.label}`)}</title>`
        + `<circle r="6.5" fill="${s.color}" stroke="#0b0f14" stroke-width="1.2"></circle></g>`;
      // tiny visible label on the sub-node
      const lx = sx + Math.cos(a) * 14, ly = sy + Math.sin(a) * 14 + 3;
      out += `<text x="${lx}" y="${ly}" text-anchor="middle" font-size="8" fill="${s.color}" opacity="0.95" paint-order="stroke" stroke="#0b0f14" stroke-width="2">${esc(s.label)}</text>`;
    });
  });

  // legend
  out += `<g transform="translate(${W - 230}, 12)">`
    + `<text x="0" y="-4" font-size="9" fill="#8b97a5">${metric.replace(/_/g, " ")}</text>`;
  for (let i = 0; i < 70; i++) {
    const t = i / 69;
    out += `<rect x="${i}" y="0" width="2" height="9" fill="${_metricColor(vmin + t * (vmax - vmin), vmin, vmax)}"></rect>`;
  }
  out += `<text x="0" y="20" font-size="8.5" fill="#8b97a5">${_fmtNum(vmin)}</text>`
    + `<text x="69" y="20" text-anchor="end" font-size="8.5" fill="#8b97a5">${_fmtNum(vmax)}</text>`
    + `<text x="78" y="9" font-size="9" fill="#d9a441">● tag</text>`
    + `<text x="78" y="20" font-size="9" fill="#4f8cff">● finding</text>`
    + `<text x="78" y="31" font-size="9" fill="#35c4b6">● artifact</text></g>`;

  out += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="#d7dee7">experiment graph — ${metric.replace(/_/g, " ")} (spokes = tags · findings · artifacts; edge labels = similarity/overlap)</text>`;
  out += `</svg>`;
  return out;
}

function renderExperiments() {
  const runs = state.expRuns || [];
  const empty = '<div class="empty">No workflow runs yet. Trigger the privacy workflow in chat (or add &quot;rerun with fresh results&quot;) to build up a history.</div>';
  const metric = expMetric();
  const charts = [
    ["expmain-timeline", "timeline", 1240],
    ["expmain-graph", "graph", 1240],
  ];
  for (const [id, kind, w] of charts) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = runs.length
      ? (kind === "timeline" ? buildTimelineSvg(metric, w) : buildGraphSvg(metric, w))
      : empty;
  }
  renderExpDetail();
}

function selectRun(id) {
  state.expSelected = id;
  renderExperiments();
}

function switchMainView(view) {
  $("chat-panel").classList.toggle("hidden", view !== "chat");
  $("exp-panel").classList.toggle("hidden", view !== "experiments");
  $("agent-panel").classList.toggle("hidden", view !== "agent");
  document.querySelectorAll(".mainview-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mainview === view));
  const app = document.getElementById("app");
  if (view === "experiments" || view === "agent") {
    // maximize width: collapse the side panel for the expanded views
    if (state._sideBefore == null)
      state._sideBefore = app.classList.contains("side-collapsed");
    app.classList.add("side-collapsed");
  } else if (state._sideBefore != null) {
    app.classList.toggle("side-collapsed", !!state._sideBefore);
    state._sideBefore = null;
  }
  if (view === "experiments") loadExperiments();
  if (view === "agent") loadAgent();
}

/* ============================ agent dashboard ============================= */

async function loadAgent() {
  try {
    state.agent = await api("/api/agent");
    renderAgent();
  } catch (e) { /* silent */ }
}

function renderAgent() {
  const a = state.agent || {};
  const body = $("agent-body");
  if (!body) return;
  const escA = esc;

  let h = "";

  // agent tools (subagents / capabilities)
  h += `<div class="agent-card"><div class="agent-card-head">🛠 Agent tools (${(a.tools || []).length})</div>`;
  h += (a.tools || []).map((t) =>
    `<div class="agent-item"><span class="agent-name">${escA(t.name)}</span><span class="agent-desc">${escA(t.description)}</span></div>`).join("")
    || '<div class="empty">none</div>';
  h += `</div>`;

  // MCP servers
  h += `<div class="agent-card"><div class="agent-card-head">🔌 MCP servers (${(a.mcp || []).length})</div>`;
  for (const s of a.mcp || []) {
    h += `<div class="agent-mcp">
      <div class="agent-mcp-head">
        <span class="status-dot ${s.ok ? "ok" : ""}"></span>
        <b>${escA(s.name)}</b>
        <span class="muted">${escA(s.transport || "stdio")} · ${(s.tools || []).length} tools</span>
        ${s.ok ? "" : `<span class="muted">${escA(s.error || "offline")}</span>`}
        <button class="btn subtle small agent-mcp-del" data-name="${escA(s.name)}">remove</button>
      </div>
      <div class="agent-mcp-tools">${(s.tools || []).slice(0, 14).map((t) => `<span class="agent-tool-chip">${escA(t)}</span>`).join("")}</div>
    </div>`;
  }
  h += `<details class="agent-form"><summary>+ Add MCP server</summary>
    <input id="mcp-add-name" placeholder="name (e.g. uniprot)">
    <select id="mcp-add-transport"><option value="stdio">stdio</option><option value="http">streamable HTTP</option></select>
    <input id="mcp-add-command" placeholder="command, or URL for HTTP">
    <input id="mcp-add-args" placeholder="args, comma-separated (stdio)">
    <label class="check"><input id="mcp-add-trusted" type="checkbox"> trusted (skip approval)</label>
    <button id="mcp-add-save" class="btn primary small">Add</button>
    <span id="mcp-add-status" class="muted"></span>
  </details>`;
  h += `</div>`;

  // skills
  h += `<div class="agent-card"><div class="agent-card-head">🧩 Skills &amp; capabilities</div>`;
  h += `<div class="ed-sec">Custom skills (${(a.skills || []).length})</div>`;
  h += (a.skills || []).map((sk) =>
    `<div class="agent-item"><span class="agent-name">${escA(sk.name)}</span><span class="agent-desc">${escA(sk.description || sk.instruction || "")}</span><button class="btn subtle small agent-skill-del" data-id="${escA(sk.id)}">✕</button></div>`).join("")
    || '<div class="empty">No custom skills yet.</div>';
  h += `<details class="agent-form"><summary>+ Add skill</summary>
    <input id="skill-name" placeholder="skill name">
    <input id="skill-desc" placeholder="short description">
    <textarea id="skill-instruction" rows="3" placeholder="instruction the agent should follow when this skill applies"></textarea>
    <button id="skill-add" class="btn primary small">Add skill</button>
    <span id="skill-status" class="muted"></span>
  </details>`;
  h += `<div class="ed-sec">Bundled notebooks (${(a.bundled && a.bundled.notebooks || []).length})</div>`;
  h += `<div class="agent-bundled">${(a.bundled && a.bundled.notebooks || []).map((n) => `<span class="agent-tool-chip nb-run" data-nb="${escA(n)}">📓 ${escA(n)}</span>`).join("")}</div>`;
  h += `<div class="ed-sec">Bundled scripts (${(a.bundled && a.bundled.scripts || []).length})</div>`;
  h += `<div class="agent-bundled">${(a.bundled && a.bundled.scripts || []).map((s) => `<span class="agent-tool-chip">🐍 ${escA(s)}</span>`).join("")}</div>`;
  h += `</div>`;

  // status & add-ons
  h += `<div class="agent-card"><div class="agent-card-head">📊 Status &amp; add-ons</div>
    <table class="agent-table">
      <tr><th>LLM model</th><td>${escA(a.llm && a.llm.model || "—")}</td></tr>
      <tr><th>Gateway</th><td>${escA(a.llm && a.llm.base_url || "—")}</td></tr>
      <tr><th>Tool endpoint</th><td>${escA(a.llm && a.llm.tool_base_url || "—")}</td></tr>
      <tr><th>Projects</th><td>${(a.addons && a.addons.projects) ?? "—"}</td></tr>
      <tr><th>Experiments tracked</th><td>${(a.addons && a.addons.experiments) ?? "—"}</td></tr>
      <tr><th>Artifacts</th><td>${(a.addons && a.addons.artifacts) ?? "—"}</td></tr>
      <tr><th>Notebooks / scripts</th><td>${(a.addons && a.addons.notebooks) ?? 0} / ${(a.addons && a.addons.scripts) ?? 0}</td></tr>
      <tr><th>MCP connected</th><td>${(a.mcp || []).filter((s) => s.ok).length}/${(a.mcp || []).length}</td></tr>
    </table>
  </div>`;

  body.innerHTML = h;
}

// agent dashboard interactions (delegated)
$("agent-body").addEventListener("click", async (e) => {
  const del = e.target.closest(".agent-mcp-del");
  if (del) {
    await api(`/api/mcp/servers/${encodeURIComponent(del.dataset.name)}`, { method: "DELETE" });
    loadAgent();
    return;
  }
  const sdel = e.target.closest(".agent-skill-del");
  if (sdel) {
    await api(`/api/agent/skills/${encodeURIComponent(sdel.dataset.id)}`, { method: "DELETE" });
    loadAgent();
    return;
  }
  const run = e.target.closest(".nb-run");
  if (run && state.project) {
    toast("Running notebook " + run.dataset.nb + "…");
    try {
      const r = await api(`/api/projects/${state.project}/notebooks/${encodeURIComponent(run.dataset.nb)}/execute`,
        { method: "POST", body: JSON.stringify({ cells: "all" }) });
      toast("Ran " + r.report.filter((x) => x.ok).length + " cells in " + run.dataset.nb);
      refreshNotebooks();
    } catch (err) { toast(err.message, 4000); }
  }
});
$("agent-body").addEventListener("change", () => { /* inputs are read on save */ });
$("agent-body").addEventListener("click", (e) => {
  if (e.target.id === "mcp-add-save") {
    const body = {
      name: $("mcp-add-name").value.trim(),
      transport: $("mcp-add-transport").value,
      trusted: $("mcp-add-trusted").checked,
    };
    if (body.transport === "stdio") { body.command = $("mcp-add-command").value.trim(); body.args = $("mcp-add-args").value.trim(); }
    else { body.url = $("mcp-add-command").value.trim(); }
    if (!body.name) { $("mcp-add-status").textContent = "name required"; return; }
    api("/api/mcp/servers", { method: "POST", body: JSON.stringify(body) })
      .then(() => { $("mcp-add-status").textContent = "added"; loadAgent(); })
      .catch((err) => { $("mcp-add-status").textContent = err.message; });
  }
  if (e.target.id === "skill-add") {
    const body = { name: $("skill-name").value.trim(), description: $("skill-desc").value.trim(), instruction: $("skill-instruction").value.trim() };
    if (!body.name) { $("skill-status").textContent = "name required"; return; }
    api("/api/agent/skills", { method: "POST", body: JSON.stringify(body) })
      .then(() => { $("skill-status").textContent = "added (injected into agent context)"; loadAgent(); })
      .catch((err) => { $("skill-status").textContent = err.message; });
  }
});
$("agent-refresh").addEventListener("click", loadAgent);

function similarRuns(id) {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  const byId = {}; nodes.forEach((n) => { byId[n.id] = n; });
  const out = [];
  for (const e of (state.expGraph && state.expGraph.edges) || []) {
    let other = null;
    if (e.source === id) other = e.target;
    else if (e.target === id) other = e.source;
    if (!other) continue;
    const n = byId[other];
    if (n) out.push({ id: n.id, index: n.index, seed: n.seed,
                      similarity: e.similarity, overlap: e.overlap });
  }
  return out.sort((a, b) => (b.similarity || 0) - (a.similarity || 0)).slice(0, 3);
}

function renderExpDetail() {
  const el = $("exp-detail");
  const runs = state.expRuns || [];
  const idx = runs.findIndex((r) => r.id === state.expSelected);
  const run = idx >= 0 ? runs[idx] : null;
  if (!run) {
    const msg = '<div class="empty">Select a run node to see its summary, findings and related runs.</div>';
    if (el) el.innerHTML = msg;
    if ($("expmain-detail")) $("expmain-detail").innerHTML = msg;
    return;
  }
  const s1 = run.stage1 || [], s2 = run.stage2 || {}, s3 = run.stage3 || [];
  const last1 = s1[s1.length - 1] || {}, first3 = s3[0] || {};
  const set = run.settings || {};
  const badge = run.fresh ? '<span class="exp-badge fresh">fresh</span>'
                          : '<span class="exp-badge det">deterministic</span>';
  const time = new Date(run.timestamp).toLocaleString();
  const pct = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(1) + "%");

  // notebook / generic experiment run (metrics dict, no stage structure)
  if ((run.metrics && Object.keys(run.metrics).length) && !s1.length) {
    let h = `<div class="ed-head">Run #${idx + 1} · ${esc(run.label || "notebook")} ${badge}</div>`;
    h += `<div class="ed-meta">${time}</div>`;
    h += `<div class="ed-sec">Experiment</div>`;
    h += `<div class="ed-find">${esc(run.label || "notebook run")}`
      + (run.seed ? ` · seed ${run.seed}` : "") + ` · ${run.kind || "notebook"}</div>`;
    h += `<div class="ed-sec">Metrics</div><table>`;
    for (const [k, v] of Object.entries(run.metrics)) {
      h += `<tr><th>${esc(k.replace(/_/g, " "))}</th><td>${_fmtNum(v)}</td></tr>`;
    }
    h += `</table>`;
    h += `<div class="ed-sec">Artifacts</div>`;
    for (const a of run.artifacts || []) {
      const obj = typeof a === "object" ? a : { name: a, id: null };
      h += `<a class="ed-art" data-art-id="${esc(obj.id || "")}">📄 ${esc(obj.name || obj.id)}</a>`;
    }
    if (el) el.innerHTML = h;
    if ($("expmain-detail") && $("expmain-detail") !== el) $("expmain-detail").innerHTML = h;
    return;
  }

  let h = `<div class="ed-head">Run #${idx + 1} · seed ${run.seed} ${badge}</div>`;
  h += `<div class="ed-meta">${time}</div>`;
  h += `<div class="ed-sec">Settings</div><table>
    <tr><th>Population</th><td>${set.population_size ?? "—"}</td></tr>
    <tr><th>Victim sample</th><td>${set.victim_size ?? "—"}</td></tr>
    <tr><th>Coverage</th><td>${(set.coverage_levels || []).join(", ") || "—"}</td></tr>
    <tr><th>ε levels</th><td>${(set.dp_epsilons || []).join(", ") || "—"}</td></tr>
  </table>`;
  h += `<div class="ed-sec">Key metrics</div><table>
    <tr><th>Linkage @50% coverage</th><td>${pct(last1.linkage_success)}</td></tr>
    <tr><th>Plausibility @50%</th><td>${last1.attack_plausibility != null ? last1.attack_plausibility.toFixed(2) + " (" + (last1.plausibility_verdict || "?") + ")" : "—"}</td></tr>
    <tr><th>Unique records</th><td>${s2.unique_pct != null ? s2.unique_pct.toFixed(1) + "%" : "—"}</td></tr>
    <tr><th>Extreme+unique corner cases</th><td>${s2.extreme_unique_corner_cases ?? "—"}</td></tr>
    <tr><th>Re-identification risk</th><td>${s2.reid_risk ?? "—"}</td></tr>
    <tr><th>Membership advantage</th><td>${s2.membership_advantage ?? "—"}</td></tr>
    <tr><th>DP attacker RMSE @ε=0.1</th><td>${first3.attacker_pred_rmse != null ? first3.attacker_pred_rmse.toFixed(2) : "—"}</td></tr>
  </table>`;
  if (s2.message) h += `<div class="ed-sec">Findings</div><div class="ed-find">${esc(s2.message)}</div>`;
  h += `<div class="ed-sec">Artifacts</div>`;
  for (const a of run.artifacts || []) {
    const obj = typeof a === "object" ? a : { name: a, id: null };
    h += `<a class="ed-art" data-art-id="${esc(obj.id || "")}">📄 ${esc(obj.name || obj.id)}</a>`;
  }
  const sims = similarRuns(run.id);
  if (sims.length) {
    h += `<div class="ed-sec">Similar / overlapping runs</div>`;
    for (const s of sims) {
      h += `<div class="ed-sim"><a class="ed-sim-link" data-id="${esc(s.id)}">Run #${s.index + 1} (seed ${s.seed})</a> — similarity <b>${((s.similarity || 0) * 100).toFixed(0)}%</b> · overlap <b>${((s.overlap || 0) * 100).toFixed(0)}%</b></div>`;
    }
  }
  if (el) el.innerHTML = h;
  const elMain = $("expmain-detail");
  if (elMain && elMain !== el) elMain.innerHTML = h;
}

async function openArtifactById(id) {
  if (!id) return;
  try {
    const r = await api(`/api/artifacts/${encodeURIComponent(id)}/meta`);
    openArtifact(r.artifact);
  } catch (e) { toast("Artifact not found"); }
}

["expmain-timeline", "expmain-graph"].forEach((id) => {
  $(id).addEventListener("click", (e) => {
    const n = e.target.closest(".exp-node, .exp-subnode");
    if (n && n.dataset.id) selectRun(n.dataset.id);
  });
});
["exp-detail", "expmain-detail"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", (e) => {
    const sim = e.target.closest(".ed-sim-link");
    if (sim) { selectRun(sim.dataset.id); return; }
    const art = e.target.closest(".ed-art");
    if (art) openArtifactById(art.dataset.artId);
  });
});
$("exp-refresh-main").addEventListener("click", loadExperiments);
$("exp-cmp-go").addEventListener("click", renderExpCompare);
$("exp-cmp-a").addEventListener("change", renderExpCompare);
$("exp-cmp-b").addEventListener("change", renderExpCompare);

function setExpMetric(v) {
  state.expMetric = v;
  if ($("exp-metric")) $("exp-metric").value = v;
  if ($("exp-metric-main")) $("exp-metric-main").value = v;
  renderExperiments();
}
if ($("exp-metric")) $("exp-metric").addEventListener("change", (e) => setExpMetric(e.target.value));
$("exp-metric-main").addEventListener("change", (e) => setExpMetric(e.target.value));

function bindExpView(scope) {
  const root = scope === "main" ? "#exp-panel" : "#side-panel";
  const tl = scope === "main" ? "expmain-timeline" : "exp-timeline";
  const gr = scope === "main" ? "expmain-graph" : "exp-graph";
  document.querySelectorAll(root + " .expview-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll(root + " .expview-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      $(tl).classList.toggle("hidden", b.dataset.expview !== "timeline");
      $(gr).classList.toggle("hidden", b.dataset.expview !== "graph");
    });
  });
}
bindExpView("side");
bindExpView("main");

$("mainview-chat").addEventListener("click", () => switchMainView("chat"));
$("mainview-experiments").addEventListener("click", () => switchMainView("experiments"));
$("mainview-agent").addEventListener("click", () => switchMainView("agent"));

/* ============================ notebooks =================================== */

let currentNotebook = null; // {name, nb}

function setNbStatus(msg, isErr) {
  const el = $("nb-status");
  el.textContent = msg || "";
  el.className = "nb-status" + (isErr ? " err" : "");
}

function _toSourceLines(str) {
  return str.split("\n").map((l, i, a) => (i < a.length - 1 ? l + "\n" : l));
}

async function refreshNotebooks() {
  try {
    const r = await api(`/api/projects/${state.project}/notebooks`);
    state.notebooks = r.notebooks || [];
    renderNotebooks();
  } catch (e) { /* silent */ }
}

function renderNotebooks() {
  const list = $("notebook-list");
  if (!list) return;
  list.innerHTML = "";
  const nbs = state.notebooks || [];
  if (!nbs.length) {
    list.innerHTML = '<div class="empty">No notebooks yet. Create one to run experiments as .ipynb — results are held in the notebook.</div>';
    return;
  }
  // Normalise tags (notebooks without tags fall under "untagged").
  const tagsOf = (nb) => (nb.tags && nb.tags.length ? nb.tags : ["untagged"]);
  const allTags = [...new Set(nbs.flatMap(tagsOf))].sort();

  // tag filter chips
  const chips = $("nb-tagchips");
  if (chips) {
    let h = `<button class="nb-chip ${state.nbTag === "all" ? "active" : ""}" data-tag="all">All (${nbs.length})</button>`;
    for (const t of allTags) {
      const c = nbs.filter((nb) => tagsOf(nb).includes(t)).length;
      h += `<button class="nb-chip ${state.nbTag === t ? "active" : ""}" data-tag="${esc(t)}">${esc(t)} (${c})</button>`;
    }
    chips.innerHTML = h;
  }

  const filtered = state.nbTag === "all"
    ? nbs : nbs.filter((nb) => tagsOf(nb).includes(state.nbTag));
  const groups = state.nbTag === "all" ? allTags : [state.nbTag];

  for (const g of groups) {
    const items = state.nbTag === "all"
      ? filtered.filter((nb) => tagsOf(nb).includes(g))
      : filtered;
    if (!items.length) continue;
    const head = document.createElement("div");
    head.className = "nb-group";
    head.textContent = `🏷 ${g} — ${items.length}`;
    list.appendChild(head);
    for (const nb of items) list.appendChild(nbItem(nb));
  }
}

function nbItem(nb) {
  const el = document.createElement("div");
  el.className = "nb-item";
  const tags = ((nb.tags && nb.tags.length ? nb.tags : ["untagged"]).map((t) => `<span class="nb-tag">${esc(t)}</span>`)).join("");
  el.innerHTML = `<span class="nb-icon">📓</span>
    <div class="nbinfo">
      <div class="nbname">${esc(nb.name)}${tags}</div>
      ${nb.description ? `<div class="nbdesc">${esc(nb.description)}</div>` : ""}
      <div class="nbmeta">${nb.cells} cells · ${nb.code_cells} code · ${esc(nb.source || "project")}${nb.source === "examples" ? " · demo" : ""}</div>
    </div>
    <span class="nb-badge ${nb.executions ? "run" : ""}">${nb.executions ? nb.executions + " runs" : "idle"}</span>`;
  el.addEventListener("click", () => openNotebook(nb.name));
  return el;
}

// tag chip filtering (delegated; chips are re-rendered each time)
$("nb-tagchips").addEventListener("click", (e) => {
  const chip = e.target.closest(".nb-chip");
  if (!chip) return;
  state.nbTag = chip.dataset.tag;
  renderNotebooks();
});

async function openNotebook(name) {
  try {
    const r = await api(`/api/projects/${state.project}/notebooks/${encodeURIComponent(name)}`);
    currentNotebook = { name, nb: r.notebook };
    $("nb-title").textContent = name + ".ipynb";
    renderNotebookCells();
    $("notebook-modal").classList.remove("hidden");
    // Sizes only resolve once the modal is visible (hidden elements have no layout).
    requestAnimationFrame(() =>
      document.querySelectorAll(".nb-source").forEach(autoSizeNbSource));
    setNbStatus("");
  } catch (e) { toast(e.message, 4000); }
}

function renderNotebookCells() {
  const wrap = $("nb-cells");
  wrap.innerHTML = "";
  (currentNotebook.nb.cells || []).forEach((cell, idx) => wrap.appendChild(buildNbCell(cell, idx)));
}

function buildNbCell(cell, idx) {
  const div = document.createElement("div");
  div.className = "nb-cell";
  const head = document.createElement("div");
  head.className = "nb-cell-head";
  const meta = cell.metadata?.fox || {};
  const numClass = meta.ok ? "done" : (meta.ok === false ? "fail" : "");
  const numText = cell.execution_count ? "[" + cell.execution_count + "]" : "";
  const actions = cell.cell_type === "code"
    ? '<button class="nb-run">Run</button><button class="nb-del">del</button>'
    : '<button class="nb-del">del</button>';
  head.innerHTML = `<span class="nb-cell-tag">${esc(cell.cell_type)}</span>
    <div class="nb-cell-actions">${actions}</div>
    <span class="nb-cellnum ${numClass}">${numText}</span>`;
  div.appendChild(head);

  if (cell.cell_type === "markdown") {
    const md = document.createElement("div");
    md.className = "nb-markdown";
    md.innerHTML = renderMarkdown((cell.source || []).join(""));
    div.appendChild(md);
  } else {
    const ta = document.createElement("textarea");
    ta.className = "nb-source";
    ta.value = (cell.source || []).join("");
    div.appendChild(ta);
    const outputs = document.createElement("div");
    outputs.className = "nb-outputs";
    renderNbOutputs(outputs, cell.outputs || []);
    div.appendChild(outputs);
    head.querySelector(".nb-run").addEventListener("click", () => runNbCell(idx));
    // Auto-grow the cell so all of its code is visible (up to a cap).
    autoSizeNbSource(ta);
    ta.addEventListener("input", () => autoSizeNbSource(ta));
  }
  head.querySelector(".nb-del").addEventListener("click", () => {
    currentNotebook.nb.cells.splice(idx, 1);
    renderNotebookCells();
  });
  return div;
}

function autoSizeNbSource(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 480) + "px";
}

function renderNbOutputs(container, outputs) {
  container.innerHTML = "";
  for (const o of outputs || []) {
    if (o.output_type === "stream") {
      const el = document.createElement("div");
      el.className = "nb-out stream";
      el.textContent = (o.text || []).join("");
      container.appendChild(el);
    } else if (o.output_type === "display_data") {
      if (o.data && o.data["image/png"]) {
        const im = document.createElement("img");
        im.src = "data:image/png;base64," + o.data["image/png"];
        container.appendChild(im);
      } else {
        const el = document.createElement("div");
        el.className = "nb-out stream";
        const t = o.data && o.data["text/plain"];
        el.textContent = Array.isArray(t) ? t.join("") : (t || "");
        container.appendChild(el);
      }
    } else if (o.output_type === "error") {
      const el = document.createElement("div");
      el.className = "nb-out error";
      el.textContent = (o.traceback || []).join("\n") || (o.ename + ": " + o.evalue);
      container.appendChild(el);
    }
  }
}

function syncNbSources() {
  document.querySelectorAll("#nb-cells .nb-cell").forEach((el, idx) => {
    const ta = el.querySelector(".nb-source");
    if (ta && currentNotebook.nb.cells[idx]) {
      currentNotebook.nb.cells[idx].source = _toSourceLines(ta.value);
    }
  });
}

function updateNbCellInPlace(idx, newCell) {
  // Update a single cell's outputs/status without re-rendering, so the
  // auto-sized source textarea keeps its height (dynamic insertion).
  const el = document.querySelectorAll("#nb-cells .nb-cell")[idx];
  if (!el || !newCell) return;
  const meta = newCell.metadata?.fox || {};
  const num = el.querySelector(".nb-cellnum");
  if (num) {
    num.className = "nb-cellnum " + (meta.ok ? "done" : (meta.ok === false ? "fail" : ""));
    num.textContent = newCell.execution_count ? "[" + newCell.execution_count + "]" : "";
  }
  const out = el.querySelector(".nb-outputs");
  if (out) renderNbOutputs(out, newCell.outputs || []);
}

async function runNbCell(idx) {
  syncNbSources();
  setNbStatus("Running cell " + idx + "…");
  try {
    const res = await api(
      `/api/projects/${state.project}/notebooks/${encodeURIComponent(currentNotebook.name)}/execute`,
      { method: "POST", body: JSON.stringify({ cells: String(idx) }) });
    currentNotebook.nb = res.notebook;
    updateNbCellInPlace(idx, res.notebook.cells[idx]);
    const r0 = res.report[0];
    setNbStatus("Cell " + idx + (r0 && r0.ok ? " ran ok" : " failed"), !(r0 && r0.ok));
    afterNbRun();
  } catch (e) { setNbStatus("Error: " + e.message, true); }
}

async function runAllNb() {
  syncNbSources();
  setNbStatus("Running all cells…");
  try {
    const res = await api(
      `/api/projects/${state.project}/notebooks/${encodeURIComponent(currentNotebook.name)}/execute`,
      { method: "POST", body: JSON.stringify({ cells: "all" }) });
    currentNotebook.nb = res.notebook;
    for (const r of res.report) updateNbCellInPlace(r.index, res.notebook.cells[r.index]);
    const fails = res.report.filter((r) => !r.ok).length;
    setNbStatus("Ran " + res.report.length + " code cell(s)" + (fails ? " — " + fails + " failed" : ""));
    afterNbRun();
  } catch (e) { setNbStatus("Error: " + e.message, true); }
}

async function saveNb() {
  syncNbSources();
  try {
    const r = await api(
      `/api/projects/${state.project}/notebooks/${encodeURIComponent(currentNotebook.name)}`,
      { method: "PUT", body: JSON.stringify({ cells: currentNotebook.nb.cells }) });
    currentNotebook.nb = r.notebook;
    renderNotebookCells();
    setNbStatus("Saved");
  } catch (e) { setNbStatus("Error: " + e.message, true); }
}

function addNbCell() {
  syncNbSources();
  currentNotebook.nb.cells.push({
    cell_type: "code", id: Math.random().toString(36).slice(2, 8),
    metadata: {}, source: [""], execution_count: null, outputs: [],
  });
  renderNotebookCells();
  const last = $("nb-cells").lastElementChild;
  if (last) last.querySelector(".nb-source")?.focus();
}

function afterNbRun() {
  refreshNotebooks();
  refreshArtifacts();
  refreshKernelPanel();
}

async function refreshArtifacts() {
  try {
    const r = await api(`/api/projects/${state.project}/artifacts`);
    state.artifacts = r.artifacts || [];
    renderArtifacts();
  } catch (e) { /* silent */ }
}

async function refreshKernelPanel() {
  try {
    const r = await api(`/api/projects/${state.project}/state`);
    renderKernel(r.variables, r.env);
  } catch (e) { /* silent */ }
}

$("nb-new-btn").addEventListener("click", () => {
  $("nb-new-name").value = "my_experiment";
  $("nb-new-code").value = "";
  $("nb-new-modal").classList.remove("hidden");
});
$("nb-new-close").addEventListener("click", () => $("nb-new-modal").classList.add("hidden"));
$("nb-new-create").addEventListener("click", async () => {
  const name = $("nb-new-name").value.trim() || "untitled";
  const code = $("nb-new-code").value;
  const cells = [{ cell_type: "markdown", source: "# " + name + "\n" }];
  if (code.trim()) cells.push({ cell_type: "code", source: code });
  try {
    const r = await api(`/api/projects/${state.project}/notebooks`, {
      method: "POST", body: JSON.stringify({ name, cells }),
    });
    $("nb-new-modal").classList.add("hidden");
    await refreshNotebooks();
    openNotebook(r.name);
  } catch (e) { toast(e.message, 4000); }
});
$("nb-add-cell").addEventListener("click", addNbCell);
$("nb-save").addEventListener("click", saveNb);
$("nb-run-all").addEventListener("click", runAllNb);
$("nb-close").addEventListener("click", () => $("notebook-modal").classList.add("hidden"));
$("notebook-modal").addEventListener("click", (e) => {
  if (e.target === $("notebook-modal")) $("notebook-modal").classList.add("hidden");
});

/* ============================ init ======================================= */

(async function init() {
  try {
    const c = await api("/api/config");
    state.config = c.config;
  } catch (e) { toast("Backend not reachable"); }
  await loadProjects();
  $("model-select").value = state.config?.llm?.model || "";
  await refreshModels();
  await refreshState();
  loadExperiments();
  connect();
})();
