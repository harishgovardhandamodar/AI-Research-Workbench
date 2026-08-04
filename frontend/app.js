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

// Rotating work-in-progress phrases shown next to the busy indicator while the
// agent is active, to keep the chat window engaging.
const WIP_PHRASES = [
  "Crunching numbers…",
  "Brewing up an experiment…",
  "Calling the tools…",
  "Talking to the MCP servers…",
  "Finetuning the details…",
  "Polishing the figures…",
  "Reading the paper…",
  "Running the numbers again…",
  "\u201CAll models are wrong, but some are useful.\u201D \u2014 G.E.P. Box",
  "\u201CE pur si muove.\u201D \u2014 Galileo",
  "\u201CThe important thing is not to stop questioning.\u201D \u2014 Einstein",
  "\u201CIt is not knowledge, but the act of learning.\u201D \u2014 Cato",
];
let busyPhraseTimer = null;
const busyPhraseEl = document.createElement("span");
busyPhraseEl.className = "busy-phrase";
busyPhraseEl.title = "Work in progress";

function startBusyPhrases(active) {
  clearInterval(busyPhraseTimer);
  busyPhraseTimer = null;
  if (active) {
    busyPhraseEl.textContent = " · " + WIP_PHRASES[0];
    busyPhraseTimer = setInterval(() => {
      busyPhraseEl.textContent = " · " + WIP_PHRASES[Math.floor(Math.random() * WIP_PHRASES.length)];
    }, 6000);
  } else {
    busyPhraseEl.textContent = "";
  }
}

function setBusyStatus(p) {
  const el = $("busy-status");
  if (!el) return;
  // Legacy plain-message status ("Agent is thinking…", "").
  if (typeof p === "string" || (p && !p.phase)) {
    const txt = typeof p === "string" ? p : p.message || "";
    el.innerHTML = txt ? esc(txt) : "";
    el.appendChild(busyPhraseEl);
    el.classList.toggle("working", !!txt);
    startBusyPhrases(!!txt);
    return;
  }
  // Rich structured status: agent · model · tool · MCP · skills · workflow.
  const parts = [];
  const who = [p.agent, p.model].filter(Boolean).join(" · ");
  if (who) parts.push(`<span class="bs-who">${esc(who)}</span>`);
  if (p.phase === "starting") parts.push(`<span class="bs-chip">starting…</span>`);
  if (p.tool) {
    const m = p.mcp ? `<span class="bs-mcp">MCP ${esc(p.mcp)}</span>` : "";
    parts.push(`<span class="bs-chip bs-tool" title="Tool">${m}<span class="bs-toolname">${esc(p.tool)}</span></span>`);
  }
  if (p.skills && p.skills.length) {
    parts.push(`<span class="bs-chip bs-skill" title="Skills">📚 ${esc(p.skills.map((s) => String(s).replace(/_/g, " ")).join(", "))}</span>`);
  }
  if (p.workflow) parts.push(`<span class="bs-chip bs-wf" title="Workflow">${esc(p.workflow)}</span>`);
  const html = parts.join(" ");
  el.innerHTML = html;
  el.appendChild(busyPhraseEl);
  el.classList.toggle("working", !!html);
  startBusyPhrases(!!html);
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
}

function handleEvent(type, p) {
  switch (type) {
    case "user_message": renderUserMessage(p.content, p.tags, p.created_at, p.experiment_id); break;
    case "stream_delta": streamDelta(p.text); break;
    case "assistant_message": finalizeAssistant(p.content, p.tags, p.created_at, p.experiment_id); break;
    case "tool_start": toolStart(p); break;
    case "tool_result": toolResult(p); break;
    case "artifact": addArtifact(p.artifact); renderArtifacts(); renderArtifactInline(p.artifact); break;
    case "approval_request": showApproval(p); break;
    case "approval_result":
      if (p.decision === "timeout")
        toast("Approval request timed out — command denied.", 5000);
      loadApprovals();
      break;
    case "review_start": setReviewStatus("Reviewing latest turn…"); break;
    case "review":
      state._lastFindings = p.findings || [];
      state._lastSuggestions = p.suggestions || [];
      renderReview(state._lastFindings, state._lastSuggestions);
      break;
    case "notice": toast(p.message, 6000); break;
    case "status": setBusyStatus(p); break;
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

function fmtClock(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

function fmtDay(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts * 1000);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return "Today";
    return d.toLocaleDateString([], {
      month: "short", day: "numeric",
      year: d.getFullYear() === today.getFullYear() ? undefined : "numeric",
    });
  } catch (e) { return ""; }
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard.");
    return;
  } catch (e) { /* fall through to legacy */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); toast("Copied to clipboard."); }
  catch (e) { toast("Copy failed: " + e.message); }
  ta.remove();
}

function msgContainer(role, tags, ts) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const label = document.createElement("div");
  label.className = "msg-label";
  label.innerHTML = `<span class="msg-who">${role === "user" ? "You" : "Fox"}</span>
    <span class="spacer"></span>
    <span class="msg-time">${ts ? fmtClock(ts) : ""}</span>
    <button class="msg-copy" title="Copy message" data-role="${role}">⧉</button>`;
  div.appendChild(label);
  const tagHtml = msgTagsHtml(tags);
  if (tagHtml) div.insertAdjacentHTML("beforeend", tagHtml);
  const body = document.createElement("div");
  body.className = "msg-body";
  div.appendChild(body);
  $("messages").appendChild(div);
  return { div, body };
}

function renderUserMessage(content, tags, ts, expId) {
  const el = msgContainer("user", tags, ts);
  el.body.textContent = content;
  tagMessageExperiment(el, expId);
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

function finalizeAssistant(content, tags, ts, expId) {
  const el = curAssistantEl;
  if (el) {
    el.raw = content || el.raw || "";
    el.body.innerHTML = renderMarkdown(el.raw);
    enhanceCodeBlocks(el.body);
    maybeAttachRepoButtons(el, tags);
    tagMessageExperiment(el, expId);
    if (ts) {
      const t = el.div.querySelector(".msg-time");
      if (t) t.textContent = fmtClock(ts);
    }
    curAssistantEl = null;
  }
  state.streaming = false;
  // Attach figures that were emitted before the text (e.g. auto-workflows).
  while (pendingInlineFigs.length) {
    const art = pendingInlineFigs.shift();
    if (el) appendInlineFig(el, art);
  }
}

function enhanceCodeBlocks(root) {
  if (!root) return;
  root.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(".code-copy")) return;
    const btn = document.createElement("button");
    btn.className = "code-copy";
    btn.textContent = "⧉";
    btn.title = "Copy code";
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const code = pre.querySelector("code");
      copyText((code ? code.textContent : "") || pre.textContent || "");
    });
    pre.appendChild(btn);
  });
}

/* ---- experiment repo: manual commit / push buttons on result messages ---- */

function repoTagsMatch(tags) {
  return /(improve loop|workflow|notebook|report|experiment|rerun|finetune)/i.test((tags || []).join(" "));
}

function maybeAttachRepoButtons(el, tags) {
  if (!el || !repoTagsMatch(tags)) return;
  if (el.div.querySelector(".repo-actions")) return;
  const bar = document.createElement("div");
  bar.className = "repo-actions";
  bar.innerHTML = `
    <button class="btn subtle small repo-commit" title="Commit experiment artifacts to the management repo">Commit</button>
    <button class="btn subtle small repo-push" title="Push the management repo to GitHub">Push</button>
    <span class="muted repo-status"></span>`;
  el.div.appendChild(bar);
  bar.querySelector(".repo-commit").addEventListener("click", () => repoAction("commit", bar));
  bar.querySelector(".repo-push").addEventListener("click", () => repoAction("push", bar));
}

async function repoAction(kind, bar) {
  const st = bar.querySelector(".repo-status");
  st.classList.remove("err");
  st.textContent = kind === "commit" ? "Committing…" : "Pushing…";
  try {
    const endpoint = kind === "commit" ? "commit" : "push";
    const r = await api(`/api/projects/${state.project}/management/${endpoint}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (r.ok) {
      st.textContent = kind === "commit" ? "Committed ✓" : "Pushed ✓";
      if (r.message) st.title = r.message;
    } else {
      st.textContent = r.message || "failed";
      st.classList.add("err");
    }
  } catch (e) {
    st.textContent = "Failed: " + e.message;
    st.classList.add("err");
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
    const wf = r.workflow;
    // A completed pipeline shouldn't resurrect its progress overlay on every
    // page load; only an active one (running / waiting on approval) is shown.
    if (wf && (wf.status === "done" || wf.status === "failed")) {
      $("workflow-panel").classList.add("hidden");
      return;
    }
    renderWorkflow(wf);
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

  const r = $("r-kernel-status");
  if (r) {
    r.innerHTML = "";
    const rstate = (env && env.r) || "unknown";
    const persistent = env && env.r_persistent;
    const avail = rstate === "available";
    const d = document.createElement("div");
    d.className = avail ? "finding info" : "finding warning";
    d.innerHTML = `<span class="sev">${esc(avail ? "available" : "not installed")}</span>` +
      `<span>R runs a fresh Rscript process per call${persistent ? "" : " — state is <b>not persistent</b>"} between calls, unlike the persistent Python kernel. ${avail ? "" : "Install R (Rscript) to enable it."}</span>`;
    r.appendChild(d);
  }
}

function kv(k, val) {
  const d = document.createElement("div");
  d.className = "kv";
  d.innerHTML = `<span class="k">${esc(k)}</span><span class="v">${esc(val)}</span>`;
  return d;
}

/* ============================ review / grants ============================ */

function renderReview(findings, suggestions) {
  const c = $("review-findings");
  c.innerHTML = "";
  const fs = findings || [];
  const ss = suggestions || [];
  if (!fs.length && !ss.length) {
    c.innerHTML = '<div class="empty">No issues flagged. Reviewer runs after each turn.</div>';
    return;
  }
  for (const f of fs) {
    const d = document.createElement("div");
    d.className = `finding ${esc(f.severity || "info")}`;
    d.innerHTML = `<span class="sev">${esc(f.severity)}</span>${esc(f.message)}`;
    c.appendChild(d);
  }
  if (ss.length) {
    const h = document.createElement("div");
    h.className = "finding suggestion";
    h.innerHTML = '<span class="sev">next steps</span>';
    c.appendChild(h);
    for (const s of ss) {
      const d = document.createElement("div");
      d.className = "finding suggestion";
      const title = (typeof s === "object" && s && s.title) ? s.title
        : (typeof s === "string" ? s : "");
      const prompt = (typeof s === "object" && s && s.prompt)
        ? s.prompt : (typeof s === "string" ? s : "");
      const action = (typeof s === "object" && s && s.action)
        ? s.action : "";
      d.innerHTML = `<span class="sev">→</span><span class="sug-body">${esc(title)}` +
        (action && action !== title ? `<span class="sug-action">${esc(action)}</span>` : "") +
        `</span>`;
      if (prompt) {
        const btn = document.createElement("button");
        btn.className = "btn subtle small sug-run";
        btn.textContent = "Apply & rerun";
        btn.addEventListener("click", () => sendChat(prompt, "rerun_suggestion"));
        d.appendChild(btn);
      }
      c.appendChild(d);
    }
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

function renderApprovals(approvals) {
  const c = $("approval-list");
  if (!c) return;
  c.innerHTML = "";
  if (!approvals.length) {
    c.innerHTML = '<div class="empty">No approval decisions recorded yet.</div>';
    return;
  }
  for (const a of approvals) {
    const d = document.createElement("div");
    const sev = a.decision === "allow" ? "ok" : (a.decision === "timeout" ? "warn" : "critical");
    const when = new Date(a.created_at * 1000).toLocaleTimeString();
    d.className = "finding " + sev;
    d.title = a.command;
    d.innerHTML = `<span class="sev">${esc(a.decision)}${a.temporary ? " (temp)" : ""}</span>` +
      `<code>${esc((a.command || "").slice(0, 40))}</code> <span class="muted" style="margin-left:6px">${esc(when)}</span>`;
    c.appendChild(d);
  }
}

async function loadApprovals() {
  try {
    const r = await api(`/api/projects/${state.project}/approvals`);
    renderApprovals(r.approvals || []);
  } catch (e) { /* best-effort */ }
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

function setTurnControls(busy) {
  $("send-btn").disabled = busy;
  $("input").disabled = busy;
  $("stop-btn").classList.toggle("hidden", !busy);
  $("activity").classList.toggle("hidden", !busy);
  clearInterval(state.turnTimer);
  state.turnTimer = null;
  if (busy) {
    state.turnStart = Date.now();
    const tick = () => {
      const s = Math.max(0, Math.round((Date.now() - state.turnStart) / 1000));
      $("activity").textContent = `● generating · ${s}s`;
    };
    tick();
    state.turnTimer = setInterval(tick, 1000);
  }
}

function onTurnDone() {
  setConn("ok");
  state.busy = false;
  setTurnControls(false);
  $("input").focus();
  refreshState();
}

function onError(msg) {
  setConn("ok");
  state.busy = false;
  setTurnControls(false);
  const el = ensureAssistant();
  el.body.innerHTML += `<p style="color:var(--danger)"><strong>Error:</strong> ${esc(msg)}</p>`;
  onTurnDone();
}

async function sendChat(textOverride, intent, extra) {
  const input = $("input");
  const text = textOverride !== undefined ? textOverride : input.value.trim();
  if (!text || state.busy) return;
  if (textOverride !== undefined) {
    input.value = "";
    autoResize(input);
  }
  state.busy = true;
  setTurnControls(true);
  setConn("busy");
  const payload = { type: "chat", content: text };
  if (intent) payload.intent = intent;
  if (extra) Object.assign(payload, extra);
  send(payload);
}

function lastUserMsg() {
  const msgs = $("messages").querySelectorAll(".msg.user .msg-body");
  return msgs.length ? msgs[msgs.length - 1].textContent : "";
}

/* ============================ settings =================================== */

function openSettings() {
  const c = state.config || { llm: {}, agent: {}, mcp: {}, kaggle: {}, management: {} };
  $("cfg-base-url").value = c.llm.base_url || "";
  $("cfg-tool-url").value = c.llm.tool_base_url || "";
  $("cfg-model").value = c.llm.model || "";
  $("cfg-temp").value = c.llm.temperature ?? 0.2;
  $("cfg-reviewer").checked = c.agent?.reviewer_enabled !== false;
  const kg = c.kaggle || {};
  $("cfg-kaggle-user").value = kg.username || "";
  $("cfg-kaggle-key").value = kg.key || "";
  const mgmt = c.management || {};
  $("cfg-mgmt-repo").value = mgmt.repo_dir || "";
  $("cfg-mgmt-github").value = mgmt.github_repo || "";
  $("cfg-mgmt-autocommit").checked = mgmt.auto_commit !== false;
  $("cfg-mgmt-autopush").checked = !!mgmt.auto_push;
  const dl = $("model-list");
  dl.innerHTML = (state.models || []).map((m) => `<option value="${esc(m.id)}">`).join("");
  state.mcpServers = (c.mcp?.servers || []).map((s) => ({ ...s }));
  renderMcpList();
  $("settings-modal").classList.remove("hidden");
  refreshMcpStatus();
  detectMgmtRepos();
}

async function detectMgmtRepos() {
  const dl = $("mgmt-repo-list");
  if (!dl) return;
  try {
    const r = await api("/api/management/repos");
    dl.innerHTML = (r.repos || []).map((x) => `<option value="${esc(x.path)}">`).join("");
  } catch (e) { /* best-effort */ }
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
    kaggle: {
      username: $("cfg-kaggle-user").value.trim(),
      key: $("cfg-kaggle-key").value.trim(),
    },
    management: {
      repo_dir: $("cfg-mgmt-repo").value.trim(),
      github_repo: $("cfg-mgmt-github").value.trim(),
      auto_commit: $("cfg-mgmt-autocommit").checked,
      auto_push: $("cfg-mgmt-autopush").checked,
    },
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
  const sel = $("project-select");
  if (sel) sel.value = name;
  state.artifacts = [];
  $("messages").innerHTML = "";
  curAssistantEl = null;
  state.expDetail = {};
  state.expRanking = {};
  state.activeExperiment = null;
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
    renderReview(state._lastFindings || [], state._lastSuggestions || []);
    renderGrants(r.grants || []);
    refreshExpContext();
  } catch (e) { toast("Failed to load state: " + e.message, 4000); }
  refreshNotebooks();
  loadWorkflow();
  loadFiles();
  loadApprovals();
  loadGraphs();
}

function renderFiles(files) {
  const c = $("file-list");
  if (!c) return;
  c.innerHTML = "";
  if (!files.length) {
    c.innerHTML = '<div class="empty">No project files yet. Upload a CSV, data file or script.</div>';
    return;
  }
  for (const f of files) {
    const d = document.createElement("div");
    d.className = "file-row";
    const kb = f.size > 1024 ? (f.size / 1024).toFixed(1) + " KB" : f.size + " B";
    d.innerHTML = `<a class="file-name" href="${esc(f.url)}" target="_blank" rel="noopener" title="Open">${esc(f.name)}</a>
      <span class="muted file-size">${esc(kb)}</span>
      <button class="btn subtle small file-del" data-name="${esc(f.name)}">✕</button>`;
    d.querySelector(".file-del").addEventListener("click", () => deleteFile(f.name));
    c.appendChild(d);
  }
}

async function loadFiles() {
  try {
    const r = await api(`/api/projects/${state.project}/files`);
    renderFiles(r.files || []);
  } catch (e) { /* files list is best-effort */ }
}

async function uploadFiles() {
  const input = $("file-input");
  const file = input.files && input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("upload", file, file.name);
  try {
    const res = await fetch(B(`/api/projects/${state.project}/files`), {
      method: "POST", body: fd,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    renderFiles(data.files || []);
    input.value = "";
    toast(`Uploaded ${file.name}`);
  } catch (e) { toast("Upload failed: " + e.message, 4000); }
}

async function deleteFile(name) {
  try {
    const r = await api(`/api/projects/${state.project}/files/${encodeURIComponent(name)}`, { method: "DELETE" });
    renderFiles(r.files || []);
    toast(`Deleted ${name}`);
  } catch (e) { toast("Delete failed: " + e.message, 4000); }
}

/* ---- Kaggle dataset import ---- */

function kaggleStatus(msg, kind) {
  const el = $("kaggle-status");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("kaggle-err", kind === "err");
  el.classList.toggle("kaggle-ok", kind === "ok");
}

function toggleKaggleForm() {
  const f = $("kaggle-form");
  const open = f.classList.toggle("hidden");
  if (open) {
    const cfg = state.config || {};
    const kg = cfg.kaggle || {};
    if (!(kg.username && kg.key)) {
      kaggleStatus("Set Kaggle credentials in Settings first.", "err");
    } else {
      kaggleStatus("");
    }
    $("kaggle-slug").focus();
  }
}

async function importKaggle() {
  const slug = ($("kaggle-slug").value || "").trim();
  if (!slug) { kaggleStatus("Enter a dataset slug like 'owner/dataset'.", "err"); return; }
  const btn = $("kaggle-import");
  btn.disabled = true;
  kaggleStatus(`Downloading ${slug}…`, "ok");
  try {
    const r = await api(`/api/projects/${state.project}/kaggle/import`, {
      method: "POST",
      body: JSON.stringify({ dataset: slug }),
    });
    const n = (r.files || []).length;
    kaggleStatus(`Imported ${r.dataset} — ${n} file(s) in ${r.dir}.`, "ok");
    $("kaggle-form").classList.add("hidden");
    $("kaggle-slug").value = "";
    await loadFiles();
    toast(`Imported ${n} file(s) from Kaggle. Ask Fox to analyze data/${slug.split("/")[1]}…`);
  } catch (e) {
    kaggleStatus("Import failed: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function renderGraphs(graphs) {
  const c = $("graph-list");
  if (!c) return;
  c.innerHTML = "";
  if (!graphs.length) {
    c.innerHTML = '<div class="empty">No knowledge graphs yet. Ask the agent to build one from a paper\'s notes.</div>';
    return;
  }
  for (const g of graphs) {
    const d = document.createElement("div");
    d.className = "file-row";
    const s = g.stats || {};
    const detail = `${s.node_count || 0} nodes · ${s.edge_count || 0} edges`;
    d.innerHTML = `<a class="file-name" href="${esc(g.url)}" target="_blank" rel="noopener" title="Open graph JSON">${esc(g.name)}</a>
      <span class="muted file-size">${esc(detail)}</span>
      <button class="btn subtle small graph-view" title="Visualise this knowledge graph">View</button>`;
    d.querySelector(".graph-view").addEventListener("click", () => openGraphViewer(g.name, g.url));
    c.appendChild(d);
  }
}

async function loadGraphs() {
  try {
    const r = await api(`/api/projects/${state.project}/graphs`);
    renderGraphs(r.graphs || []);
  } catch (e) { /* best-effort */ }
}

/* ---------- shared graph pan/zoom + lonewolf zoom controls ---------- */
// VIEW_ZOOM[wrapId] tracks each graph's current viewBox (natural coordinate
// space) so pan/zoom survives re-renders of the SVG inside.
const VIEW_ZOOM = {};

function graphViewCurrent(svg) {
  if (!svg) return null;
  const c = svg.viewBox.baseVal;
  return { x: c.x, y: c.y, w: c.width, h: c.height };
}
function graphViewApply(svg, key, vb) {
  if (!svg) return;
  svg.setAttribute("viewBox", `${vb.x.toFixed(1)} ${vb.y.toFixed(1)} ${vb.w.toFixed(1)} ${vb.h.toFixed(1)}`);
  VIEW_ZOOM[key] = vb;
}
function graphViewReset(svg, key, W, H) {
  graphViewApply(svg, key, { x: 0, y: 0, w: W, h: H });
}
function graphViewZoomAt(svg, key, f, mx, my) {
  const vb = graphViewCurrent(svg);
  if (!vb) return;
  const nw = Math.max(150, Math.min(8000, vb.w * f));
  const nh = Math.max(100, Math.min(6000, vb.h * f));
  const nx = vb.x + (mx == null ? 0.5 : mx) * (vb.w - nw);
  const ny = vb.y + (my == null ? 0.5 : my) * (vb.h - nh);
  graphViewApply(svg, key, { x: nx, y: ny, w: nw, h: nh });
}
function graphViewRestore(svg, key, W, H) {
  const vb = VIEW_ZOOM[key];
  if (vb && (vb.w !== W || vb.h !== H || vb.x || vb.y)) graphViewApply(svg, key, vb);
}
// Pointer-drag pan + wheel zoom (zooms toward the cursor) on a graph wrap.
// `getSvg` resolves the <svg> (it may be rebuilt by re-renders); `skipSel` is a
// selector for interactive elements (nodes) that should not start a drag.
function attachGraphPan(wrap, key, getSvg, skipSel) {
  const st = { drag: null };
  wrap.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest(".graph-controls")) return;
    if (skipSel && e.target.closest && e.target.closest(skipSel)) return;
    const vb = graphViewCurrent(getSvg());
    if (!vb) return;
    st.drag = { sx: e.clientX, sy: e.clientY, vx: vb.x, vy: vb.y, moved: false };
    try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
    wrap.style.cursor = "grabbing";
  });
  wrap.addEventListener("pointermove", (e) => {
    const d = st.drag;
    if (!d) return;
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    d.moved = true;
    const svg = getSvg();
    const vb = graphViewCurrent(svg);
    if (vb) graphViewApply(svg, key, { x: d.vx - dx, y: d.vy - dy, w: vb.w, h: vb.h });
  });
  const end = () => { st.drag = null; wrap.style.cursor = ""; };
  wrap.addEventListener("pointerup", end);
  wrap.addEventListener("pointercancel", end);
  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    const svg = getSvg();
    if (!svg) return;
    const rect = wrap.getBoundingClientRect();
    graphViewZoomAt(svg, key, e.deltaY < 0 ? 1.15 : 0.87,
      (e.clientX - rect.left) / rect.width, (e.clientY - rect.top) / rect.height);
  }, { passive: false });
  return st;
}
// Lonewolf-style floating zoom controls (+, −, reset), bottom-left of the wrap.
// Safe to call repeatedly: re-creates the buttons if a re-render wiped them.
function attachGraphControls(wrap, key, getSvg, W, H) {
  let ctrl = wrap.querySelector(".graph-controls");
  if (!ctrl) {
    ctrl = document.createElement("div");
    ctrl.className = "graph-controls";
    const mk = (label, title, fn) => {
      const b = document.createElement("button");
      b.textContent = label; b.title = title;
      b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
      ctrl.appendChild(b);
    };
    mk("+", "Zoom In", () => graphViewZoomAt(getSvg(), key, 1.3));
    mk("−", "Zoom Out", () => graphViewZoomAt(getSvg(), key, 0.7));
    mk("⊙", "Reset View", () => graphViewReset(getSvg(), key, W, H));
    wrap.appendChild(ctrl);
  }
  return ctrl;
}

/* ===================== knowledge graph viewer ===================== */

const GRAPH_TYPE_COLORS = {
  Paper: "#a974ff",
  Author: "#c9a8ff",
  Method: "#8b5cf6",
  Dataset: "#6d4fc0",
  Metric: "#b98cff",
  Experiment: "#7a5cc0",
  Claim: "#9f7be8",
};

const GRAPH_STATE = {
  gName: "", url: "", nodes: [], edges: [], byId: {}, typeOn: {},
  sel: null, zoom: 1, panX: 0, panY: 0,
  vb: null, drag: null,
};

function graphNodeLabel(n) {
  if (n && (n.name || n.title)) return String(n.name || n.title);
  if (n && n.id) {
    const base = String(n.id);
    const idx = base.indexOf(":");
    return idx >= 0 ? base.slice(idx + 1) : base;
  }
  return "node";
}

function graphNodeColor(type) {
  return GRAPH_TYPE_COLORS[type] || "#6b4fb0";
}

function graphNormalize(g) {
  const nodes = (g.nodes || []).map((n) => ({ ...n, degree: 0 }));
  const edges = (g.edges || [])
    .filter((e) => e && e.source != null && e.target != null)
    .map((e) => ({ source: e.source, target: e.target, relation: e.relation || "" }));
  const byId = {};
  for (const n of nodes) byId[n.id] = n;
  for (const e of edges) {
    if (byId[e.source]) byId[e.source].degree += 1;
    if (byId[e.target]) byId[e.target].degree += 1;
  }
  return { nodes, edges, byId };
}

// Deterministic layout so a graph renders identically on every open.
function graphLayout(nodes, edges, w, h, byId, iters = 400) {
  const n = nodes.length;
  if (!n) return;
  let seed = 7;
  const rnd = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };
  const k = Math.sqrt((w * h) / Math.max(1, n)) * 1.4;
  nodes.forEach((nd, i) => {
    const a = (i / Math.max(1, n)) * Math.PI * 2;
    const rad = Math.min(w, h) * 0.32;
    nd.x = w / 2 + rad * Math.cos(a) + (rnd() - 0.5) * 20;
    nd.y = h / 2 + rad * Math.sin(a) + (rnd() - 0.5) * 20;
    nd.vx = 0; nd.vy = 0;
  });
  for (let it = 0; it < iters; it++) {
    const temp = Math.max(0.04, 0.8 * (1 - it / iters));
    // repulsion between every pair
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = rnd() - 0.5; dy = rnd() - 0.5; d2 = 0.01; }
        const f = (k * k) / d2;
        const fx = dx / Math.sqrt(d2) * f;
        const fy = dy / Math.sqrt(d2) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    // spring attraction along edges
    for (const e of edges) {
      const a = byId[e.source], b = byId[e.target];
      if (!a || !b || a === b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d * d) / k;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
    }
    // gravity to keep the layout centred
    for (const nd of nodes) {
      nd.vx += (w / 2 - nd.x) * 0.008;
      nd.vy += (h / 2 - nd.y) * 0.008;
    }
    // integrate + clamp velocity
    for (const nd of nodes) {
      const vmax = temp * 2.2;
      const sp = Math.sqrt(nd.vx * nd.vx + nd.vy * nd.vy) || 1;
      const s = sp > vmax ? vmax / sp : 1;
      nd.x += nd.vx * s; nd.y += nd.vy * s;
      nd.vx = 0; nd.vy = 0;
    }
  }
}

function graphRerender() {
  const svg = $("graph-svg");
  if (!svg || !GRAPH_STATE.nodes.length) return;
  const wrap = $("graph-svg-wrap");
  const W = 960, H = 520;
  const visible = GRAPH_STATE.nodes.filter((n) => GRAPH_STATE.typeOn[n.type] !== false);
  const vset = new Set(visible.map((n) => n.id));
  const edges = GRAPH_STATE.edges.filter((e) => vset.has(e.source) && vset.has(e.target));
  graphLayout(visible, edges, W, H, GRAPH_STATE.byId);

  const showLabels = visible.length <= 90;
  const radius = (d) => Math.min(22, 7 + Math.sqrt(d.degree || 0) * 4);
  const sel = GRAPH_STATE.sel;
  const selId = sel ? sel.id : null;

  let s = `<defs><marker id="gx-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#463a66"/></marker></defs>`;
  for (const e of edges) {
    const a = GRAPH_STATE.byId[e.source], b = GRAPH_STATE.byId[e.target];
    if (!a || !b) continue;
    const isSel = selId === e.source || selId === e.target;
    const cls = isSel ? "gx-edge sel" : "gx-edge";
    s += `<line class="${cls}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" marker-end="url(#gx-arrow)"><title>${esc(a.id)} —${esc(e.relation)}→ ${esc(b.id)}</title></line>`;
  }
  const byId = GRAPH_STATE.byId;
  for (const n of visible) {
    const r = radius(n);
    const isSel = selId === n.id;
    const near = isSel || (sel && (sel.in.some((x) => x.id === n.id) || sel.out.some((x) => x.id === n.id)));
    const cls = "gx-node" + (isSel ? " sel" : near ? " near" : "");
    s += `<g class="${cls}" data-node="${esc(n.id)}" transform="translate(${n.x.toFixed(1)},${n.y.toFixed(1)})">
      <circle r="${r}" fill="${graphNodeColor(n.type)}" stroke="rgba(255,255,255,.14)" stroke-width="1">
        <title>${esc(n.type)}: ${esc(graphNodeLabel(n))}</title>
      </circle>
      ${showLabels ? `<text class="gx-label" y="${(r + 12).toFixed(1)}" text-anchor="middle">${esc(graphNodeLabel(n))}</text>` : ""}
    </g>`;
  }
  if (!showLabels) {
    s += `<text class="gx-hint" x="8" y="16">Hover a node to see its label.</text>`;
  }
  const vb = VIEW_ZOOM["graph-svg-wrap"] || { x: 0, y: 0, w: W, h: H };
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  svg.innerHTML = s;
}

function graphSelect(id) {
  if (!id || !GRAPH_STATE.byId[id]) { GRAPH_STATE.sel = null; }
  else {
    const node = GRAPH_STATE.byId[id];
    GRAPH_STATE.sel = {
      id: node.id, node,
      out: GRAPH_STATE.edges.filter((e) => e.source === node.id)
        .map((e) => ({ id: e.target, relation: e.relation })),
      in: GRAPH_STATE.edges.filter((e) => e.target === node.id)
        .map((e) => ({ id: e.source, relation: e.relation })),
    };
  }
  graphRerender();
  graphRenderDetail();
}

function graphRenderDetail() {
  const pane = $("graph-detail");
  if (!pane) return;
  const sel = GRAPH_STATE.sel;
  if (!sel) {
    pane.innerHTML = '<div class="gx-detail-empty">Select a node to inspect it and its connections.</div>';
    return;
  }
  const n = sel.node;
  let props = "";
  for (const [k, v] of Object.entries(n)) {
    if (k === "id" || k === "type" || k === "degree" || v == null || v === "") continue;
    props += `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`;
  }
  const conn = (list, dir) => list.map((c) => {
    const other = GRAPH_STATE.byId[c.id];
    return `<button class="gx-conn" data-node="${esc(c.id)}">
      <span class="gx-conn-rel">${esc(dir)} ${esc(c.relation)}</span>
      <span class="gx-conn-node" style="color:${graphNodeColor(other ? other.type : "Other")}">${esc(graphNodeLabel(other))}</span>
    </button>`;
  }).join("");
  pane.innerHTML = `
    <div class="gx-detail-title" style="color:${graphNodeColor(n.type)}">${esc(n.type)}</div>
    <div class="gx-detail-name">${esc(graphNodeLabel(n))}</div>
    <div class="gx-detail-id">${esc(n.id)}</div>
    ${props ? `<div class="gx-props">${props}</div>` : ""}
    ${sel.out.length ? `<div class="gx-conn-head">Outgoing (${sel.out.length})</div>${conn(sel.out, "→")}` : ""}
    ${sel.in.length ? `<div class="gx-conn-head">Incoming (${sel.in.length})</div>${conn(sel.in, "←")}` : ""}`;
  pane.querySelectorAll("[data-node]").forEach((el) => {
    el.addEventListener("click", () => graphSelect(el.dataset.node));
  });
}

function renderGraphLegend() {
  const legend = $("graph-legend");
  if (!legend) return;
  const types = [...new Set(GRAPH_STATE.nodes.map((n) => n.type))].sort();
  legend.innerHTML = "";
  const chip = (t) => {
    const b = document.createElement("button");
    b.className = "graph-chip" + (GRAPH_STATE.typeOn[t] === false ? " off" : "");
    b.style.setProperty("--tcolor", graphNodeColor(t));
    b.textContent = t;
    b.addEventListener("click", () => {
      GRAPH_STATE.typeOn[t] = GRAPH_STATE.typeOn[t] === false ? true : false;
      renderGraphLegend();
      graphRerender();
    });
    return b;
  };
  types.forEach((t) => legend.appendChild(chip(t)));
  const stats = $("graph-stats");
  if (stats) {
    const vis = GRAPH_STATE.nodes.filter((n) => GRAPH_STATE.typeOn[n.type] !== false).length;
    stats.textContent = `${GRAPH_STATE.nodes.length} nodes · ${GRAPH_STATE.edges.length} edges · showing ${vis}`;
  }
}

function renderGraphViewer(g) {
  const norm = graphNormalize(g);
  GRAPH_STATE.nodes = norm.nodes;
  GRAPH_STATE.edges = norm.edges;
  GRAPH_STATE.byId = norm.byId;
  GRAPH_STATE.sel = null;
  GRAPH_STATE.zoom = 1; GRAPH_STATE.panX = 0; GRAPH_STATE.panY = 0;
  GRAPH_STATE.vb = null; GRAPH_STATE.drag = null;
  VIEW_ZOOM["graph-svg-wrap"] = null;
  GRAPH_STATE.typeOn = {};
  for (const n of GRAPH_STATE.nodes) GRAPH_STATE.typeOn[n.type] = true;
  renderGraphLegend();
  graphRerender();
}

function openGraphViewer(name, url) {
  graphSetMinimized(false);
  $("graph-modal").classList.remove("hidden");
  $("graph-title").textContent = "Knowledge graph — " + String(name).replace(/\.json$/, "");
  GRAPH_STATE.gName = name;
  GRAPH_STATE.url = url;
  $("graph-detail").innerHTML = '<div class="empty">Loading graph…</div>';
  $("graph-svg").innerHTML = "";
  api(url).then((r) => {
    renderGraphViewer(r.graph || r);
  }).catch((e) => {
    $("graph-detail").innerHTML = '<div class="empty">Failed to load graph: ' + esc(e.message) + "</div>";
  });
}

$("graph-close").addEventListener("click", () => $("graph-modal").classList.add("hidden"));
$("graph-modal").addEventListener("click", (e) => { if (e.target === $("graph-modal")) $("graph-modal").classList.add("hidden"); });
$("graph-minimize").addEventListener("click", () => graphSetMinimized(!$("graph-modal").classList.contains("minimized")));
function graphSetMinimized(min) {
  $("graph-modal").classList.toggle("minimized", min);
  const b = $("graph-minimize");
  b.textContent = min ? "▔" : "▁";
  b.title = min ? "Expand" : "Minimize";
}
$("graph-export").addEventListener("click", () => {
  if (GRAPH_STATE.url) window.open(B(GRAPH_STATE.url), "_blank", "noopener");
});
$("graph-relayout").addEventListener("click", () => graphRerender());
$("graph-svg").addEventListener("click", (e) => {
  if (graphPan.drag && graphPan.drag.moved) { graphPan.drag.moved = false; return; }
  const el = e.target.closest ? e.target.closest("[data-node]") : null;
  if (el) graphSelect(el.dataset.node);
  else graphSelect(null);
});

const graphWrap = $("graph-svg-wrap");
const graphPan = attachGraphPan(graphWrap, "graph-svg-wrap", () => $("graph-svg"), "[data-node]");
attachGraphControls(graphWrap, "graph-svg-wrap", () => $("graph-svg"), 960, 520);

function renderMessages(msgs) {
  const wrap = $("messages");
  wrap.innerHTML = "";
  let lastDay = "";
  let turnUser = "";
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    const day = fmtDay(m.created_at);
    if (day && day !== lastDay) {
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.textContent = day;
      wrap.appendChild(sep);
      lastDay = day;
    }
    const mtags = (m.meta && m.meta.tags) || [];
    if (m.role === "user") {
      const el = msgContainer("user", mtags, m.created_at);
      el.body.textContent = m.content;
      tagMessageExperiment(el, m.meta && m.meta.experiment_id);
      turnUser = m.id;
    } else if (m.role === "assistant") {
      const el = msgContainer("assistant", mtags, m.created_at);
      el.body.innerHTML = renderMarkdown(m.content);
      enhanceCodeBlocks(el.body);
      maybeAttachRepoButtons(el, mtags);
      tagMessageExperiment(el, m.meta && m.meta.experiment_id);
      // Re-attach figures produced during this turn (artifacts are linked to the
      // turn's user message id) to the final assistant reply of the turn, so
      // charts survive refreshState() re-renders after execution.
      const next = msgs[i + 1];
      const isFinal = !next || next.role === "user";
      if (isFinal && turnUser) attachTurnArtifacts(turnUser, el.div);
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

function attachTurnArtifacts(turnUserMsgId, div) {
  const arts = (state.artifacts || []).filter((a) =>
    a.data_type === "png" && String(a.message_id) === String(turnUserMsgId));
  for (const a of arts) appendInlineFig({ div }, a);
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
document.querySelectorAll(".quick").forEach((b) =>
  b.addEventListener("click", () =>
    sendChat(b.dataset.text || "", b.dataset.intent || "")));
// Clicking a figure rendered inside a chat message opens its artifact modal.
$("messages").addEventListener("click", (e) => {
  const img = e.target.closest("img.chat-fig");
  if (!img || !img.dataset.artId) return;
  const art = (state.artifacts || []).find((a) => a.id === img.dataset.artId);
  if (art) openArtifact(art);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  else if (e.key === "ArrowUp" && !e.shiftKey && e.target.selectionStart === 0 && !e.altKey) {
    // Edit & resend: pull your last message into the composer.
    e.preventDefault();
    const last = lastUserMsg();
    if (last) {
      e.target.value = last;
      e.target.setSelectionRange(last.length, last.length);
      autoResize(e.target);
      toast("Editing your last message — press Enter to resend.");
    }
  }
});
$("input").addEventListener("input", (e) => autoResize(e.target));
$("stop-btn").addEventListener("click", () => {
  send({ type: "stop" });
  toast("Stopping…");
});
// Copy a message's text from its ⧉ button.
$("messages").addEventListener("click", (e) => {
  const btn = e.target.closest(".msg-copy");
  if (!btn) return;
  const msg = btn.closest(".msg");
  const body = msg && msg.querySelector(".msg-body");
  copyText(body ? body.innerText : "");
});
// Clicking an experiment chip on a message focuses that experiment.
$("messages").addEventListener("click", (e) => {
  const chip = e.target.closest(".msg-exp");
  if (!chip) return;
  const eid = parseInt(chip.dataset.eid, 10);
  if (!eid) return;
  focusExperiment(eid);
});
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
$("fork-project-btn").addEventListener("click", async () => {
  const name = prompt("Fork '" + state.project + "' as (new project name):", state.project + "-fork");
  if (!name) return;
  try {
    const r = await api(`/api/projects/${encodeURIComponent(state.project)}/fork`,
                        { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects();
    await switchProject(r.name);
    toast("Forked project as '" + r.name + "'");
  } catch (e) { toast(e.message); }
});
$("delete-project-btn").addEventListener("click", async () => {
  if (!confirm(`Delete project '${state.project}'? This removes its messages, runs, artifacts and files.`)) return;
  try {
    await api(`/api/projects/${encodeURIComponent(state.project)}`, { method: "DELETE" });
    await loadProjects();
    await switchProject(state.projects.length ? state.projects[0].name : "default");
    toast("Project deleted");
  } catch (e) { toast(e.message); }
});
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
$("mgmt-detect").addEventListener("click", () => {
  const dl = $("mgmt-repo-list");
  if (!dl || !dl.options.length) { detectMgmtRepos(); toast("No sibling git repos found."); return; }
  const pick = dl.options[0].value;
  $("cfg-mgmt-repo").value = pick;
  toast("Set to " + pick);
});
$("mgmt-link").addEventListener("click", async () => {
  const gh = ($("cfg-mgmt-github").value || "").trim();
  if (!gh) { toast("Enter a GitHub repo (owner/repo).", 4000); return; }
  const status = $("mgmt-gh-status");
  status.textContent = "Linking…";
  try {
    const r = await api("/api/management/link", {
      method: "POST",
      body: JSON.stringify({ github_repo: gh }),
    });
    if (r.ok) {
      status.textContent = (r.remote ? "origin → " + r.remote : "") + (r.message ? " · " + r.message : "");
      status.classList.remove("kaggle-err");
      status.classList.add("kaggle-ok");
      toast("Linked GitHub repo — auto-commits will be pushed there.");
    } else {
      status.textContent = r.message || "link failed";
      status.classList.remove("kaggle-ok");
      status.classList.add("kaggle-err");
    }
  } catch (e) {
    status.textContent = "Failed to link: " + e.message;
    status.classList.remove("kaggle-ok");
    status.classList.add("kaggle-err");
  }
});

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
    if (t.dataset.tab === "files") { loadFiles(); loadGraphs(); }
    if (t.dataset.tab === "grants") loadApprovals();
  });
});
$("file-upload-btn").addEventListener("click", uploadFiles);
$("files-refresh").addEventListener("click", loadFiles);
$("kaggle-toggle").addEventListener("click", toggleKaggleForm);
$("kaggle-import").addEventListener("click", importKaggle);
$("kaggle-slug").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); importKaggle(); }
});

/* ============================ experiments ================================= */

async function loadExperiments() {
  try {
    const r = await api(`/api/projects/${state.project}/experiments/history`);
    state.expRuns = r.experiments || [];
    const g = await api(`/api/projects/${state.project}/experiments/graph`);
    state.expGraph = g;
    populateExpMetrics();
    renderExperiments();
  } catch (e) { /* silent */ }
  try {
    const rr = await api(`/api/projects/${state.project}/experiments`);
    state.expList = rr.experiments || [];
    renderExpList();
  } catch (e) { state.expList = state.expList || []; }
  await loadExpRankings();
  try {
    const rr = await api(`/api/projects/${state.project}/runs`);
    state.agentRuns = rr.runs || [];
  } catch (e) { state.agentRuns = state.agentRuns || []; }
  for (const r of state.agentRuns.slice().reverse()) {
    const rev = r.review || {};
    if ((rev.findings || []).length || (rev.suggestions || []).length) {
      state._lastFindings = rev.findings || [];
      state._lastSuggestions = rev.suggestions || [];
      renderReview(state._lastFindings, state._lastSuggestions);
      break;
    }
  }
  populateExpCompare();
  renderRuns();
  loadGoals();
  refreshExpContext();
}

async function loadExpRankings() {
  const exps = state.expList || [];
  state.expRanking = state.expRanking || {};
  await Promise.all(exps.map(async (e) => {
    try {
      const r = await api(`/api/projects/${state.project}/experiments/${e.id}/ranking`);
      state.expRanking[e.id] = r.ranking || null;
    } catch (err) { state.expRanking[e.id] = null; }
  }));
  renderExpRankings();
}

function renderExpRankings() {
  const el = $("exp-list");
  if (!el) return;
  (state.expList || []).forEach((e) => {
    const rank = state.expRanking && state.expRanking[e.id];
    if (!rank) return;
    const host = el.querySelector(`.exp-card[data-id="${e.id}"] .exp-rank-host`);
    if (!host) return;
    const rows = rank.rows || [];
    const head = rank.metric
      ? `<summary class="exp-rank-sum">Leaderboard — <b>${esc(rank.metric.replace(/_/g, " "))}</b> ${rank.higher_better ? "↑" : "↓"} (best ${_fmtNum(rank.best)})</summary>`
      : `<summary class="exp-rank-sum">Leaderboard — no numeric metric yet</summary>`;
    if (!rows.length) {
      host.innerHTML = `<details class="exp-rank">${head}<div class="exp-rank-body empty">No runs report the metric "${esc(rank.metric)}".</div></details>`;
      return;
    }
    let html = `<table class="exp-rank-table"><thead><tr><th>#</th><th>run</th><th>${esc(rank.metric.replace(/_/g, " "))}</th><th>Δ best</th></tr></thead><tbody>`;
    for (const row of rows) {
      const medal = row.rank === 1 ? " 🏆" : "";
      html += `<tr${row.rank === 1 ? ' class="rank-top"' : ""}>
        <td class="rank-pos">${row.rank}${medal}</td>
        <td>${esc(row.label || "#" + row.run_id)}</td>
        <td>${_fmtNum(row.metric)}</td>
        <td class="muted">${row.rank === 1 ? "—" : (row.delta_best >= 0 ? "+" : "") + _fmtNum(row.delta_best)}</td>
      </tr>`;
    }
    html += "</tbody></table>";
    host.innerHTML = `<details class="exp-rank">${head}<div class="exp-rank-body">${html}</div></details>`;
  });
}

/* ============ chat-window experiment navigation (context bar) ============= */

function expName(eid) {
  const e = (state.expList || []).find((x) => String(x.id) === String(eid));
  return e ? e.name : (eid != null ? "#" + eid : "");
}

async function loadExpDetail(eid) {
  if (state.expDetail && state.expDetail[eid]) return state.expDetail[eid];
  try {
    const r = await api(`/api/projects/${state.project}/experiments/${eid}`);
    state.expDetail = state.expDetail || {};
    state.expDetail[eid] = r.experiment || {};
    return state.expDetail[eid];
  } catch (e) { return { id: eid }; }
}

function detectActiveExperiment() {
  const exps = state.expList || [];
  if (!exps.length) { state.activeExperiment = null; return null; }
  // Prefer the experiment most recently referenced in the chat (message meta).
  const msgs = $("messages");
  if (msgs) {
    const refs = msgs.querySelectorAll(".msg[data-exp-id]");
    for (let i = refs.length - 1; i >= 0; i--) {
      const eid = parseInt(refs[i].dataset.expId, 10);
      if (eid && exps.some((x) => x.id === eid)) {
        state.activeExperiment = eid;
        return eid;
      }
    }
  }
  // Otherwise the busiest experiment (most runs), tie-break by newest.
  const pick = exps.slice().sort((a, b) =>
    (b.runs - a.runs) || (b.id - a.id))[0];
  state.activeExperiment = pick.id;
  return pick.id;
}

async function refreshExpContext() {
  if (!state.expList || !state.expList.length) {
    state.activeExperiment = null;
    $("exp-context").classList.add("hidden");
    return;
  }
  const eid = detectActiveExperiment();
  await renderExpContext();
  if (eid == null) $("exp-context").classList.add("hidden");
}

function ecBestRun(exp, runs) {
  if (!exp || !exp.goal_metric || !runs || !runs.length) return null;
  const higher = exp.higher_better !== false;
  let best = null;
  for (const r of runs) {
    const m = r.metrics && r.metrics[exp.goal_metric];
    if (m == null) continue;
    if (best === null || (higher ? m > best.v : m < best.v)) best = { v: m, run: r };
  }
  return best;
}

function ecProgress(exp, best) {
  if (!exp || !exp.goal_metric || exp.goal_target == null || !best) return 0;
  const target = Number(exp.goal_target);
  if (!target) return 0;
  const higher = exp.higher_better !== false;
  const ratio = higher ? best.v / target : target / best.v;
  return Math.max(0, Math.min(100, ratio * 100));
}

async function renderExpContext() {
  const ctx = $("exp-context");
  if (!ctx) return;
  const exps = state.expList || [];
  if (!exps.length) { ctx.classList.add("hidden"); return; }
  const sel = $("ec-select");
  sel.innerHTML = exps.map((e) =>
    `<option value="${e.id}"${e.id === state.activeExperiment ? " selected" : ""}>${esc(e.name)}</option>`).join("");
  const eid = state.activeExperiment != null ? state.activeExperiment : (exps[0].id);
  const detail = await loadExpDetail(eid);
  const exp = detail.id != null ? detail : exps.find((e) => e.id === eid);
  const runs = detail.runs || [];
  const best = ecBestRun(exp, runs);

  $("ec-status").textContent = (exp && exp.status) || "active";
  $("ec-status").className = "exp-badge " +
    ((exp && exp.status === "completed") ? "ok" : (exp && exp.status === "cancelled") ? "warn" : "det");

  const goal = exp && exp.goal_metric
    ? `goal ${exp.goal_metric} ${exp.higher_better !== false ? "↑" : "↓"} ${exp.goal_target != null ? _fmtNum(exp.goal_target) : "—"}`
    : "no goal metric";
  $("ec-goal").textContent = goal;

  const bestTxt = best
    ? `best ${_fmtNum(best.v)}${best.run.id != null ? " (run #" + best.run.id + ")" : ""}`
    : "no runs yet";
  $("ec-best").textContent = bestTxt;
  $("ec-fill").style.width = ecProgress(exp, best) + "%";

  // Run chips: click to jump to the improve-loop summary for this experiment.
  const rc = $("ec-runs");
  rc.innerHTML = "";
  if (runs.length) {
    runs.slice().reverse().forEach((r) => {
      const chip = document.createElement("button");
      chip.className = "ec-run-chip";
      const m = exp && exp.goal_metric ? (r.metrics && r.metrics[exp.goal_metric]) : null;
      chip.textContent = `#${r.id}${r.label ? " " + r.label : ""}` +
        (m != null ? " " + _fmtNum(m) : "");
      chip.title = "Jump to this run's message";
      chip.dataset.runId = r.id;
      chip.addEventListener("click", () => jumpToExpMessage(eid, r.id));
      rc.appendChild(chip);
    });
  } else {
    rc.innerHTML = '<span class="muted">No runs yet — ask the agent to improve it, or create a run.</span>';
  }
  ctx.classList.remove("hidden");
}

function tagMessageExperiment(el, expId) {
  if (!el || expId == null) return;
  el.div.dataset.expId = expId;
  const label = el.div.querySelector(".msg-label");
  if (!label || label.querySelector(".msg-exp")) return;
  const chip = document.createElement("button");
  chip.className = "msg-exp";
  chip.textContent = "⚗ " + expName(expId);
  chip.title = "Focus this experiment";
  chip.dataset.eid = expId;
  label.insertBefore(chip, label.firstChild);
}

function expMessages(eid) {
  const msgs = $("messages");
  if (!msgs) return [];
  const all = Array.from(msgs.querySelectorAll(".msg"));
  return all.filter((el) => String(el.dataset.expId) === String(eid));
}

function jumpToExpMessage(eid, runId) {
  let targets = expMessages(eid);
  if (runId != null) {
    const tagged = targets.find((el) =>
      el.textContent.indexOf("run #" + runId) >= 0 || el.textContent.indexOf("#" + runId) >= 0);
    if (tagged) targets = [tagged];
  }
  if (!targets.length) {
    focusExperiment(eid);
    return;
  }
  const el = targets[targets.length - 1];
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.add("exp-flash");
  setTimeout(() => el.classList.remove("exp-flash"), 1600);
}

function jumpExpMessage(dir) {
  const eid = state.activeExperiment;
  if (eid == null) return;
  const msgs = expMessages(eid);
  if (!msgs.length) return;
  const view = $("messages");
  const midY = view ? view.getBoundingClientRect().top + view.clientHeight / 2 : 0;
  let pick = dir < 0 ? msgs[0] : msgs[msgs.length - 1];
  let bestDist = Infinity;
  for (const el of msgs) {
    const d = el.getBoundingClientRect().top - midY;
    if (dir < 0 ? (d < 0 && midY - el.getBoundingClientRect().bottom < bestDist)
                : (d > 0 && d < bestDist)) {
      pick = el;
      bestDist = dir < 0 ? midY - el.getBoundingClientRect().bottom : d;
    }
  }
  pick.scrollIntoView({ behavior: "smooth", block: "start" });
  pick.classList.add("exp-flash");
  setTimeout(() => pick.classList.remove("exp-flash"), 1600);
}

async function focusExperiment(eid) {
  state.activeExperiment = eid;
  await renderExpContext();
}

async function ecManagementAction(kind) {
  const btn = kind === "commit" ? $("ec-commit") : $("ec-push");
  const orig = btn.textContent;
  btn.textContent = kind === "commit" ? "Committing…" : "Pushing…";
  btn.disabled = true;
  try {
    const endpoint = kind === "commit" ? "commit" : "push";
    const r = await api(`/api/projects/${state.project}/management/${endpoint}`, {
      method: "POST", body: JSON.stringify({}),
    });
    toast(r.ok ? (kind === "commit" ? "Committed ✓" : "Pushed ✓") : (r.message || "failed"), 4000);
  } catch (e) {
    toast("Failed: " + e.message, 4000);
  }
  btn.textContent = orig;
  btn.disabled = false;
}

$("ec-select").addEventListener("change", (e) => focusExperiment(parseInt(e.target.value, 10)));
$("ec-prev").addEventListener("click", () => jumpExpMessage(-1));
$("ec-next").addEventListener("click", () => jumpExpMessage(1));
$("ec-improve").addEventListener("click", () => {
  const eid = state.activeExperiment;
  if (eid == null) return;
  const name = expName(eid);
  sendChat(`Improve the experiment "${name}" toward its goal.`, "improve_loop", { experiment_id: eid });
});
$("ec-commit").addEventListener("click", () => ecManagementAction("commit"));
$("ec-push").addEventListener("click", () => ecManagementAction("push"));

function renderRuns() {
  const el = $("runs-list");
  if (!el) return;
  const runs = state.agentRuns || [];
  if (!runs.length) {
    el.innerHTML = '<div class="empty">No agent runs yet in this project.</div>';
    return;
  }
  el.innerHTML = "";
  for (const r of runs.slice().reverse()) {
    const d = document.createElement("div");
    const rev = r.review || {};
    const nf = (rev.findings || []).length;
    const ns = (rev.suggestions || []).length;
    const meta = [r.status];
    if (r.metrics && Object.keys(r.metrics).length) meta.push(Object.keys(r.metrics).length + " metric(s)");
    if (nf) meta.push(nf + " finding(s)");
    if (ns) meta.push(ns + " suggestion(s)");
    d.className = "run-row";
    const lbl = r.label ? `<span class="run-label">${esc(r.label)}</span> ` : "";
    d.innerHTML = `<span class="run-id">#${r.id}</span>
      <span class="run-prompt">${lbl}${esc((r.prompt || "").slice(0, 80))}</span>
      <span class="run-meta muted">${esc(meta.join(" · "))}</span>
      <button class="btn subtle small run-report" data-id="${r.id}">Report</button>`;
    el.appendChild(d);
  }
  el.querySelectorAll(".run-report").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      b.textContent = "…";
      try {
        const r = await api(`/api/projects/${state.project}/runs/${b.dataset.id}/report`, { method: "POST" });
        toast(`Report for run #${b.dataset.id} added to chat (artifact ${r.artifact_id.slice(0, 8)}).`);
        await refreshState();
      } catch (e) { toast("Failed to generate report: " + e.message); }
      b.disabled = false;
      b.textContent = "Report";
    }));
}

async function loadGoals() {
  try {
    state.goals = (await api(`/api/projects/${state.project}/goals`)).goals || [];
  } catch (e) { state.goals = state.goals || []; }
  const sel = $("goal-experiment");
  if (sel) {
    const cur = sel.value || "";
    sel.innerHTML = '<option value="">all runs (project-wide)</option>' +
      (state.expList || []).map((e) =>
        `<option value="${e.id}"${String(e.id) === cur ? " selected" : ""}>${esc(e.name)}</option>`).join("");
  }
  const el = $("goal-list");
  if (!el) return;
  el.innerHTML = "";
  if (!state.goals.length) {
    el.innerHTML = '<div class="empty">No goals yet — add a target metric and the workbench will flag new bests and progress on each run.</div>';
    return;
  }
  const expName = (id) => {
    if (id == null) return "all runs";
    const e = (state.expList || []).find((x) => x.id === id);
    return e ? e.name : "experiment #" + id;
  };
  for (const g of state.goals) {
    const d = document.createElement("div");
    d.className = "goal-chip";
    d.innerHTML = `<b>${esc(g.label || g.metric)}</b>
      <span class="muted">${esc(g.metric)} ${g.higher_better ? "↑" : "↓"} target ${g.target} · ${esc(expName(g.experiment_id))}</span>
      <button class="goal-del" data-metric="${esc(g.metric)}" data-eid="${g.experiment_id ?? ""}" title="remove">✕</button>`;
    el.appendChild(d);
  }
  el.querySelectorAll(".goal-del").forEach((b) =>
    b.addEventListener("click", async () => {
      const q = b.dataset.eid ? `?experiment_id=${b.dataset.eid}` : "";
      try {
        await api(`/api/projects/${state.project}/goals/${encodeURIComponent(b.dataset.metric)}${q}`, { method: "DELETE" });
        loadGoals();
      } catch (e) { toast("Failed to remove goal: " + e.message); }
    }));
}

async function addGoal() {
  const metric = $("goal-metric").value.trim();
  const target = parseFloat($("goal-target").value);
  if (!metric || Number.isNaN(target)) { toast("Metric and numeric target are required"); return; }
  const eid = ($("goal-experiment") && $("goal-experiment").value) || "";
  try {
    await api(`/api/projects/${state.project}/goals`, {
      method: "POST",
      body: JSON.stringify({
        metric, target, higher_better: $("goal-hb").checked,
        experiment_id: eid ? parseInt(eid, 10) : null,
      }),
    });
    $("goal-metric").value = $("goal-target").value = "";
    if ($("goal-experiment")) $("goal-experiment").value = "";
    loadGoals();
    toast("Goal saved — progress is checked after every run.");
  } catch (e) { toast("Failed to add goal: " + e.message); }
}

function renderExpList() {
  const el = $("exp-list");
  if (!el) return;
  const exps = state.expList || [];
  if (!exps.length) {
    el.innerHTML = '<div class="empty">No experiments yet — ask Fox to plan and run one in chat, or create one below.</div>';
    return;
  }
  el.innerHTML = "";
  for (const e of exps) {
    const card = document.createElement("div");
    card.className = "exp-card";
    card.dataset.id = e.id;
    const goal = e.goal_metric
      ? `<span class="muted">goal ${esc(e.goal_metric)} ${e.higher_better ? "↑" : "↓"} ${e.goal_target != null ? _fmtNum(e.goal_target) : "—"}</span>`
      : "";
    let planHtml = "";
    if (e.plan) {
      planHtml = `<details class="exp-plan"><summary>Plan</summary><div class="exp-plan-body">${esc(e.plan)}</div></details>`;
    }
    const status = e.status || "active";
    const active = status === "active";
    const badgeCls = active ? "det" : (status === "completed" ? "ok" : "warn");
    card.innerHTML = `<div class="exp-card-head">
        <b class="exp-card-name">${esc(e.name)}</b>
        <span class="exp-badge ${badgeCls}">${esc(status)}</span>
        <span class="muted exp-card-runs">${e.runs} run(s)</span>
        <span class="spacer"></span>
        <select class="exp-status" data-id="${e.id}" title="lifecycle status">
          <option value="active"${active ? " selected" : ""}>active</option>
          <option value="completed"${status === "completed" ? " selected" : ""}>completed</option>
          <option value="cancelled"${status === "cancelled" ? " selected" : ""}>cancelled</option>
        </select>
        <button class="btn subtle small exp-improve" data-id="${e.id}" data-name="${esc(e.name)}"${active ? "" : " disabled title=\"reopen the experiment first\""}>Improve</button>
      </div>
      ${e.hypothesis ? `<div class="exp-card-hyp muted">${esc(e.hypothesis)}</div>` : ""}
      ${goal}
      ${planHtml}
      <div class="exp-rank-host"></div>`;
    el.appendChild(card);
  }
  el.querySelectorAll(".exp-improve").forEach((b) =>
    b.addEventListener("click", () => {
      sendChat(`Improve the experiment "${b.dataset.name}" — run the next variant toward its goal.`,
               "improve_loop", { experiment_id: b.dataset.id });
    }));
  el.querySelectorAll(".exp-status").forEach((sel) =>
    sel.addEventListener("change", async () => {
      try {
        await api(`/api/projects/${state.project}/experiments/${sel.dataset.id}`, {
          method: "PATCH",
          body: JSON.stringify({ status: sel.value }),
        });
        toast(`Experiment #${sel.dataset.id} → ${sel.value}.`);
        await loadExperiments();
      } catch (e) { toast("Failed to update experiment: " + e.message); }
    }));
}

async function createExp() {
  const name = $("exp-new-name").value.trim();
  if (!name) { toast("Experiment name is required"); return; }
  let target = null;
  const t = $("exp-new-goal-target").value.trim();
  if (t !== "") {
    target = parseFloat(t);
    if (Number.isNaN(target)) { toast("Goal target must be a number"); return; }
  }
  try {
    await api(`/api/projects/${state.project}/experiments`, {
      method: "POST",
      body: JSON.stringify({
        name,
        hypothesis: $("exp-new-hypothesis").value.trim(),
        goal_metric: $("exp-new-goal-metric").value.trim(),
        goal_target: target,
        higher_better: $("exp-new-hb").checked,
        plan: $("exp-new-plan").value.trim(),
      }),
    });
    $("exp-new-name").value = $("exp-new-hypothesis").value = "";
    $("exp-new-goal-metric").value = $("exp-new-goal-target").value = "";
    $("exp-new-plan").value = "";
    $("exp-new-form").classList.add("hidden");
    await loadExperiments();
    toast("Experiment created — ask Fox to run variants for it in chat.");
  } catch (e) { toast("Failed to create experiment: " + e.message); }
}

function expMetric() { return state.expMetric || ""; }
function expNodeValue(node, metric) {
  const v = (node.metrics && node.metrics[metric] != null) ? node.metrics[metric] : node[metric];
  return (v == null || Number.isNaN(Number(v))) ? null : Number(v);
}
function _fmtAxis(v) { return String(Math.round(Number(v) * 1000) / 1000); }

function populateExpMetrics() {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  const keys = new Set();
  nodes.forEach((n) => Object.keys(n.metrics || {}).forEach((k) => keys.add(k)));
  const opts = [...keys].sort();
  if (!opts.includes(state.expMetric)) state.expMetric = opts[0] || "";
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
  return `rgb(${lerp(79, 201, t)},${lerp(63, 168, t)},${lerp(138, 255, t)})`;
}

/* ---- experiment grouping + compare-mode helpers (timeline / graph UX) ---- */

const EXP_COLORS = ["#a974ff", "#4f8cff", "#4cd08d", "#d29922", "#e06c6c",
                    "#00bcd4", "#f48fb1", "#9ccc65"];

function expOf(eid) {
  if (eid == null) return null;
  return (state.expList || []).find((e) => String(e.id) === String(eid)) || null;
}

function expColor(eid) {
  const idx = (state.expList || []).findIndex((e) => String(e.id) === String(eid));
  return idx >= 0 ? EXP_COLORS[idx % EXP_COLORS.length] : "#9b93ab";
}

function expLegend() {
  const exps = (state.expList || []).filter((e) => e.runs > 0);
  return exps.map((e) =>
    `<span class="exp-legend-item"><span class="exp-legend-dot" style="background:${expColor(e.id)}"></span>${esc(e.name)}</span>`).join("");
}

function expBestRun(runs, metric, higher) {
  let best = null;
  for (const r of runs) {
    const v = r.metrics && r.metrics[metric];
    if (v == null) continue;
    if (best === null || (higher ? v > best.v : v < best.v)) best = { v, id: r.id };
  }
  return best;
}

// Compare-mode: click two chart nodes to fill the run comparison.
let expComparePicks = { a: null, b: null };

function expCompareModeOn() {
  expComparePicks = { a: null, b: null };
  $("exp-compare-mode").classList.add("active");
  $("exp-compare-pick").classList.remove("hidden");
  updateComparePickBar();
}

function expCompareModeOff() {
  $("exp-compare-mode").classList.remove("active");
  $("exp-compare-pick").classList.add("hidden");
  expComparePicks = { a: null, b: null };
}

function updateComparePickBar() {
  const ra = runsById(expComparePicks.a);
  const rb = runsById(expComparePicks.b);
  $("cpk-a").textContent = ra ? "#" + ra.id + " " + (ra.label || "") : "—";
  $("cpk-b").textContent = rb ? "#" + rb.id + " " + (rb.label || "") : "—";
}

function runsById(id) {
  if (id == null) return null;
  return (state.expRuns || []).find((r) => String(r.id) === String(id)) || null;
}

function handleExpNodeClick(id) {
  if (state.expCompareMode) {
    if (expComparePicks.a == null || expComparePicks.a === id) {
      expComparePicks.a = expComparePicks.a === id ? null : id;
    } else if (expComparePicks.b == null || expComparePicks.b === id) {
      expComparePicks.b = expComparePicks.b === id ? null : id;
    }
    updateComparePickBar();
    if (expComparePicks.a != null && expComparePicks.b != null) {
      const selA = $("exp-cmp-a"), selB = $("exp-cmp-b");
      if (selA && selB) {
        selA.value = String(expComparePicks.a);
        selB.value = String(expComparePicks.b);
      }
      renderExpCompare();
      expCompareModeOff();
      const cmp = $("exp-compare");
      if (cmp) cmp.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    return;
  }
  selectRun(id);
}

// --------------------------------------------------------- run comparison ----

function populateExpCompare() {
  const runs = (state.agentRuns || []).map((r) => ({
    value: String(r.id),
    label: `#${r.id} ${(r.label || r.prompt || "").replace(/\s+/g, " ").slice(0, 70)}`,
  })).reverse();
  const runVals = new Set(runs.map((r) => r.value));
  const exps = ((state.expGraph && state.expGraph.nodes) || [])
    .filter((n) => !runVals.has(String(n.id)))
    .map((n) => ({
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
    const ra = runsById(a), rb = runsById(b);
    const artCol = (run) => {
      const arts = (run && run.artifacts) || [];
      if (!arts.length) return '<div class="muted">no artifacts</div>';
      return `<div class="cmp-arts">` + arts.map((x) => {
        const obj = typeof x === "object" ? x : { name: x, id: x, data_type: null };
        if (obj.id && obj.data_type === "png") {
          return `<img class="cmp-thumb" src="${B(`/artifacts/${obj.id}`)}" data-art-id="${esc(obj.id)}" title="${esc(obj.name || obj.id)}">`;
        }
        return `<a class="ed-art" data-art-id="${esc(obj.id || "")}">📄 ${esc(obj.name || obj.id)}</a>`;
      }).join("") + `</div>`;
    };
    let h = `<div class="cmp-head">
      <div class="cmp-col"><div class="cmp-run">${esc(c.a)}</div>${artCol(ra)}</div>
      <div class="cmp-vs">vs</div>
      <div class="cmp-col"><div class="cmp-run">${esc(c.b)}</div>${artCol(rb)}</div>
    </div>`;
    if (!c.rows.length) {
      h += `<div class="empty">No shared numeric metrics between <b>${esc(c.a)}</b> and <b>${esc(c.b)}</b>.</div>`;
      el.innerHTML = h;
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
    h += `<table class="cmp-table"><tbody>${rows}</tbody></table>
      <div class="cmp-summary muted">${sum.shared} shared metric(s) · ${sum.increased} up · ${sum.decreased} down · ${sum.unchanged} unchanged</div>`;
    el.innerHTML = h;
  } catch (e) {
    el.innerHTML = `<div class="empty">Comparison failed: ${esc(e.message || e)}</div>`;
  }
}

// --------------------------------------------------------- timeline chart ----

function buildTimelineSvg(metric, W) {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  const H = 340, padL = 48, padR = 16, padT = 30, padB = 44;
  const vals = nodes.map((n) => expNodeValue(n, metric));
  const present = vals.filter((v) => v != null);
  if (!present.length) return '<div class="empty">No numeric values for this metric.</div>';
  const min = Math.min(...present), max = Math.max(...present), span = (max - min) || 1;
  const xs = nodes.map((_, i) => nodes.length > 1
    ? padL + i * (W - padL - padR) / (nodes.length - 1) : W / 2);
  const y = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);

  // Per-experiment goal lines (dashed) when this metric is a goal.
  const goalLines = [];
  const seenExp = new Set();
  nodes.forEach((n) => {
    const exp = expOf(n.experiment_id);
    if (!exp || seenExp.has(exp.id)) return;
    seenExp.add(exp.id);
    if (exp.goal_metric === metric && exp.goal_target != null) {
      goalLines.push({ v: Number(exp.goal_target), color: expColor(exp.id), name: exp.name });
    }
  });

  // Best run for this metric (direction-aware via the goal experiment).
  const firstExpNode = nodes.find((n) => n.experiment_id != null);
  const goalExp = expOf(firstExpNode && firstExpNode.experiment_id);
  const higher = goalExp ? goalExp.higher_better !== false : true;
  const best = expBestRun(nodes, metric, higher);

  let out = `<svg viewBox="0 0 ${W} ${H}">`
    + `<defs><linearGradient id="tlfill" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0%" stop-color="#a974ff" stop-opacity="0.35"/>`
    + `<stop offset="100%" stop-color="#a974ff" stop-opacity="0"/></linearGradient></defs>`;

  for (let k = 0; k <= 4; k++) {
    const v = min + span * k / 4, yy = y(v);
    out += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="#332d44" stroke-width="0.5"></line>`;
    out += `<text x="${padL - 8}" y="${yy + 3}" text-anchor="end" font-size="10" fill="#9b93ab">${_fmtNum(v)}</text>`;
  }
  // Goal lines.
  for (const g of goalLines) {
    const gy = Math.max(padT, Math.min(H - padB, y(g.v)));
    out += `<g><line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="${g.color}" stroke-width="1.6" stroke-dasharray="7 5" opacity="0.9"></line>`
      + `<text x="${W - padR}" y="${gy - 5}" text-anchor="end" font-size="9.5" fill="${g.color}">goal ${esc(g.name)} ${_fmtNum(g.v)}</text></g>`;
  }

  const pts = nodes.map((n, i) => vals[i] == null ? null : `${xs[i]},${y(vals[i])}`).filter(Boolean);
  if (pts.length) {
    const linePts = pts.join(" ");
    const area = `${xs[0]},${y(min)} ${linePts} ${xs[nodes.length - 1]},${y(min)}`;
    out += `<polygon points="${area}" fill="url(#tlfill)"></polygon>`;
    out += `<polyline points="${linePts}" fill="none" stroke="#a974ff" stroke-width="2" filter="drop-shadow(0 0 6px rgba(169,116,255,.5))"></polyline>`;
  }

  nodes.forEach((n, i) => {
    if (vals[i] == null) return;
    const eid = n.experiment_id;
    const color = eid != null ? expColor(eid) : (n.fresh ? "#d29922" : "#b98cff");
    const sel = state.expSelected === n.id ? " selected" : "";
    const isBest = best && String(best.id) === String(n.id);
    const tip = `Run #${i + 1} · ${n.label || ""}${n.fresh ? " (fresh)" : ""}\n${metric}: ${_fmtNum(vals[i])}\n${expOf(eid) ? "experiment: " + expOf(eid).name : ""}\n${n.timestamp ? new Date(n.timestamp).toLocaleString() : ""}`;
    let mark = "";
    if (isBest) {
      mark = `<circle r="12" fill="none" stroke="#f3f0fa" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.9"></circle>`;
    }
    out += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${xs[i]},${y(vals[i])})">`
      + `<title>${esc(tip)}</title>`
      + mark
      + `<circle r="7" fill="${color}" stroke="#19132b" stroke-width="2" filter="drop-shadow(0 0 6px ${color}aa)"></circle>`
      + (isBest ? `<text y="-20" text-anchor="middle" font-size="10">★</text>` : "")
      + `<text y="-12" text-anchor="middle" font-size="10" font-weight="700" fill="${color}">${_fmtNum(vals[i])}</text>`
      + `<text y="22" text-anchor="middle" font-size="9" fill="#9b93ab">#${i + 1}${n.label ? " " + esc(n.label.slice(0, 12)) : ""}</text></g>`;
  });

  out += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="#f3f0fa">${metric.replace(/_/g, " ")} — evolution across runs (★ best · dashed = goal)</text>`;
  out += `</svg>`;
  const legend = expLegend();
  return out + (legend ? `<div class="exp-chart-legend">${legend}</div>` : "");
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

// Per-run sub-nodes: kind/seed tags, findings and artifact keywords, so the
// graph shows at a glance what each run contains (works for any run kind).
function expSubNodes(run) {
  const nodes = [];
  const tag = (label) => nodes.push({ kind: "tag", label, color: "#d29922" });
  const find = (label) => nodes.push({ kind: "finding", label, color: "#b98cff" });
  const art = (label) => nodes.push({ kind: "artifact", label, color: "#a974ff" });

  if (run.seed != null) tag("seed " + run.seed);
  if (run.fresh) tag("fresh");
  if (run.kind) tag(run.kind);
  for (const f of (run.findings || []).slice(0, 4)) {
    find(String(f).replace(/\s+/g, " ").slice(0, 24));
  }
  for (const a of (run.artifacts || []).slice(0, 3)) {
    const name = (typeof a === "object" ? a.name : a) || "";
    art(name.replace(/\.(png|md)$/, "").slice(0, 16));
  }
  return nodes;
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
    id: g.id, seed: g.seed, fresh: g.fresh, index: i, run: runs[i] || {}, g,
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
      + `stroke-width="${(0.7 + sim * 3).toFixed(2)}" opacity="${(0.35 + sim * 0.45).toFixed(2)}"></line>`
      + `<text x="${mx}" y="${my - 4}" text-anchor="middle" font-size="8.5" fill="#9b93ab" paint-order="stroke" stroke="#0a0a0d" stroke-width="2.5">sim ${(sim * 100).toFixed(0)}% · ov ${(ov * 100).toFixed(0)}%</text></g>`;
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
        + `stroke="#463a66" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.9"></line>`;
    });
  }

  // experiment nodes + sub-nodes
  nodes.forEach((n, i) => {
    const v = vals[i];
    const color = v == null ? "#9b93ab" : _metricColor(v, vmin, vmax);
    const ec = expColor(n.g.experiment_id);
    const sel = state.expSelected === n.id ? " selected" : "";
    const tip = `Run #${i + 1} · ${n.run.label || ""}${n.fresh ? " (fresh)" : ""}\n${metric}: ${_fmtNum(v)}\n${expOf(n.g.experiment_id) ? "experiment: " + expOf(n.g.experiment_id).name : ""}\nclick for full summary`;
    out += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${pos[i].x},${pos[i].y})">`
      + `<title>${esc(tip)}</title>`
      + (n.g.experiment_id != null
        ? `<circle r="21" fill="none" stroke="${ec}" stroke-width="2" opacity="0.75"></circle>` : "")
      + `<circle r="16" fill="${color}" stroke="#19132b" stroke-width="2.5" filter="drop-shadow(0 0 10px ${color}99)"></circle>`
      + (v != null ? `<text y="4" text-anchor="middle" font-size="11" font-weight="700" fill="#0a0a0d">${_fmtNum(v)}</text>` : "")
      + `<text y="-28" text-anchor="middle" font-size="11" font-weight="700" fill="#f3f0fa">Run #${i + 1}</text>`
      + `<text y="34" text-anchor="middle" font-size="9" fill="#9b93ab">${esc((n.run.label || "run " + n.id).slice(0, 18))}</text></g>`;

    const subs = expSubNodes(n.run);
    const R = 92;
    subs.forEach((s, k) => {
      const a = -Math.PI / 2 + k / subs.length * 2 * Math.PI;
      const sx = pos[i].x + Math.cos(a) * R;
      const sy = pos[i].y + Math.sin(a) * R;
      out += `<g class="exp-subnode" data-id="${esc(n.id)}" transform="translate(${sx},${sy})">`
        + `<title>${esc(`${n.seed}: ${s.kind} — ${s.label}`)}</title>`
        + `<circle r="6.5" fill="${s.color}" stroke="#19132b" stroke-width="1.2" filter="drop-shadow(0 0 5px ${s.color}aa)"></circle></g>`;
      // tiny visible label on the sub-node
      const lx = sx + Math.cos(a) * 14, ly = sy + Math.sin(a) * 14 + 3;
      out += `<text x="${lx}" y="${ly}" text-anchor="middle" font-size="8" fill="${s.color}" opacity="0.95" paint-order="stroke" stroke="#0a0a0d" stroke-width="2">${esc(s.label)}</text>`;
    });
  });

  // legend
  out += `<g transform="translate(${W - 290}, 12)">`
    + `<text x="0" y="-4" font-size="9" fill="#9b93ab">${metric.replace(/_/g, " ")}</text>`;
  for (let i = 0; i < 70; i++) {
    const t = i / 69;
    out += `<rect x="${i}" y="0" width="2" height="9" fill="${_metricColor(vmin + t * (vmax - vmin), vmin, vmax)}"></rect>`;
  }
  out += `<text x="0" y="20" font-size="8.5" fill="#9b93ab">${_fmtNum(vmin)}</text>`
    + `<text x="69" y="20" text-anchor="end" font-size="8.5" fill="#9b93ab">${_fmtNum(vmax)}</text>`
    + `<text x="78" y="9" font-size="9" fill="#d29922">● tag</text>`
    + `<text x="78" y="20" font-size="9" fill="#b98cff">● finding</text>`
    + `<text x="78" y="31" font-size="9" fill="#a974ff">● artifact</text></g>`;

  out += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="#f3f0fa">experiment graph — ${metric.replace(/_/g, " ")} (spokes = tags · findings · artifacts; edge labels = similarity/overlap; ring = experiment)</text>`;
  out += `</svg>`;
  const legend = expLegend();
  return out + (legend ? `<div class="exp-chart-legend">${legend}</div>` : "");
}

function renderExperiments() {
  const runs = state.expRuns || [];
  const empty = '<div class="empty">No runs yet in this project. Ask Fox to run an analysis or experiment in chat and each turn will appear here.</div>';
  const metric = expMetric();
  const charts = [
    ["expmain-timeline", "timeline", 1240, 330],
    ["expmain-graph", "graph", 1240, 580],
  ];
  for (const [id, kind, w, h] of charts) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = runs.length
      ? (kind === "timeline" ? buildTimelineSvg(metric, w) : buildGraphSvg(metric, w))
      : empty;
    graphViewRestore(el.querySelector("svg"), id, w, h);
    attachGraphControls(el, id, () => el.querySelector("svg"), w, h);
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
  $("editor-panel").classList.toggle("hidden", view !== "editor");
  document.querySelectorAll(".mainview-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mainview === view));
  const app = document.getElementById("app");
  if (view === "experiments" || view === "agent" || view === "editor") {
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
  if (view === "editor") loadEditor();
}

/* ============================ editor (VS Code) ============================= */

async function loadEditor() {
  const status = $("editor-status");
  try {
    const r = await api("/api/editor");
    const ed = (r && r.editor) || {};
    const url = (ed.url || "").replace(/\/+$/, "");
    const frame = $("editor-frame");
    const fallback = $("editor-fallback");
    const openNew = $("editor-open-new");
    if (!ed.enabled) {
      status.textContent = "editor disabled (FOX_EDITOR_ENABLED=0)";
      fallback.classList.remove("hidden");
      frame.classList.add("hidden");
      $("editor-fallback-link").href = "#";
      return;
    }
    openNew.href = url;
    $("editor-fallback-link").href = url;
    frame.src = url + "/?folder=" + encodeURIComponent(ed.folder || "");
    status.textContent = ed.reachable ? "connected" : "code-server unreachable — start it with `docker compose up -d code-server`";
    if (!ed.reachable) fallback.classList.remove("hidden");
    else fallback.classList.add("hidden");
    frame.classList.toggle("hidden", !ed.reachable);
  } catch (e) {
    status.textContent = "failed to load editor config";
  }
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
    if (n) out.push({ id: n.id, index: n.index, seed: n.seed, label: n.label,
                      similarity: e.similarity, overlap: e.overlap });
  }
  return out.sort((a, b) => (b.similarity || 0) - (a.similarity || 0)).slice(0, 3);
}

function renderExpDetail() {
  const el = $("exp-detail");
  const runs = state.expRuns || [];
  const run = runs.find((r) => r.id === state.expSelected) || null;
  if (!run) {
    const msg = '<div class="empty">Select a run node to see its summary, findings and related runs.</div>';
    if (el) el.innerHTML = msg;
    if ($("expmain-detail")) $("expmain-detail").innerHTML = msg;
    return;
  }
  const badge = run.fresh ? '<span class="exp-badge fresh">fresh</span>'
                          : `<span class="exp-badge det">${esc(run.kind || "run")}</span>`;
  const time = run.timestamp ? new Date(run.timestamp).toLocaleString() : "—";
  let h = `<div class="ed-head">${esc(run.label || ("Run #" + run.id))} ${badge}</div>`;
  h += `<div class="ed-meta">${esc(time)}</div>`;

  const mkeys = Object.keys(run.metrics || {});
  if (mkeys.length) {
    h += `<div class="ed-sec">Metrics</div><table>`;
    for (const k of mkeys) {
      h += `<tr><th>${esc(k.replace(/_/g, " "))}</th><td>${_fmtNum(run.metrics[k])}</td></tr>`;
    }
    h += `</table>`;
  }
  if (run.prompt) {
    h += `<div class="ed-sec">Prompt</div><div class="ed-find">${esc(run.prompt)}</div>`;
  }
  if ((run.findings || []).length) {
    h += `<div class="ed-sec">Findings</div>`;
    for (const f of run.findings) h += `<div class="ed-find">${esc(f)}</div>`;
  }
  h += `<div class="ed-sec">Artifacts</div>`;
  const arts = run.artifacts || [];
  if (!arts.length) {
    h += `<div class="muted">none</div>`;
  } else {
    h += `<div class="ed-arts">`;
    for (const a of arts) {
      const obj = typeof a === "object" ? a : { name: a, id: a, data_type: null };
      if (obj.id && obj.data_type === "png") {
        h += `<div class="ed-art-thumb"><img src="${B(`/artifacts/${obj.id}`)}" alt="${esc(obj.name || obj.id)}" data-art-id="${esc(obj.id)}" title="${esc(obj.name || obj.id)}"></div>`;
      } else {
        h += `<a class="ed-art" data-art-id="${esc(obj.id || "")}">📄 ${esc(obj.name || obj.id)}</a>`;
      }
    }
    h += `</div>`;
  }
  const sims = similarRuns(run.id);
  if (sims.length) {
    h += `<div class="ed-sec">Similar / overlapping runs</div>`;
    for (const s of sims) {
      h += `<div class="ed-sim"><a class="ed-sim-link" data-id="${esc(s.id)}">${esc(s.label || ("Run #" + (s.index + 1)))}</a> — similarity <b>${((s.similarity || 0) * 100).toFixed(0)}%</b> · overlap <b>${((s.overlap || 0) * 100).toFixed(0)}%</b></div>`;
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

const EXP_VIEWS = ["expmain-timeline", "expmain-graph"];
const expPan = {};
EXP_VIEWS.forEach((id) => {
  const wrap = $(id);
  const getSvg = () => wrap.querySelector("svg");
  expPan[id] = attachGraphPan(wrap, id, getSvg, ".exp-node, .exp-subnode");
  attachGraphControls(wrap, id, getSvg, 1240, id === "expmain-timeline" ? 330 : 580);
});
["expmain-timeline", "expmain-graph"].forEach((id) => {
  $(id).addEventListener("click", (e) => {
    if (expPan[id].drag && expPan[id].drag.moved) { expPan[id].drag.moved = false; return; }
    const n = e.target.closest(".exp-node, .exp-subnode");
    if (n && n.dataset.id) handleExpNodeClick(n.dataset.id);
  });
});
["exp-detail", "expmain-detail"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", (e) => {
    const sim = e.target.closest(".ed-sim-link");
    if (sim) { selectRun(sim.dataset.id); return; }
    const thumb = e.target.closest(".ed-art-thumb img, .cmp-thumb");
    if (thumb) { openArtifactById(thumb.dataset.artId); return; }
    const art = e.target.closest(".ed-art");
    if (art) openArtifactById(art.dataset.artId);
  });
});
$("exp-refresh-main").addEventListener("click", loadExperiments);
$("exp-cmp-go").addEventListener("click", renderExpCompare);
$("exp-cmp-a").addEventListener("change", renderExpCompare);
$("exp-cmp-b").addEventListener("change", renderExpCompare);
$("exp-compare-mode").addEventListener("click", () => {
  if (state.expCompareMode) { state.expCompareMode = false; expCompareModeOff(); }
  else { state.expCompareMode = true; expCompareModeOn(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.expCompareMode) {
    state.expCompareMode = false;
    expCompareModeOff();
  }
});
$("goal-add").addEventListener("click", addGoal);
$("goal-target").addEventListener("keydown", (e) => { if (e.key === "Enter") addGoal(); });
$("goal-metric").addEventListener("keydown", (e) => { if (e.key === "Enter") addGoal(); });
$("runs-refresh").addEventListener("click", loadExperiments);
$("exp-new-toggle").addEventListener("click", () => $("exp-new-form").classList.toggle("hidden"));
$("exp-new-create").addEventListener("click", createExp);

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
$("mainview-editor").addEventListener("click", () => switchMainView("editor"));
$("editor-refresh").addEventListener("click", loadEditor);

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
