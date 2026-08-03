/* Fox — AI Science Workbench frontend */

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
    case "user_message": renderUserMessage(p.content); break;
    case "stream_delta": streamDelta(p.text); break;
    case "assistant_message": finalizeAssistant(p.content); break;
    case "tool_start": toolStart(p); break;
    case "tool_result": toolResult(p); break;
    case "artifact": addArtifact(p.artifact); renderArtifacts(); renderArtifactInline(p.artifact); break;
    case "approval_request": showApproval(p); break;
    case "review_start": setReviewStatus("Reviewing latest turn…"); break;
    case "review": renderReview(p.findings || []); break;
    case "done": onTurnDone(); loadExperiments(); break;
    case "error": onError(p.message); break;
  }
}

/* ============================ chat rendering ============================= */

let curAssistantEl = null;
let pendingInlineFigs = [];

function msgContainer(role) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const label = document.createElement("div");
  label.className = "msg-label";
  label.textContent = role === "user" ? "You" : "Fox";
  div.appendChild(label);
  const body = document.createElement("div");
  body.className = "msg-body";
  div.appendChild(body);
  $("messages").appendChild(div);
  return { div, body };
}

function renderUserMessage(content) {
  const { body } = msgContainer("user");
  body.textContent = content;
  scrollBottom();
}

function ensureAssistant() {
  if (curAssistantEl && document.body.contains(curAssistantEl.div)) return curAssistantEl;
  const el = msgContainer("assistant");
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

function finalizeAssistant(content) {
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
  $("approval-modal").classList.remove("hidden");
}

$("approval-allow").addEventListener("click", () => {
  $("approval-modal").classList.add("hidden");
  if (state.pendingApproval) send({ type: "approval", request_id: state.pendingApproval.request_id, decision: true });
  state.pendingApproval = null;
});
$("approval-deny").addEventListener("click", () => {
  $("approval-modal").classList.add("hidden");
  if (state.pendingApproval) send({ type: "approval", request_id: state.pendingApproval.request_id, decision: false });
  state.pendingApproval = null;
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
}

function renderMessages(msgs) {
  const wrap = $("messages");
  wrap.innerHTML = "";
  for (const m of msgs) {
    if (m.role === "user") {
      const { body } = msgContainer("user");
      body.textContent = m.content;
    } else if (m.role === "assistant") {
      const { body } = msgContainer("assistant");
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
  h.innerHTML = `<h1>Fox — AI Science Workbench · chat transcript</h1>`
    + `<div>Project: <strong>${esc(state.project || "—")}</strong> · Model: `
    + `<strong>${esc(state.config?.llm?.model || "—")}</strong> · Exported `
    + new Date().toLocaleString() + "</div>";
  window.print();
});$("settings-close").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
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
    if (t.dataset.tab === "experiments") loadExperiments();
  });
});

/* ============================ experiments ================================= */

async function loadExperiments() {
  try {
    const r = await api("/api/experiments");
    state.expRuns = r.experiments || [];
    const g = await api("/api/experiments/graph");
    state.expGraph = g;
    renderExperiments();
  } catch (e) { /* silent */ }
}

function expMetric() { return $("exp-metric") ? $("exp-metric").value : "linkage50"; }
function expView() {
  const b = document.querySelector(".expview-btn.active");
  return b ? b.dataset.expview : "timeline";
}
function expNodeValue(run, metric) {
  const v = run[metric];
  return (v == null || Number.isNaN(Number(v))) ? null : Number(v);
}
function _fmtAxis(v) { return String(Math.round(Number(v) * 1000) / 1000); }

function buildExpSvg(opts) {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  const metric = expMetric();
  const W = 640, H = 300, padL = 40, padR = 14, padT = 18, padB = 26;
  const vals = nodes.map((n) => expNodeValue(n, metric));
  const present = vals.filter((v) => v != null);
  if (!present.length) return '<div class="empty">No numeric values for this metric.</div>';
  const min = Math.min(...present), max = Math.max(...present), span = (max - min) || 1;
  const xs = nodes.map((_, i) => nodes.length > 1
    ? padL + i * (W - padL - padR) / (nodes.length - 1) : W / 2);
  const y = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);

  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = min + span * k / 4, yy = y(v);
    g += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="#232b36" stroke-width="0.5"></line>`;
    g += `<text x="${padL - 6}" y="${yy + 3}" text-anchor="end" font-size="9" fill="#8b97a5">${_fmtAxis(v)}</text>`;
  }

  let line = "";
  if (opts.line) {
    const pts = vals.map((v, i) => v == null ? null : `${xs[i]},${y(v)}`).filter(Boolean).join(" ");
    if (pts) line = `<polyline points="${pts}" fill="none" stroke="#35c4b6" stroke-width="1.5" opacity="0.7"></polyline>`;
  }

  let edges = "";
  if (opts.edges && state.expGraph) {
    const byId = {}; nodes.forEach((n, i) => { byId[n.id] = i; });
    for (const e of state.expGraph.edges || []) {
      const a = byId[e.source], b = byId[e.target];
      if (a == null || b == null || vals[a] == null || vals[b] == null) continue;
      const sim = e.similarity || 0;
      if (sim < 0.35 && !(e.overlap > 0.5)) continue;
      edges += `<line class="exp-edge" x1="${xs[a]}" y1="${y(vals[a])}" x2="${xs[b]}" `
        + `y2="${y(vals[b])}" stroke-width="${(0.5 + sim * 2.5).toFixed(2)}"></line>`;
    }
  }

  let nodeSvg = "";
  nodes.forEach((n, i) => {
    if (vals[i] == null) return;
    const color = n.fresh ? "#d9a441" : "#4f8cff";
    const sel = state.expSelected === n.id ? " selected" : "";
    nodeSvg += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${xs[i]},${y(vals[i])})">`
      + `<circle r="7" fill="${color}"></circle><text y="-11" text-anchor="middle">#${i + 1}</text></g>`;
  });

  return `<svg viewBox="0 0 ${W} ${H}">${g}${line}${edges}${nodeSvg}</svg>`;
}

function renderExperiments() {
  const runs = state.expRuns || [];
  const empty = '<div class="empty">No workflow runs yet. Trigger the privacy workflow in chat (or add &quot;rerun with fresh results&quot;) to build up a history.</div>';
  const tl = $("exp-timeline"), gr = $("exp-graph");
  if (tl) tl.innerHTML = runs.length ? buildExpSvg({ line: true, edges: false }) : empty;
  if (gr) gr.innerHTML = runs.length ? buildExpSvg({ line: false, edges: true }) : empty;
  renderExpDetail();
}

function selectRun(id) {
  state.expSelected = id;
  renderExperiments();
}

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
    if (el) el.innerHTML = '<div class="empty">Select a run node to see its summary, findings and related runs.</div>';
    return;
  }
  const s1 = run.stage1 || [], s2 = run.stage2 || {}, s3 = run.stage3 || [];
  const last1 = s1[s1.length - 1] || {}, first3 = s3[0] || {};
  const set = run.settings || {};
  const badge = run.fresh ? '<span class="exp-badge fresh">fresh</span>'
                          : '<span class="exp-badge det">deterministic</span>';
  const time = new Date(run.timestamp).toLocaleString();
  const pct = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(1) + "%");

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
  el.innerHTML = h;
}

async function openArtifactById(id) {
  if (!id) return;
  try {
    const r = await api(`/api/artifacts/${encodeURIComponent(id)}/meta`);
    openArtifact(r.artifact);
  } catch (e) { toast("Artifact not found"); }
}

["exp-timeline", "exp-graph"].forEach((id) => {
  $(id).addEventListener("click", (e) => {
    const n = e.target.closest(".exp-node");
    if (n) selectRun(n.dataset.id);
  });
});
$("exp-detail").addEventListener("click", (e) => {
  const sim = e.target.closest(".ed-sim-link");
  if (sim) { selectRun(sim.dataset.id); return; }
  const art = e.target.closest(".ed-art");
  if (art) openArtifactById(art.dataset.artId);
});
$("exp-refresh").addEventListener("click", loadExperiments);
$("exp-metric").addEventListener("change", renderExperiments);
document.querySelectorAll(".expview-btn").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".expview-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("exp-timeline").classList.toggle("hidden", b.dataset.expview !== "timeline");
    $("exp-graph").classList.toggle("hidden", b.dataset.expview !== "graph");
    renderExperiments();
  });
});

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
  list.innerHTML = "";
  if (!(state.notebooks || []).length) {
    list.innerHTML = '<div class="empty">No notebooks yet. Create one to run experiments as .ipynb — results are held in the notebook.</div>';
    return;
  }
  for (const nb of state.notebooks) {
    const el = document.createElement("div");
    el.className = "nb-item";
    el.innerHTML = `<span class="nb-icon">📓</span>
      <div class="nbinfo">
        <div class="nbname">${esc(nb.name)}</div>
        <div class="nbmeta">${nb.cells} cells · ${nb.code_cells} code · ${esc(nb.source || "project")}${nb.source === "examples" ? " · demo" : ""}</div>
      </div>
      <span class="nb-badge ${nb.executions ? "run" : ""}">${nb.executions ? nb.executions + " runs" : "idle"}</span>`;
    el.addEventListener("click", () => openNotebook(nb.name));
    list.appendChild(el);
  }
}

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
