/* Fox — AI Science Workbench frontend */

const $ = (id) => document.getElementById(id);
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
  const res = await fetch(path, {
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
  const url = `${proto}://${location.host}/ws/projects/${encodeURIComponent(state.project)}`;
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
    case "artifact": addArtifact(p.artifact); renderArtifacts(); break;
    case "approval_request": showApproval(p); break;
    case "review_start": setReviewStatus("Reviewing latest turn…"); break;
    case "review": renderReview(p.findings || []); break;
    case "done": onTurnDone(); break;
    case "error": onError(p.message); break;
  }
}

/* ============================ chat rendering ============================= */

let curAssistantEl = null;

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
      ${a.data_type === "png" ? `<img class="thumb" src="/artifacts/${a.id}" alt="">` : ""}
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
    ? `<img src="/artifacts/${a.id}" alt="">`
    : `<pre>${esc(a.description)}\n\n${esc(a.data_type === "html" ? "(html artifact)" : "")}</pre>`;
  $("art-meta").textContent = `${a.kind} · created ${new Date(a.created_at * 1000).toLocaleString()} · ${a.size} bytes`;
  $("art-code").textContent = a.code || "(no code recorded)";
  $("art-env").textContent = a.env && Object.keys(a.env).length ? JSON.stringify(a.env, null, 2) : "(no env snapshot)";
  $("regen-input").value = "";
  $("regen-status").textContent = "";
  $("artifact-modal").classList.remove("hidden");
}

function renderArtifactInline(art) {
  if (!curAssistantEl) return;
  if (art.data_type !== "png") return;
  const fig = document.createElement("div");
  fig.className = "inline-fig";
  fig.innerHTML = `<img src="/artifacts/${art.id}" alt="${esc(art.name)}" title="${esc(art.description)}">`;
  fig.querySelector("img").addEventListener("click", () => openArtifact(art));
  curAssistantEl.div.insertBefore(fig, null);
  curAssistantEl.div.appendChild(fig);
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
  const c = state.config || { llm: {}, agent: {} };
  $("cfg-base-url").value = c.llm.base_url || "";
  $("cfg-tool-url").value = c.llm.tool_base_url || "";
  $("cfg-model").value = c.llm.model || "";
  $("cfg-temp").value = c.llm.temperature ?? 0.2;
  $("cfg-reviewer").checked = c.agent?.reviewer_enabled !== false;
  const dl = $("model-list");
  dl.innerHTML = (state.models || []).map((m) => `<option value="${esc(m.id)}">`).join("");
  $("settings-modal").classList.remove("hidden");
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
$("settings-close").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
$("cfg-save").addEventListener("click", saveSettings);
$("cfg-test").addEventListener("click", testConnection);
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
  connect();
})();
