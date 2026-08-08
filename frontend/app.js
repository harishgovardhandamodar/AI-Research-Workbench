/* Local - Open - Agentic Experimentation Workbench frontend */

const $ = (id) => document.getElementById(id);
const FOX_BASE = window.FOX_BASE || "";
const B = (path) => (FOX_BASE ? FOX_BASE + path : path);

/* ---------- theme (light / dark) ---------- */

let themeVars = {};
function cssVar(name, fallback) {
  if (themeVars[name] === undefined) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    themeVars[name] = v || fallback || "";
  }
  return themeVars[name];
}
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "dark";
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme === "light" ? "light" : "dark");
  themeVars = {};  // invalidate cached chart colours
  try { localStorage.setItem("fox-theme", theme === "light" ? "light" : "dark"); } catch (e) {}
  const btn = $("theme-toggle");
  if (btn) btn.textContent = theme === "light" ? "🌙" : "🌓";
}
function toggleTheme() {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
  // re-render charts whose colours come from CSS variables
  if (state.expRuns) renderExperiments();
  if (state.branches && branchView === "branches") renderBranchGraph();
  if (state.audit) renderAuditOverview();
  if (state.branches && branchView !== "branches") switchBranchView(branchView);
}
(function initTheme() {
  let saved = "dark";
  try { saved = localStorage.getItem("fox-theme") || "dark"; } catch (e) {}
  const reduced = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  applyTheme(saved === "light" ? "light" : (saved === "dark" ? "dark" : (reduced ? "light" : "dark")));
})();
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
  suggestions: {},   // suggestion id -> {status, delta, improved}
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
  let text = esc(String(src)).replace(/\r\n/g, "\n");
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

  // lists (simple, non-nested; task-list items get checkboxes)
  text = text.replace(/((?:^[ \t]*[-*] .*\n?)+)/gm, (m) => {
    const items = m.trim().split("\n").map((l) => {
      const done = /^[ \t]*[-*] \[x\]/i.test(l);
      const checked = /^[ \t]*[-*] \[[ xX]\]/.test(l);
      const content = l.replace(/^[ \t]*[-*] \[[ xX]\]\s*/, "").replace(/^[ \t]*[-*] /, "");
      const box = checked ? `<input type="checkbox" disabled ${done ? "checked" : ""}>` : "";
      return `<li>${box}${content}</li>`;
    }).join("");
    return `<ul>${items}</ul>`;
  });
  text = text.replace(/((?:^[ \t]*\d+\. .*\n?)+)/gm, (m) => {
    const items = m.trim().split("\n").map((l) => `<li>${l.replace(/^[ \t]*\d+\. /, "")}</li>`).join("");
    return `<ol>${items}</ol>`;
  });

  text = text.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // auto-link bare URLs (after markdown links so already-linked ones win)
  text = text.replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  // images (workflow figures): ![name](/artifacts/<id>) -> <img> (base-aware);
  // also accept artifact:<id> URLs the agent sometimes writes.
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => {
    const artId = /^\/artifacts\//.test(url) ? url.split("/").pop()
      : /^artifact:/.test(url) ? url.replace(/^artifact:/, "") : "";
    const src = artId ? B("/artifacts/" + artId) : url;
    return `<img src="${esc(src)}" alt="${esc(alt)}" class="chat-fig" data-art-id="${esc(artId)}">`;
  });
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
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
    case "user_message": renderUserMessage(p.content, p.tags, p.created_at, p.experiment_id, p.id); break;
    case "stream_delta": streamDelta(p.text); break;
    case "assistant_message": finalizeAssistant(p.content, p.tags, p.created_at, p.experiment_id, p.mcp_name, p.action, p.tools, p.model, p.id); break;
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
      loadSuggestions();
      break;
    case "notice": toast(p.message, 6000); break;
    case "status": setBusyStatus(p); break;
    case "workflow": renderWorkflow(p); loadCampaigns(); break;
    case "done": onTurnDone(); attachNextSteps(); loadExperiments(); loadSuggestions(); loadCampaigns(); break;
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

function msgContainer(role, tags, ts, target, who, mid) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (mid != null) div.dataset.mid = mid;
  const label = document.createElement("div");
  label.className = "msg-label";
  const sender = role === "user" ? "You"
    : (who || foxSenderLabel()) || "Fox";
  const primaryAction = role === "user"
    ? `<button class="msg-act msg-edit" title="Edit & resend this message">✎</button>`
    : `<button class="msg-act msg-retry" title="Regenerate this reply">↻</button>`;
  label.innerHTML = `<span class="msg-who">${esc(sender)}</span>
    <span class="spacer"></span>
    <span class="msg-time">${ts ? fmtClock(ts) : ""}</span>
    <span class="msg-actions">
      ${primaryAction}
      <button class="msg-act msg-copy" title="Copy message" data-role="${role}">⧉</button>
      <button class="msg-act msg-del" title="Delete this message">🗑</button>
    </span>`;
  div.appendChild(label);
  const tagHtml = msgTagsHtml(tags);
  if (tagHtml) div.insertAdjacentHTML("beforeend", tagHtml);
  const body = document.createElement("div");
  body.className = "msg-body";
  div.appendChild(body);
  (target || $("messages")).appendChild(div);
  // Retry / edit / delete are wired directly (copy stays delegated below).
  const retry = div.querySelector(".msg-retry");
  if (retry) retry.addEventListener("click", () => retryMessage(div));
  const edit = div.querySelector(".msg-edit");
  if (edit) edit.addEventListener("click", () => editUserMessage(div));
  const del = div.querySelector(".msg-del");
  if (del) del.addEventListener("click", () => deleteMessage(mid, div));
  return { div, body };
}

function retryMessage(msgEl) {
  const text = previousUserText(msgEl);
  if (!text) { toast("No earlier user message to retry."); return; }
  sendChat(text);
}

function editUserMessage(msgEl) {
  const input = $("input");
  const body = msgEl && msgEl.querySelector(".msg-body");
  const txt = body ? body.textContent : "";
  if (input && txt) {
    input.value = txt;
    autoResize(input);
    input.focus();
    input.setSelectionRange(txt.length, txt.length);
    toast("Editing this message — press Enter to resend.");
  }
}

function deleteMessage(mid, msgEl) {
  if (!confirm("Delete this message?")) return;
  if (mid == null) { msgEl.remove(); toast("Message removed (not persisted)."); return; }
  api(`/api/projects/${state.project}/messages/${mid}`, { method: "DELETE" })
    .then(() => { toast("Message deleted."); refreshState(); })
    .catch((e) => toast("Delete failed: " + e.message, 4000));
}

// Text of the most recent user message that appears before `el` in the chat.
function previousUserText(el) {
  const msgs = $("messages").querySelectorAll(".msg");
  let out = "";
  for (const m of msgs) {
    if (m === el) break;
    if (m.classList.contains("user")) {
      const b = m.querySelector(".msg-body");
      if (b) out = b.textContent;
    }
  }
  return out;
}

// "Fox - <Model> - <MCP Name> - <Action>": which model, MCP server and tool
// action produced this bubble, so a glance at the chat shows what Fox actually
// did. Falls back to the current model when a message predates the model/mcp
// fields (old persisted history), and to plain "Fox" when nothing is known.
function foxCurrentModel() {
  try { return (state.config && state.config.llm && state.config.llm.model) || ""; }
  catch (e) { return ""; }
}

function foxSenderLabel(mcp, action, model) {
  const parts = ["Fox"];
  const mdl = model || foxCurrentModel();
  if (mdl) parts.push(mdl);
  if (mcp && action) {
    parts.push(mcp);
    parts.push(action);
  }
  return parts.join(" - ");
}

function foxToolName(name) {
  if (!name) return "tool";
  if (name.indexOf("__") > 0) return name.split("__").join(" · ");
  return name;
}

/* ---- conversation sets: group request + steps + result, collapsible ---- */

function setDt(ts) {
  try {
    const n = Number(ts);
    if (!isFinite(n)) return "";
    const d = new Date(n * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleString();
  } catch (e) { return ""; }
}

function setTitleFor(userMsg) {
  try {
    const meta = (userMsg && userMsg.meta) || {};
    const exp = expOf(meta.experiment_id);
    const title = exp ? exp.name
      : (userMsg && userMsg.content ? String(userMsg.content).replace(/\s+/g, " ").slice(0, 60) : "conversation");
    const it = /iteration\s+(\d+)/i.exec((meta.tags || []).join(" "));
    return {
      title,
      iteration: it ? it[1] : "",
      expId: meta.experiment_id != null ? meta.experiment_id : null,
      ts: userMsg ? userMsg.created_at : null,
    };
  } catch (e) {
    return { title: "conversation", iteration: "", expId: null, ts: null };
  }
}

function msgSetCreate(userMsg, open) {
  const wrap = $("messages");
  const div = document.createElement("div");
  div.className = "msg-set" + (open ? "" : " collapsed");
  if (userMsg && userMsg.id != null) div.dataset.userId = userMsg.id;
  const head = document.createElement("button");
  head.className = "mset-head";
  head.type = "button";
  head.title = "Expand / collapse";
  const body = document.createElement("div");
  body.className = "mset-body";
  div.appendChild(head);
  div.appendChild(body);
  wrap.appendChild(div);
  const setState = { preview: "" };
  const update = () => {
    try {
      const info = setTitleFor(userMsg);
      const it = info.iteration ? " · iteration " + info.iteration : "";
      const dt = info.ts ? " · " + setDt(info.ts) : "";
      const prev = setState.preview
        ? `<span class="mset-prev">» ${esc(setState.preview)}</span>` : "";
      head.innerHTML = `<span class="caret">▸</span>`
        + `<span class="mset-title">${esc(info.title)}</span>`
        + (it ? `<span class="mset-iter">${esc(it)}</span>` : "")
        + prev
        + `<span class="spacer"></span>`
        + `<span class="mset-time">${esc(dt)}</span>`;
    } catch (e) {
      head.textContent = "▸ conversation";
    }
  };
  update();
  return { div, body, setState, update };
}

function expandSetOf(el) {
  const set = el.closest(".msg-set");
  if (set) set.classList.remove("collapsed");
}

function renderUserMessage(content, tags, ts, expId, mid) {
  const userMsg = {
    id: mid,
    content: content || "",
    tags: tags || [],
    created_at: ts,
    meta: { tags: tags || [], experiment_id: expId },
  };
  const set = msgSetCreate(userMsg, true);
  state._currentSet = set;
  const el = msgContainer("user", tags, ts, set.body, undefined, mid);
  el.body.textContent = content;
  tagMessageExperiment(el, expId);
  scrollBottom();
}

function ensureAssistant(tags, mid) {
  if (curAssistantEl && document.body.contains(curAssistantEl.div)) return curAssistantEl;
  if (!state._currentSet) state._currentSet = msgSetCreate(null, true);
  const el = msgContainer("assistant", tags, null, state._currentSet.body, undefined, mid);
  curAssistantEl = el;
  setConn("busy");
  state.streaming = true;
  return el;
}

function streamDelta(text) {
  const el = ensureAssistant();
  el.raw = (el.raw || "") + text;
  // Debounce the full markdown re-render: rendering the whole buffer on every
  // token is O(n²) and flickers on half-formed tables/fences. Re-render at
  // ~10 Hz during streaming, then once, cleanly, on finalize.
  if (!el._renderTimer) {
    el._renderTimer = setTimeout(() => {
      el._renderTimer = null;
      el.body.innerHTML = renderMarkdown(el.raw) + '<span class="cursor"></span>';
    }, 90);
  }
  scrollBottom();
}

function renderStreamFinal(el) {
  if (!el) return;
  if (el._renderTimer) { clearTimeout(el._renderTimer); el._renderTimer = null; }
  el.body.innerHTML = renderMarkdown(el.raw || "");
}

function finalizeAssistant(content, tags, ts, expId, mcp, action, tools, model, mid) {
  const el = curAssistantEl;
  if (el) {
    el.raw = content || el.raw || "";
    renderStreamFinal(el);
    enhanceCodeBlocks(el.body);
    maybeAttachRepoButtons(el, tags);
    tagMessageExperiment(el, expId);
    if (mid != null) el.div.dataset.mid = mid;
    if (ts) {
      const t = el.div.querySelector(".msg-time");
      if (t) t.textContent = fmtClock(ts);
    }
    const who = el.div.querySelector(".msg-who");
    if (who) who.textContent = foxSenderLabel(mcp, action, model);
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
      state.mgmtActivity = { action: kind, ...r };
      const link = repoCommitLinkHtml(r);
      if (kind === "commit") {
        st.innerHTML = "Committed " + link +
          (r.committed_at ? " · " + esc(fmtDt(r.committed_at)) : "") + " ✓";
      } else {
        st.innerHTML = "Pushed " + link +
          (r.pushed_at ? " on " + esc(fmtDt(r.pushed_at)) : "") + " ✓";
      }
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

function repoCommitLinkHtml(r) {
  if (!r || !r.commit) return "";
  return r.commit_url
    ? `<a href="${esc(r.commit_url)}" target="_blank" rel="noopener" title="Open commit on GitHub">${esc(r.commit)}</a>`
    : esc(r.commit);
}

function fmtDt(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

function scrollBottom() {
  const m = $("messages");
  if (!m) return;
  // Only auto-scroll when the user is already near the bottom, so they can read
  // earlier messages while a long process streams without being yanked down.
  const nearBottom = m.scrollHeight - m.scrollTop - m.clientHeight < 120;
  if (nearBottom) m.scrollTop = m.scrollHeight;
}

// Jump straight to the newest message (used on full chat re-renders, e.g. page
// refresh / project switch), regardless of the current scroll position.
function scrollToLatest() {
  const m = $("messages");
  if (!m) return;
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
  // An idle panel is just noise — only surface it while a pipeline is actually
  // active (running or waiting on approval).
  if (snap.status === "idle" || !stages.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
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
    if (s.state === "failed") {
      const retry = document.createElement("button");
      retry.className = "btn subtle small wf-retry";
      retry.textContent = "↻ retry";
      retry.addEventListener("click", () => sendChat("", "retry_stage", { stage: s.id }));
      row.appendChild(retry);
    }
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
      <span class="tname">${esc(foxToolName(p.name))}</span>
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

async function openArtifact(a) {
  currentArtifact = a;
  $("art-title").textContent = `${a.name} — ${a.kind}`;
  const view = $("art-view");
  if (a.data_type === "png") {
    view.innerHTML = `<img src="${B(`/artifacts/${a.id}`)}" alt="">`;
  } else {
    view.innerHTML = '<div class="muted">Loading…</div>';
    try {
      const res = await fetch(B(`/artifacts/${a.id}`));
      const text = await res.text();
      if (a.data_type === "html") {
        view.innerHTML = `<iframe class="art-html" srcdoc="${esc(text)}"></iframe>`;
      } else {
        const md = document.createElement("div");
        md.className = "art-md";
        md.innerHTML = renderMarkdown(text);
        view.innerHTML = "";
        view.appendChild(md);
      }
    } catch (e) {
      view.innerHTML = `<pre>${esc(a.description || "")}</pre>`;
    }
  }
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

/* ======================== kernel live status ======================== */

let _kernelPollTimer = null;

function renderKernelStatus(st) {
  const pill = $("kernel-status");
  if (!pill) return;
  if (!st || !st.state) { pill.hidden = true; return; }
  const state = st.state || "unknown";
  const busy = state === "busy";
  pill.hidden = false;
  pill.classList.toggle("busy", busy);
  pill.classList.toggle("stopped", state === "stopped" || state === "dead");
  pill.classList.toggle("remote", !!st.remote);
  const label = busy ? "kernel busy" : (state === "idle" ? "kernel idle" : state);
  pill.innerHTML = `<span class="kp-dot"></span><span>${esc(label)}${st.remote ? " (remote)" : ""}</span>`;

  const detail = $("kernel-status-detail");
  if (detail) {
    detail.innerHTML = "";
    const rows = [
      ["state", state],
      ["pid", st.pid != null ? st.pid : "—"],
      ["uptime", st.uptime != null ? `${st.uptime}s` : "—"],
      ["executions", st.exec_count],
      ["cwd", st.cwd || "—"],
    ];
    if (st.current_code) rows.push(["running", String(st.current_code).slice(0, 80)]);
    if (st.remote_url) rows.push(["server", st.remote_url]);
    if (st.last_error) rows.push(["last error", String(st.last_error).slice(0, 120)]);
    const d = document.createElement("div");
    d.className = busy ? "finding info" : "finding";
    d.innerHTML = rows.map(([k, v]) =>
      `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("");
    detail.appendChild(d);
  }
}

function startKernelPolling() {
  stopKernelPolling();
  _kernelPollTimer = setInterval(fetchKernelStatus, 3000);
}

function stopKernelPolling() {
  if (_kernelPollTimer) { clearInterval(_kernelPollTimer); _kernelPollTimer = null; }
}

async function fetchKernelStatus() {
  if (!state.project) return;
  try {
    const st = await api(`/api/projects/${state.project}/kernel/status`);
    renderKernelStatus(st);
  } catch (e) { /* silent: server may be between states */ }
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
      const st = suggestionStatus(s);
      d.innerHTML = `<span class="sev">→</span><span class="sug-body">${esc(title)}` +
        (action && action !== title ? `<span class="sug-action">${esc(action)}</span>` : "") +
        `</span>`;
      if (st.badge) {
        const b = document.createElement("span");
        b.className = "sug-badge " + st.cls;
        b.textContent = st.badge;
        d.appendChild(b);
      }
      if (prompt && !st.done) {
        const btn = document.createElement("button");
        btn.className = "btn subtle small sug-run";
        btn.textContent = "Apply & rerun";
        btn.addEventListener("click", () => sendChat(prompt, "rerun_suggestion", { suggestion_id: s.id }));
        d.appendChild(btn);
      }
      c.appendChild(d);
    }
  }
}

function suggestionStatus(s) {
  const id = (s && typeof s === "object" && s.id) ? Number(s.id) : null;
  const rec = id ? (state.suggestions[id] || {}) : {};
  if (rec.improved === 1) return { done: true, badge: "✓ improved", cls: "ok" };
  if (rec.improved === 0) return { done: true, badge: "✗ no gain", cls: "warn" };
  if (rec.status === "applied") return { done: true, badge: "applied", cls: "det" };
  if (rec.status === "rejected" || rec.status === "accepted") return { done: true, badge: rec.status, cls: "det" };
  if (id != null) return { done: false, badge: "pending", cls: "muted" };
  return { done: false, badge: "", cls: "" };
}

function setReviewStatus(txt) {
  const c = $("review-findings");
  c.innerHTML = `<div class="empty">${esc(txt)}</div>`;
}

// Render the latest reviewer suggestions as "next steps" under the most recent
// assistant reply, right where the user is looking, with one-click rerun.
function attachNextSteps() {
  const ss = state._lastSuggestions || [];
  if (!ss.length) return;
  const msgs = $("messages").querySelectorAll(".msg.assistant");
  const last = msgs[msgs.length - 1];
  if (!last || last.querySelector(".next-steps")) return;
  const block = document.createElement("div");
  block.className = "next-steps";
  block.innerHTML = '<div class="next-steps-title">Suggested next steps</div>';
  for (const s of ss) {
    const title = (typeof s === "object" && s && s.title) ? s.title
      : (typeof s === "string" ? s : "");
    const prompt = (typeof s === "object" && s && s.prompt) ? s.prompt
      : (typeof s === "string" ? s : "");
    if (!title) continue;
    const row = document.createElement("div");
    row.className = "next-step";
    const st = suggestionStatus(s);
    row.innerHTML = `<span class="ns-title">${esc(title)}</span>`;
    if (st.badge) {
      const b = document.createElement("span");
      b.className = "sug-badge " + st.cls;
      b.textContent = st.badge;
      row.appendChild(b);
    }
    if (prompt && !st.done) {
      const btn = document.createElement("button");
      btn.className = "btn subtle small ns-run";
      btn.textContent = "Run";
      btn.addEventListener("click", () => sendChat(prompt, "rerun_suggestion", { suggestion_id: s.id }));
      row.appendChild(btn);
    }
    block.appendChild(row);
  }
  last.appendChild(block);
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
  document.querySelectorAll(".quick").forEach((b) => { b.disabled = busy; });
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
  if (curAssistantEl && curAssistantEl._renderTimer) {
    clearTimeout(curAssistantEl._renderTimer);
    curAssistantEl._renderTimer = null;
  }
  const el = ensureAssistant();
  el.body.innerHTML += `<p style="color:var(--danger)"><strong>Error:</strong> ${esc(msg)}</p>`;
  state.streaming = false;
  curAssistantEl = null;
  onTurnDone();
}

function setViewParam(kind) {
  const url = new URL(window.location.href);
  url.searchParams.set("flat", kind === "flat" ? "1" : "0");
  url.searchParams.set("sets", kind === "sets" ? "1" : "0");
  window.location.href = url.toString();
}

async function sendChat(textOverride, intent, extra) {
  const input = $("input");
  const text = textOverride !== undefined ? textOverride : input.value.trim();
  if ((!text && !intent) || state.busy) return;
  // Local UI switches (rendering mode).
  const t = text.trim();
  if (t === "/flat" || t === "/flat=1") { setViewParam("flat"); return; }
  if (t === "/sets" || t === "/sets=1") { setViewParam("sets"); return; }
  // Data inspection command: "@schema data.csv" renders a schema card inline
  // (no LLM round-trip) so the user can see columns/dtypes before prompting.
  if (/^@schema\b/i.test(t)) {
    const fname = t.replace(/^@schema\b/i, "").trim().split(/\s+/)[0] || "";
    if (textOverride === undefined) {
      input.value = "";
      autoResize(input);
    }
    if (fname) await showSchemaInline(fname);
    else toast("Usage: @schema <filename> (e.g. @schema data.csv)", 4000);
    return;
  }
  if (textOverride !== undefined) {
    input.value = "";
    autoResize(input);
  }
  state._lastSuggestions = null;
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
  dl.innerHTML = (state.models || []).map((m) => {
    const meta = modelMeta(m);
    const label = meta.hint ? `${m.id} · ${meta.hint}` : m.id;
    return `<option value="${esc(m.id)}">${esc(label)}</option>`;
  }).join("");
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
  if (!state.project) {
    const saved = localStorage.getItem("fox.project");
    state.project = state.projects.some((p) => p.name === saved) ? saved
      : (state.projects[0]?.name || "");
    if (!state.project) {
      await api("/api/projects", { method: "POST", body: JSON.stringify({ name: "default" }) });
      state.project = "default";
    }
  }
  renderSessionList();
}

function renderSessionList() {
  const cur = $("session-current");
  if (cur) cur.textContent = state.project || "—";
  const list = $("session-list");
  if (!list) return;
  list.innerHTML = state.projects.map((p) =>
    `<button class="session-item${p.name === state.project ? " active" : ""}" data-session="${esc(p.name)}">`
    + `<span class="session-item-ico">${p.name === state.project ? "▸" : "·"}</span>`
    + `<span class="session-item-name">${esc(p.name)}</span></button>`
  ).join("");
  list.querySelectorAll(".session-item").forEach((el) =>
    el.addEventListener("click", () => {
      closeSessionMenu();
      if (el.dataset.session !== state.project) switchProject(el.dataset.session);
    })
  );
}

function toggleSessionMenu(force) {
  const menu = $("session-menu");
  if (!menu) return;
  const open = force !== undefined ? force : menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !open);
  if (open) renderSessionList();
}

function closeSessionMenu() { toggleSessionMenu(false); }

async function switchProject(name) {
  if (state.ws) state.ws.close();
  state.project = name;
  localStorage.setItem("fox.project", name);
  const cur = $("session-current");
  if (cur) cur.textContent = name;
  renderSessionList();
  state.artifacts = [];
  $("messages").innerHTML = "";
  curAssistantEl = null;
  state._currentSet = null;
  state.expDetail = {};
  state.expRanking = {};
  state.activeExperiment = null;
  await refreshState();
  connect();
  startKernelPolling();
}

async function refreshState() {
  try {
    const r = await api(`/api/projects/${state.project}/state`);
    state.artifacts = r.artifacts || [];
    state.mgmtActivity = r.management_activity || null;
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
  fetchKernelStatus();
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

/* ---- data schema inspection (@schema command) ---- */

function schemaCardHtml(s) {
  if (!s || !s.columns || !s.columns.length) {
    return '<div class="muted">No schema available for this file.</div>';
  }
  const cols = s.columns.map((c) => `
    <div class="sc-col">
      <span class="sc-name" title="${esc(c.name)}">${esc(c.name)}</span>
      <span class="sc-dtype">${esc(c.dtype)}</span>
      <span class="sc-kind">${esc(c.kind)}</span>
      ${c.null_pct ? `<span class="sc-null">${Math.round(c.null_pct * 100)}% null</span>` : ""}
      <span class="sc-sample">${esc((c.sample || []).join(" · ") || "—")}</span>
    </div>`).join("");
  const prev = (s.preview && s.preview.length) ? `
    <table class="schema-preview"><thead><tr>
      ${s.columns.map((c) => `<th>${esc(c.name)}</th>`).join("")}
    </tr></thead><tbody>
      ${s.preview.map((row) => `<tr>${s.columns.map((c) => `<td>${esc(row[c.name] ?? "")}</td>`).join("")}</tr>`).join("")}
    </tbody></table>` : "";
  return `<div class="schema-head">
      <span class="sc-file">📋 ${esc(s.file)}</span>
      <span class="sc-rows">${s.rows} rows · ${s.columns.length} columns</span>
      <span class="spacer"></span>
      <button class="btn subtle small schema-copy" title="Copy schema as text">⧉</button>
    </div>
    <div class="schema-cols">${cols}</div>
    ${prev}`;
}

async function showSchemaInline(fname) {
  const userMsg = { content: "@schema " + fname, created_at: null };
  const set = msgSetCreate(userMsg, true);
  state._currentSet = set;
  const uel = msgContainer("user", [], null, set.body);
  uel.body.textContent = "@schema " + fname;
  const ael = msgContainer("assistant", [], null, set.body, "Fox · schema");
  const box = document.createElement("div");
  box.className = "schema-card";
  box.innerHTML = '<div class="empty">Reading schema…</div>';
  ael.body.appendChild(box);
  scrollBottom();
  try {
    const r = await api(`/api/projects/${state.project}/files/schema?fname=${encodeURIComponent(fname)}`);
    box.innerHTML = schemaCardHtml(r.schema);
  } catch (e) {
    box.classList.add("schema-err");
    box.innerHTML = `<span class="sev">error</span>${esc(e.message)}`;
  }
  const copyBtn = box.querySelector(".schema-copy");
  if (copyBtn) copyBtn.addEventListener("click", () => copyText(box.innerText));
  scrollBottom();
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
function attachGraphPan(wrap, key, getSvg, skipSel, opts) {
  const noWheel = !!(opts && opts.noWheel);
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
  if (noWheel) return st;
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

// Draggable nodes: grab a node and the graph re-settles around it live, with
// heavier (higher-weight) edges pulling their clusters together. Dragged
// nodes are pinned (fx/fy) until the next Relayout.
function attachGraphNodeDrag(wrap, getSvg, getNode) {
  const st = { moved: false, raf: 0, id: null };
  const toLocal = (e) => {
    const svg = getSvg();
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
    return { x: p.x, y: p.y };
  };
  wrap.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const el = e.target.closest ? e.target.closest("[data-node]") : null;
    if (!el) return;
    const nd = getNode(el.dataset.node);
    if (!nd) return;
    e.preventDefault();
    st.moved = false;
    st.id = nd.id;
    try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
    wrap.style.cursor = "grabbing";
    const p = toLocal(e);
    if (p) { nd.x = p.x; nd.y = p.y; nd.fx = p.x; nd.fy = p.y; graphRender(); }
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!st.id) return;
    const nd = getNode(st.id);
    if (!nd) return;
    const p = toLocal(e);
    if (!p) return;
    if (!st.moved && Math.hypot(p.x - nd.fx, p.y - nd.fy) > 4) st.moved = true;
    nd.x = p.x; nd.y = p.y; nd.fx = p.x; nd.fy = p.y;
    if (!st.raf) {
      st.raf = requestAnimationFrame(() => {
        st.raf = 0;
        graphStep(2);
        graphRender();
      });
    }
  });
  const end = (didSettle) => {
    if (!st.id) return;
    const didMove = st.moved;
    st.id = null;
    wrap.style.cursor = "";
    if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; }
    if (didMove && didSettle) {
      graphStep(80);
      graphRender();
    }
  };
  wrap.addEventListener("pointerup", () => end(true));
  wrap.addEventListener("pointercancel", () => { st.moved = false; end(false); });
  return st;
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
  vb: null, drag: null, k: 0, w: 960, h: 520, weightStrength: 100,
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

// Edge weights drive the force layout: tighter relations (cites / extends /
// improves / ...) pull their endpoints together more strongly than a loose
// `related_to`. A per-edge `weight` in the graph JSON overrides this.
const RELATION_WEIGHT = {
  cites: 1.6, references: 1.6, extends: 1.5, improves: 1.5, nests: 1.5,
  proposes: 1.4, introduces: 1.4, uses: 1.3, compares: 1.2, contrasts: 1.2,
  related_to: 1.0,
};

function graphNormalize(g) {
  const nodes = (g.nodes || []).map((n) => ({ ...n, degree: 0 }));
  const edges = (g.edges || [])
    .filter((e) => e && e.source != null && e.target != null)
    .map((e) => ({
      source: e.source,
      target: e.target,
      relation: e.relation || "",
      weight: e.weight != null ? Number(e.weight) || 1 : (RELATION_WEIGHT[e.relation] || 1),
    }));
  const byId = {};
  for (const n of nodes) byId[n.id] = n;
  for (const e of edges) {
    if (byId[e.source]) byId[e.source].degree += 1;
    if (byId[e.target]) byId[e.target].degree += 1;
  }
  return { nodes, edges, byId };
}

// Effective edge weight after applying the live "weight strength" slider.
function graphEffWeight(w) {
  const s = (GRAPH_STATE.weightStrength == null ? 100 : GRAPH_STATE.weightStrength) / 100;
  return 1 + (w - 1) * s;
}

// Deterministic layout so a graph renders identically on every open. Edge
// weights scale the spring attraction, and nodes pinned by dragging (fx/fy)
// stay where they were dropped while the rest re-settles around them.
function graphLayout(nodes, edges, w, h, byId, iters = 400) {
  const n = nodes.length;
  if (!n) return;
  let seed = 7;
  const rnd = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };
  const k = Math.sqrt((w * h) / Math.max(1, n)) * 1.4;
  GRAPH_STATE.k = k; GRAPH_STATE.w = w; GRAPH_STATE.h = h;
  nodes.forEach((nd, i) => {
    const a = (i / Math.max(1, n)) * Math.PI * 2;
    const rad = Math.min(w, h) * 0.32;
    nd.x = nd.fx != null ? nd.fx : w / 2 + rad * Math.cos(a) + (rnd() - 0.5) * 20;
    nd.y = nd.fy != null ? nd.fy : h / 2 + rad * Math.sin(a) + (rnd() - 0.5) * 20;
    nd.vx = 0; nd.vy = 0;
  });
  graphStep(iters);
}

// Run `iters` force-simulation steps over the current node positions, with
// edge weights pulling endpoints together and dragged (pinned) nodes fixed.
function graphStep(iters) {
  const nodes = GRAPH_STATE.nodes;
  const n = nodes.length;
  if (!n) return;
  const w = GRAPH_STATE.w || 960, h = GRAPH_STATE.h || 520;
  const k = GRAPH_STATE.k || Math.sqrt((w * h) / n) * 1.4;
  const visible = nodes.filter((nd) => GRAPH_STATE.typeOn[nd.type] !== false);
  const vset = new Set(visible.map((nd) => nd.id));
  const edges = GRAPH_STATE.edges.filter((e) => vset.has(e.source) && vset.has(e.target));
  const pinned = (nd) => nd.fx != null && nd.fy != null;
  for (let it = 0; it < iters; it++) {
    const temp = Math.max(0.04, 0.8 * (1 - it / Math.max(1, iters)));
    // repulsion between every visible pair (pinned pairs stay put)
    for (let i = 0; i < visible.length; i++) {
      const a = visible[i];
      for (let j = i + 1; j < visible.length; j++) {
        const b = visible[j];
        if (pinned(a) && pinned(b)) continue;
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
        const f = (k * k) / d2;
        const fx = dx / Math.sqrt(d2) * f;
        const fy = dy / Math.sqrt(d2) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    // weighted spring attraction along edges
    for (const e of edges) {
      const a = GRAPH_STATE.byId[e.source], b = GRAPH_STATE.byId[e.target];
      if (!a || !b || a === b) continue;
      const we = graphEffWeight(e.weight == null ? 1 : e.weight);
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d * d) / k * we;
      if (!pinned(a)) { a.vx += (dx / d) * f; a.vy += (dy / d) * f; }
      if (!pinned(b)) { b.vx -= (dx / d) * f; b.vy -= (dy / d) * f; }
    }
    // gravity to keep the layout centred
    for (const nd of visible) {
      if (pinned(nd)) continue;
      nd.vx += (w / 2 - nd.x) * 0.008;
      nd.vy += (h / 2 - nd.y) * 0.008;
    }
    // integrate + clamp velocity
    for (const nd of visible) {
      if (pinned(nd)) continue;
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
  const visible = GRAPH_STATE.nodes.filter((n) => GRAPH_STATE.typeOn[n.type] !== false);
  const vset = new Set(visible.map((n) => n.id));
  const edges = GRAPH_STATE.edges.filter((e) => vset.has(e.source) && vset.has(e.target));
  graphLayout(visible, edges, 960, 520, GRAPH_STATE.byId);
  graphRender();
}

function graphRender() {
  const svg = $("graph-svg");
  if (!svg || !GRAPH_STATE.nodes.length) return;
  const wrap = $("graph-svg-wrap");
  const W = GRAPH_STATE.w || 960, H = GRAPH_STATE.h || 520;
  const visible = GRAPH_STATE.nodes.filter((n) => GRAPH_STATE.typeOn[n.type] !== false);
  const vset = new Set(visible.map((n) => n.id));
  const edges = GRAPH_STATE.edges.filter((e) => vset.has(e.source) && vset.has(e.target));

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
    const we = graphEffWeight(e.weight == null ? 1 : e.weight);
    const sw = (0.6 + we * 0.8).toFixed(2);
    s += `<line class="${cls}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" marker-end="url(#gx-arrow)" style="stroke-width:${sw}px"><title>${esc(a.id)} —${esc(e.relation)}→ ${esc(b.id)} (weight ${we.toFixed(2)})</title></line>`;
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
  graphRender();
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
      graphRender();
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
  const ws = $("graph-weights");
  if (ws) GRAPH_STATE.weightStrength = Number(ws.value);
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
$("graph-relayout").addEventListener("click", () => {
  GRAPH_STATE.nodes.forEach((nd) => { nd.fx = null; nd.fy = null; });
  graphRerender();
});
$("graph-weights").addEventListener("input", (e) => {
  GRAPH_STATE.weightStrength = Number(e.target.value);
  graphStep(60);
  graphRender();
});
$("graph-svg").addEventListener("click", (e) => {
  if (graphPan.drag && graphPan.drag.moved) { graphPan.drag.moved = false; return; }
  if (graphDrag && graphDrag.moved) { graphDrag.moved = false; return; }
  const el = e.target.closest ? e.target.closest("[data-node]") : null;
  if (el) graphSelect(el.dataset.node);
  else graphSelect(null);
});

const graphWrap = $("graph-svg-wrap");
const graphPan = attachGraphPan(graphWrap, "graph-svg-wrap", () => $("graph-svg"), "[data-node]");
attachGraphControls(graphWrap, "graph-svg-wrap", () => $("graph-svg"), 960, 520);
const graphDrag = attachGraphNodeDrag(graphWrap, () => $("graph-svg"), (id) => GRAPH_STATE.byId[id]);

function flatMode() {
  // Flat (plain bubbles) is the default; grouped sets are opt-in via ?sets=1.
  try {
    const q = window.location.search || "";
    if (/[?&]sets=1/.test(q)) return false;
    return true;
  } catch (e) { return true; }
}

// Plain per-message rendering (no grouping) — used with ?flat=1 as a fallback.
function renderMessagesFlat(msgs, wrap) {
  let turnUser = "";
  msgs.forEach((m, i) => {
    const mtags = (m.meta && m.meta.tags) || [];
    if (m.role === "user") {
      const el = msgContainer("user", mtags, m.created_at, undefined, undefined, m.id);
      el.body.textContent = m.content;
      tagMessageExperiment(el, m.meta && m.meta.experiment_id);
      turnUser = m.id;
    } else if (m.role === "assistant") {
      if (!(m.content || "").trim()) return;
      const el = msgContainer("assistant", mtags, m.created_at, undefined,
        foxSenderLabel(m.meta && m.meta.mcp_name, m.meta && m.meta.action,
                       m.meta && m.meta.model), m.id);
      el.body.innerHTML = renderMarkdown(m.content);
      enhanceCodeBlocks(el.body);
      maybeAttachRepoButtons(el, mtags);
      tagMessageExperiment(el, m.meta && m.meta.experiment_id);
      const next = msgs[i + 1];
      if ((!next || next.role === "user") && turnUser) attachTurnArtifacts(turnUser, el.div);
    } else if (m.role === "tool") {
      const meta = m.meta || {};
      const card = document.createElement("div");
      card.className = "toolcard";
      card.innerHTML = `
        <div class="toolcard-head">
          <span class="caret">▶</span><span class="tname">${esc(foxToolName(meta.name || "tool"))}</span>
          <span class="tstatus ok">persisted</span>
        </div>
        <div class="toolcard-body"><pre>${esc(truncate(m.content || "", 2000))}</pre></div>`;
      card.querySelector(".toolcard-head").addEventListener("click", () => card.classList.toggle("open"));
      wrap.appendChild(card);
    }
  });
  attachNextSteps();
}

function renderMessages(msgs) {
  const wrap = $("messages");
  wrap.innerHTML = "";
  if (flatMode()) {
    renderMessagesFlat(msgs || [], wrap);
    const st = $("chat-stats");
    if (st) st.textContent = (window.FOX_VER || "?") + " · flat mode";
    if (state._tagFilter) applyTagFilter();
    scrollToLatest();
    return;
  }
  let lastDay = "";
  let turnUser = "";
  let currentSet = null;
  let setCount = 0;
  let errCount = 0;
  for (let i = 0; i < msgs.length; i++) {
    try {
      const m = msgs[i];
      const mtags = (m.meta && m.meta.tags) || [];
      if (m.role === "user") {
        const day = fmtDay(m.created_at);
        if (day && day !== lastDay) {
          const sep = document.createElement("div");
          sep.className = "day-sep";
          sep.textContent = day;
          wrap.appendChild(sep);
          lastDay = day;
        }
        currentSet = msgSetCreate(m, true);
        setCount++;
        const el = msgContainer("user", mtags, m.created_at, currentSet.body, undefined, m.id);
        el.body.textContent = m.content;
        tagMessageExperiment(el, m.meta && m.meta.experiment_id);
        turnUser = m.id;
        continue;
      }
      if (!currentSet) currentSet = msgSetCreate(null, true);
      if (m.role === "assistant") {
        // Drop empty bubbles (intermediate tool-call rows carry no text; the
        // tool cards below represent the steps).
        if (!(m.content || "").trim()) continue;
        currentSet.setState.preview = String(m.content).replace(/\s+/g, " ").slice(0, 70);
        currentSet.update();
        const el = msgContainer("assistant", mtags, m.created_at, currentSet.body,
          foxSenderLabel(m.meta && m.meta.mcp_name, m.meta && m.meta.action,
                         m.meta && m.meta.model), m.id);
        el.body.innerHTML = renderMarkdown(m.content);
        enhanceCodeBlocks(el.body);
        maybeAttachRepoButtons(el, mtags);
        tagMessageExperiment(el, m.meta && m.meta.experiment_id);
        // Re-attach figures produced during this turn to the final assistant
        // reply of the turn, so charts survive refreshState() re-renders.
        const next = msgs[i + 1];
        const isFinal = !next || next.role === "user";
        if (isFinal && turnUser) attachTurnArtifacts(turnUser, el.div);
      } else if (m.role === "tool") {
        const meta = m.meta || {};
        const card = document.createElement("div");
        card.className = "toolcard";
        card.innerHTML = `
          <div class="toolcard-head">
            <span class="caret">▶</span><span class="tname">${esc(foxToolName(meta.name || "tool"))}</span>
            <span class="tstatus ok">persisted</span>
          </div>
          <div class="toolcard-body"><pre>${esc(truncate(m.content || "", 2000))}</pre></div>`;
        card.querySelector(".toolcard-head").addEventListener("click", () => card.classList.toggle("open"));
        currentSet.body.appendChild(card);
      }
    } catch (e) {
      errCount++;
      // Never blank the conversation: log and fall back to a flat bubble.
      console.error("renderMessages: message", msgs[i] && msgs[i].id, e);
      try {
        const fm = msgs[i];
        if (!fm) continue;
        const el = msgContainer(fm.role === "user" ? "user" : "assistant",
                                (fm.meta && fm.meta.tags) || [], fm.created_at,
                                undefined,
                                foxSenderLabel(fm.meta && fm.meta.mcp_name,
                                               fm.meta && fm.meta.action,
                                               fm.meta && fm.meta.model),
                                fm.id);
        if (fm.role === "user") { el.body.textContent = fm.content || ""; turnUser = fm.id; }
        else el.body.innerHTML = renderMarkdown(fm.content || "");
      } catch (e2) { /* give up on this one */ }
    }
  }
  const stats = $("chat-stats");
  if (stats) {
    const bodyN = (() => { let n = 0; wrap.querySelectorAll(".mset-body").forEach((b) => n += b.children.length); return n; })();
    stats.textContent = (window.FOX_VER || "?") + " · " + setCount + " conversation set(s) · " +
      (msgs || []).length + " message(s) · " + bodyN + " rendered" + (errCount ? " · " + errCount + " error(s)" : "");
  }
  if (state._tagFilter) applyTagFilter();
  scrollToLatest();
}

function attachTurnArtifacts(turnUserMsgId, div) {
  const arts = (state.artifacts || []).filter((a) =>
    a.data_type === "png" && String(a.message_id) === String(turnUserMsgId));
  for (const a of arts) appendInlineFig({ div }, a);
}

/* ============================ model refresh ============================== */

// Friendly metadata for a model id so the dropdown is more than a raw id list.
const MODEL_FAMILIES = [
  { re: /qwen/i, family: "Qwen", provider: "Alibaba" },
  { re: /llama/i, family: "Llama", provider: "Meta" },
  { re: /mistral/i, family: "Mistral", provider: "Mistral AI" },
  { re: /mixtral/i, family: "Mixtral", provider: "Mistral AI" },
  { re: /codestral/i, family: "Codestral", provider: "Mistral AI" },
  { re: /deepseek/i, family: "DeepSeek", provider: "DeepSeek" },
  { re: /gemini/i, family: "Gemini", provider: "Google" },
  { re: /gemma/i, family: "Gemma", provider: "Google" },
  { re: /gpt/i, family: "GPT", provider: "OpenAI" },
  { re: /claude/i, family: "Claude", provider: "Anthropic" },
  { re: /phi/i, family: "Phi", provider: "Microsoft" },
  { re: /granite/i, family: "Granite", provider: "IBM" },
  { re: /command\b/i, family: "Command", provider: "Cohere" },
  { re: /aya/i, family: "Aya", provider: "Cohere" },
  { re: /starcoder/i, family: "StarCoder", provider: "BigCode" },
  { re: /codellama/i, family: "CodeLlama", provider: "Meta" },
  { re: /llava/i, family: "LLaVA", provider: "UW-Madison" },
  { re: /nomic/i, family: "Nomic", provider: "Nomic AI" },
  { re: /dbrx/i, family: "DBRX", provider: "Databricks" },
  { re: /solar/i, family: "Solar", provider: "Upstage" },
  { re: /openchat/i, family: "OpenChat", provider: "OpenChat" },
  { re: /dolphin/i, family: "Dolphin", provider: "Cognitive" },
];

function modelMeta(m) {
  const s = String((m && m.id) || "");
  let family = "";
  let provider = (m && m.owned_by) || "";
  for (const f of MODEL_FAMILIES) {
    if (f.re.test(s)) { family = f.family; provider = provider || f.provider; break; }
  }
  let size = (m && m.size) || "";
  if (!size) {
    const sizeM = /(?:^|[:_-])(\d+(?:\.\d+)?)b/i.exec(s);
    if (sizeM) size = sizeM[1] + "B";
  }
  const tags = [];
  if (/instruct|[-_]it\b/i.test(s)) tags.push("instruct");
  const recommended = isCurrentModel(s);
  const hint = [family, size, (m && m.quantization) || ""].filter(Boolean).join(" ");
  return { family: family || "Other", provider, size, tags, hint, recommended };
}

function isCurrentModel(id) {
  try { return state.config && state.config.llm && state.config.llm.model === id; }
  catch (e) { return false; }
}

function renderModelSelect() {
  const sel = $("model-select");
  if (!sel) return;
  const models = state.models || [];
  if (!models.length) { sel.innerHTML = ""; return; }
  const groups = {};
  for (const m of models) {
    const meta = modelMeta(m);
    const key = meta.recommended ? "★ Current" : meta.family;
    (groups[key] = groups[key] || []).push({ m, meta });
  }
  const keys = Object.keys(groups).sort((a, b) => {
    if (a === "★ Current") return -1;
    if (b === "★ Current") return 1;
    return a.localeCompare(b);
  });
  sel.innerHTML = keys.map((k) => {
    const opts = groups[k].map(({ m, meta }) => {
      const label = meta.hint ? `${m.id} · ${meta.hint}` : m.id;
      return `<option value="${esc(m.id)}">${esc(label)}</option>`;
    }).join("");
    return `<optgroup label="${esc(k)}">${opts}</optgroup>`;
  }).join("");
  if (state.config?.llm?.model && models.some((m) => m.id === state.config.llm.model)) {
    sel.value = state.config.llm.model;
  }
}

async function refreshModels() {
  const sel = $("model-select");
  try {
    const r = await api("/api/models");
    state.models = r.models || [];
    renderModelSelect();
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
  b.addEventListener("click", () => {
    let extra = null;
    if (b.dataset.extra) { try { extra = JSON.parse(b.dataset.extra); } catch (e) { extra = null; } }
    sendChat(b.dataset.text || "", b.dataset.intent || "", extra);
  }));
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

/* ---- composer file attach (📎) ---- */
$("attach-btn").addEventListener("click", () => $("chat-attach-input").click());
$("chat-attach-input").addEventListener("change", async () => {
  const input = $("chat-attach-input");
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
    const ta = $("input");
    const hint = `I attached ${file.name} — it is saved in the project as ${file.name}. Inspect it with @schema ${file.name}, or ask Fox to load and analyze it.`;
    ta.value = (ta.value ? ta.value.trimEnd() + "\n" : "") + hint;
    autoResize(ta);
    ta.focus();
    toast(`Uploaded ${file.name} — data hint added to your message.`);
    loadFiles();
  } catch (e) { toast("Upload failed: " + e.message, 4000); }
  input.value = "";
});

/* ---- configurable quick-actions tray (custom shortcuts) ---- */
function qaLoad() {
  try { state.customActions = JSON.parse(localStorage.getItem("fox.quickActions") || "[]"); }
  catch (e) { state.customActions = []; }
  if (!Array.isArray(state.customActions)) state.customActions = [];
}
function qaSave() {
  try { localStorage.setItem("fox.quickActions", JSON.stringify(state.customActions || [])); }
  catch (e) { /* storage unavailable */ }
}
function qaRender() {
  const wrap = $("quick-actions");
  if (!wrap) return;
  wrap.querySelectorAll(".qa-custom").forEach((el) => el.remove());
  (state.customActions || []).forEach((a, i) => {
    const row = document.createElement("span");
    row.className = "qa-custom";
    const b = document.createElement("button");
    b.className = "btn subtle small quick";
    b.textContent = a.label || a.text || "shortcut";
    b.title = a.text || "";
    b.addEventListener("click", () => sendChat(a.text || "", a.intent || "", null));
    const rm = document.createElement("button");
    rm.className = "qa-remove";
    rm.textContent = "✕";
    rm.title = "Remove this shortcut";
    rm.addEventListener("click", (e) => {
      e.stopPropagation();
      state.customActions.splice(i, 1);
      qaSave();
      qaRender();
    });
    row.appendChild(b);
    row.appendChild(rm);
    wrap.appendChild(row);
  });
}
$("qa-add").addEventListener("click", () => {
  const label = prompt("Shortcut label (e.g. 'Plot loss curves'):");
  if (!label) return;
  const text = prompt("Prompt text sent to Fox:", label);
  if (!text) return;
  state.customActions = state.customActions || [];
  state.customActions.push({ label, text });
  qaSave();
  qaRender();
  toast("Shortcut added.");
});
$("qa-reset").addEventListener("click", () => {
  if (!confirm("Remove all custom shortcuts?")) return;
  state.customActions = [];
  qaSave();
  qaRender();
  toast("Custom shortcuts cleared.");
});
qaLoad();
qaRender();
// Copy a message's text from its ⧉ button.
$("messages").addEventListener("click", (e) => {
  const btn = e.target.closest(".msg-copy");
  if (!btn) return;
  const msg = btn.closest(".msg");
  const body = msg && msg.querySelector(".msg-body");
  copyText(body ? body.innerText : "");
});
// Clicking a message-set header expands/collapses it (delegated so it always works).
$("messages").addEventListener("click", (e) => {
  const head = e.target.closest(".mset-head");
  if (head) {
    const set = head.closest(".msg-set");
    if (set) set.classList.toggle("collapsed");
    return;
  }
  const chip = e.target.closest(".msg-exp");
  if (!chip) return;
  const eid = parseInt(chip.dataset.eid, 10);
  if (!eid) return;
  focusExperiment(eid);
});
// Clicking a tag badge (e.g. an MCP server or action) filters the chat to the
// messages carrying that tag, so tool provenance is navigable at a glance.
$("messages").addEventListener("click", (e) => {
  const tag = e.target.closest(".m-tag");
  if (!tag) return;
  const value = tag.textContent.trim();
  state._tagFilter = state._tagFilter === value ? null : value;
  applyTagFilter();
});

function applyTagFilter() {
  const value = state._tagFilter;
  const any = state._tagFilter != null;
  $("messages").querySelectorAll(".msg").forEach((m) => {
    const has = Array.from(m.querySelectorAll(".m-tag"))
      .some((t) => t.textContent.trim() === value);
    m.classList.toggle("tag-dim", any && !has);
    m.classList.toggle("tag-hot", any && has);
  });
}
$("session-switch").addEventListener("click", (e) => { e.stopPropagation(); toggleSessionMenu(); });
$("session-new").addEventListener("click", async () => {
  const name = prompt("New session name:");
  if (!name) return;
  try {
    await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects();
    await switchProject(name);
    closeSessionMenu();
  } catch (e) { toast(e.message); }
});
$("session-fork").addEventListener("click", async () => {
  const name = prompt("Fork '" + state.project + "' as (new session name):", state.project + "-fork");
  if (!name) return;
  try {
    const r = await api(`/api/projects/${encodeURIComponent(state.project)}/fork`,
                        { method: "POST", body: JSON.stringify({ name }) });
    await loadProjects();
    await switchProject(r.name);
    toast("Forked session as '" + r.name + "'");
    closeSessionMenu();
  } catch (e) { toast(e.message); }
});
$("session-del").addEventListener("click", async () => {
  if (!confirm(`Delete session '${state.project}'? This removes its messages, runs, artifacts and files.`)) return;
  try {
    await api(`/api/projects/${encodeURIComponent(state.project)}`, { method: "DELETE" });
    await loadProjects();
    await switchProject(state.projects.length ? state.projects[0].name : "default");
    toast("Session deleted");
    closeSessionMenu();
  } catch (e) { toast(e.message); }
});
document.addEventListener("click", (e) => {
  const ctrl = $("session-ctrl");
  if (ctrl && !ctrl.contains(e.target)) closeSessionMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeSessionMenu();
    const ov = $("branch-overlay");
    if (ov && !ov.classList.contains("hidden")) {
      ov.classList.add("hidden");
      $("branch-toggle").classList.remove("active");
    }
  }
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
$("theme-toggle").addEventListener("click", toggleTheme);
$("brand").addEventListener("click", () => {
  window.location.href = (window.FOX_BASE || "") + "/features.html";
});
$("docs-btn").addEventListener("click", () => {
  window.open((window.FOX_BASE || "") + "/gitbook/", "_blank");
});
// The Settings-modal help link is a static anchor in index.html; make it
// FOX_BASE-aware so it also works behind a path prefix (e.g. /fox in Jupyter).
const settingsHelp = $("settings-help");
if (settingsHelp) settingsHelp.href = (window.FOX_BASE || "") + "/gitbook/";
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
  h.innerHTML = `<h1>Local - Open - Agentic Experimentation Workbench · chat transcript</h1>`
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
  loadSuggestions();
  loadLearnings();
  loadCompareExperiments();
  try {
    const r = await api(`/api/projects/${state.project}/experiments/history?limit=2000`);
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
  try {
    const fc = await api(`/api/projects/${state.project}/experiments/focus`);
    state.focusExperiment = fc.focus_id;
    renderExpList();
  } catch (e) { /* silent */ }
  await loadExpRankings();
  try {
    const rr = await api(`/api/projects/${state.project}/runs?limit=2000`);
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
  renderExpKpis();
}

async function loadSuggestions() {
  try {
    const r = await api(`/api/projects/${state.project}/suggestions`);
    const map = {};
    for (const s of r.suggestions || []) map[s.id] = s;
    state.suggestions = map;
  } catch (e) { /* silent */ }
}

async function loadLearnings() {
  try {
    const r = await api(`/api/projects/${state.project}/learnings`);
    state.learnings = (r.learnings || []).reduce((m, l) => {
      const k = l.experiment_id != null ? String(l.experiment_id) : "_";
      (m[k] = m[k] || []).push(l);
      return m;
    }, {});
    renderExpKpis();
  } catch (e) { state.learnings = state.learnings || {}; }
}

async function loadCompareExperiments() {
  const el = $("exp-compare-leaderboard");
  if (!el) return;
  try {
    const r = await api(`/api/projects/${state.project}/experiments/compare`);
    const rows = r.rows || [];
    if (!rows.length) { el.innerHTML = '<div class="exp-empty">Set a goal metric on experiments to compare them.</div>'; return; }
    const metric = r.metric || "metric";
    let h = `<table class="exp-rank-table"><thead><tr><th>#</th><th>experiment</th><th>${esc(metric)}</th><th>Δ best</th><th>% target</th><th>status</th></tr></thead><tbody>`;
    rows.forEach((row, i) => {
      const rank = i + 1;
      const best = row.best != null ? _fmtNum(row.best) : "—";
      const db = row.delta_best != null ? ((row.delta_best >= 0 ? "+" : "") + _fmtNum(row.delta_best)) : "—";
      const pt = (row.to_target != null && row.best != null)
        ? (row.target != null ? Math.round((row.best / row.target) * 100) + "%" : "—")
        : "—";
      h += `<tr${rank === 1 ? ' class="rank-top lb-row"' : ' class="lb-row"'} data-eid="${row.id}" title="Open this experiment">
        <td class="rank-pos">${rank}${rank === 1 ? " 🏆" : ""}</td>` +
        `<td>${esc(row.name)}</td><td>${best}</td><td class="muted">${db}</td><td class="muted">${pt}</td>` +
        `<td><span class="exp-badge ${row.status === "active" ? "det" : row.status === "completed" ? "ok" : "warn"}">${esc(row.status)}</span></td></tr>`;
    });
    h += "</tbody></table>";
    el.innerHTML = h;
    el.querySelectorAll(".lb-row").forEach((tr) =>
      tr.addEventListener("click", () => revealExpCard(Number(tr.dataset.eid))));
  } catch (e) { el.innerHTML = '<div class="muted">Compare failed: ' + esc(e.message) + "</div>"; }
}

function revealExpCard(eid) {
  // Clear any experiment search filter so the card is reachable, then expand +
  // scroll to it with a flash.
  if (state.expSearch) {
    state.expSearch = "";
    const s = $("exp-search");
    if (s) s.value = "";
  }
  renderExpList();
  const list = $("exp-list");
  const card = list && list.querySelector(`.exp-card[data-id="${eid}"]`);
  if (!card) { toast("Experiment not found in the list."); return; }
  const detail = card.querySelector(".exp-card-detail");
  if (detail && detail.classList.contains("hidden")) {
    const btn = card.querySelector(".exp-details");
    if (btn) btn.click();
  }
  const nav = $("exp-section-experiments");
  if (nav) nav.scrollIntoView({ behavior: "smooth", block: "start" });
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("exp-flash");
  setTimeout(() => card.classList.remove("exp-flash"), 1800);
}

/* ============================ experiments overview (round 13) ============= */

function goalReachedLocal(g) {
  const metric = g && g.metric;
  const target = g && g.target;
  if (!metric || target == null) return false;
  const higher = !!(g && g.higher_better);
  let best = null;
  for (const r of (state.agentRuns || [])) {
    const v = (r.metrics || {})[metric];
    if (v == null || Number.isNaN(Number(v))) continue;
    const n = Number(v);
    if (best === null || (higher ? n > best : n < best)) best = n;
  }
  return best !== null && (higher ? best >= target : best <= target);
}

function goalProgress(g) {
  const metric = g && g.metric;
  const target = g && g.target;
  const higher = !!(g && g.higher_better);
  const eid = g && g.experiment_id != null ? String(g.experiment_id) : null;
  let best = null;
  for (const r of (state.agentRuns || [])) {
    if (eid && String(r.experiment_id) !== eid) continue;
    const v = (r.metrics || {})[metric];
    if (v == null || Number.isNaN(Number(v))) continue;
    const n = Number(v);
    if (best === null || (higher ? n > best : n < best)) best = n;
  }
  if (best == null || target == null) return { best, pct: 0, reached: false, delta: null };
  const reached = higher ? best >= target : best <= target;
  // pct of target for the progress bar; >100 clamped in the UI.
  const pct = target ? (best / target) * 100 : 0;
  const delta = higher ? target - best : best - target;
  return { best, pct, reached, delta: _fmtNum(Math.abs(delta)) + (higher ? "↑" : "↓") };
}

function renderExpKpis() {
  const el = $("exp-kpis");
  if (!el) return;
  const learnings = Object.values(state.learnings || {}).reduce((n, a) => n + (a ? a.length : 0), 0);
  const openGoals = (state.goals || []).filter((g) => !goalReachedLocal(g)).length;
  const kpis = [
    ["Experiments", (state.expList || []).length, "experiments"],
    ["Runs", (state.agentRuns || []).length, "runs"],
    ["Campaigns", (state.campaigns || []).length, "campaigns"],
    ["Benchmarks", (state.evals || []).length, "benchmarks"],
    ["Learnings", learnings, "experiments"],
    ["Open goals", openGoals, "goals"],
  ];
  el.innerHTML = kpis.map(([l, n, target]) =>
    `<div class="exp-kpi" data-target="${target}" title="Jump to ${target}"><div class="kpi-n">${n}</div><div class="kpi-l">${esc(l)}</div></div>`).join("");
  el.querySelectorAll(".exp-kpi").forEach((k) =>
    k.addEventListener("click", () => {
      const sec = $("exp-section-" + k.dataset.target);
      if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      k.classList.add("exp-flash");
      setTimeout(() => k.classList.remove("exp-flash"), 1200);
    }));
}

function initExpSectionNav() {
  const chips = document.querySelectorAll(".exp-nav-chip");
  const observed = [];
  chips.forEach((chip) => {
    const sec = $("exp-section-" + chip.dataset.target);
    if (sec) observed.push({ chip, sec });
    chip.addEventListener("click", () => {
      const target = $("exp-section-" + chip.dataset.target);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      setExpParam("section", chip.dataset.target);
    });
  });
  if (!("IntersectionObserver" in window)) return;
  const obs = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting) {
        const target = (e.target.id || "").replace("exp-section-", "");
        chips.forEach((c) => c.classList.toggle("active", c.dataset.target === target));
      }
    });
  }, { rootMargin: "-70px 0px -72% 0px" });
  observed.forEach((o) => obs.observe(o.sec));
}

/* ---------- shareable URLs + keyboard navigation (experiments tab) ---------- */

// Record the active section / run in the URL (?view=experiments&section=&run=)
// so a refresh — or a pasted link — restores the exact place in the tab.
function setExpParam(key, value) {
  try {
    const url = new URL(window.location.href);
    if (value) url.searchParams.set(key, String(value));
    else url.searchParams.delete(key);
    if (url.searchParams.get("view") !== "experiments")
      url.searchParams.set("view", "experiments");
    history.replaceState(null, "", url.toString());
  } catch (e) { /* ignore */ }
}

function expandRunById(rid) {
  const el = $("runs-list");
  if (!el || rid == null) return;
  const row = el.querySelector(`.run-row[data-id="${rid}"]`);
  if (!row) { toast("Run #" + rid + " isn't in the loaded list."); return; }
  const head = row.querySelector(".run-row-head");
  if (head && !row.classList.contains("open")) head.click();
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("exp-flash");
  setTimeout(() => row.classList.remove("exp-flash"), 1800);
}

// After the tab's data is loaded, honor ?view=experiments[&section=&run=].
function expDeepLink() {
  let q;
  try { q = new URLSearchParams(window.location.search); } catch (e) { return; }
  if (q.get("view") !== "experiments") return;
  switchMainView("experiments");
  const section = q.get("section");
  if (section) setTimeout(() => {
    const s = $("exp-section-" + section);
    if (s) s.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 250);
  const run = q.get("run");
  if (run) setTimeout(() => expandRunById(Number(run)), 400);
}

// Single-key jump between Experiments-tab sections (ignored while typing).
const EXP_KEY_SECTIONS = {
  o: "overview", g: "goals", c: "chart", x: "experiments",
  m: "campaigns", b: "benchmarks", r: "runs",
};
function setupExpKeyboard() {
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    const tag = (t && (t.tagName || "")).toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" ||
        (t && t.isContentEditable) || e.altKey || e.ctrlKey || e.metaKey) return;
    const panel = $("exp-panel");
    if (!panel || panel.classList.contains("hidden")) return;
    const sec = EXP_KEY_SECTIONS[e.key];
    if (sec) {
      e.preventDefault();
      const s = $("exp-section-" + sec);
      if (s) s.scrollIntoView({ behavior: "smooth", block: "start" });
      setExpParam("section", sec);
    }
  });
}


/* ============================ evals (round 9) ============================= */

let evalPollTimer = null;

function startEvalPoll() {
  if (evalPollTimer) return;
  evalPollTimer = setInterval(async () => {
    await loadEvals();
    if (!state.evalRunning) { clearInterval(evalPollTimer); evalPollTimer = null; }
  }, 3000);
}

async function loadEvals() {
  try {
    const r = await api(`/api/projects/${state.project}/evals`);
    state.evals = r.evals || [];
    state.evalRunning = !!r.running;
    renderEvals();
    renderExpKpis();
  } catch (e) { /* silent */ }
}

function renderEvals() {
  const el = $("eval-list");
  if (!el) return;
  const es = state.evals || [];
  if (!es.length) { el.innerHTML = '<div class="empty">No benchmarks yet — compare models on a task.</div>'; return; }
  el.innerHTML = "";
  for (const ev of es) {
    const running = ev.status === "running";
    const done = ev.status === "done";
    const report = (ev.report || "").replace(/\s+/g, " ").slice(0, 600);
    const models = (ev.models || []).slice(0, 6);
    const prog = running ? "running" : (done ? "full" : "empty");
    const card = document.createElement("div");
    card.className = "exp-card";
    card.innerHTML = `<div class="exp-card-head">
        <b class="exp-card-name">${esc(ev.name)}</b>
        ${campaignStatusBadge(ev.status)}
        <span class="muted exp-card-runs">${ev.models.length} model(s)</span>
        <span class="spacer"></span>
        ${running ? `<button class="btn subtle small eval-stop" data-id="${ev.id}">⏹ Stop</button>`
          : `<button class="btn subtle small eval-run" data-id="${ev.id}">▶ ${done ? "Rerun" : "Run"}</button>`}
      </div>
      <div class="exp-card-progress ${prog}"></div>
      ${ev.prompt ? `<div class="exp-card-hyp muted">${esc(ev.prompt)}</div>` : ""}
      ${models.length ? `<div class="run-tools">${models.map((m) => `<span class="run-tool-chip">${esc(m)}</span>`).join("")}</div>` : ""}
      ${ev.report ? `<details class="exp-plan"><summary>Leaderboard</summary><div class="exp-plan-body">${esc(report)}</div></details>` : ""}`;
    el.appendChild(card);
  }
  el.querySelectorAll(".eval-run").forEach((b) => b.addEventListener("click", async () => {
    try {
      await api(`/api/projects/${state.project}/evals/${b.dataset.id}/run`, { method: "POST" });
      toast("Model benchmark started in the background.");
      await loadEvals();
      startEvalPoll();
    } catch (e) { toast("Failed to start eval: " + e.message); }
  }));
  el.querySelectorAll(".eval-stop").forEach((b) => b.addEventListener("click", async () => {
    try {
      await api(`/api/projects/${state.project}/evals/${b.dataset.id}/stop`, { method: "POST" });
      toast("Stop requested.");
    } catch (e) { toast("Failed to stop eval: " + e.message); }
  }));
}

function learningsHtml(eid) {
  const ls = (state.learnings || {})[eid != null ? String(eid) : "_"] || [];
  if (!ls.length) return "";
  const items = ls.map((l) => {
    const cls = l.improved === 1 ? "ok" : (l.improved === 0 ? "warn" : "det");
    const badge = l.improved === 1 ? "✓ improved" : (l.improved === 0 ? "✗ no gain" : "");
    return `<div class="learning-row"><span class="sug-badge ${cls}">${badge}</span><span>${esc(l.summary)}</span></div>`;
  }).join("");
  return `<details class="exp-plan"><summary>Learnings (${ls.length})</summary><div class="exp-plan-body">${items}</div></details>`;
}

/* ============================ campaigns (round 6) ========================= */

let campaignPollTimer = null;

function startCampaignPoll() {
  if (campaignPollTimer) return;
  campaignPollTimer = setInterval(async () => {
    await loadCampaigns();
    if (!state.campaignRunning) {
      clearInterval(campaignPollTimer);
      campaignPollTimer = null;
    }
  }, 3000);
}

function campaignStatusBadge(s) {
  const map = { planned: "det", running: "warn", done: "ok", failed: "warn" };
  return `<span class="exp-badge ${map[s] || "det"}">${esc(s)}</span>`;
}

async function loadCampaigns() {
  try {
    const r = await api(`/api/projects/${state.project}/campaigns`);
    state.campaigns = r.campaigns || [];
    state.campaignRunning = !!r.running;
    renderCampaigns();
    renderExpKpis();
  } catch (e) { /* silent */ }
}

function renderCampaigns() {
  const el = $("campaign-list");
  if (!el) return;
  const cs = state.campaigns || [];
  if (!cs.length) {
    el.innerHTML = '<div class="empty">No campaigns yet — start one to run a multi-step investigation in the background.</div>';
    return;
  }
  el.innerHTML = "";
  for (const c of cs) {
    const running = c.status === "running";
    const done = c.status === "done";
    const resumable = c.steps > 0 && !done;
    const report = (c.report || "").replace(/\s+/g, " ").slice(0, 600);
    const prog = running ? "running" : (done ? "full" : "empty");
    const card = document.createElement("div");
    card.className = "exp-card";
    card.innerHTML = `<div class="exp-card-head">
        <b class="exp-card-name">${esc(c.name)}</b>
        ${campaignStatusBadge(c.status)}
        <span class="muted exp-card-runs">${c.steps} step(s)</span>
        <span class="spacer"></span>
        ${running
          ? `<button class="btn subtle small camp-stop" data-id="${c.id}">⏹ Stop</button>`
          : (!done
              ? `<button class="btn subtle small camp-run" data-id="${c.id}">▶ ${resumable ? "Resume" : "Run"}</button>`
              : "")}
      </div>
      <div class="exp-card-progress ${prog}"></div>
      ${c.research_question ? `<div class="exp-card-hyp muted">${esc(c.research_question)}</div>` : ""}
      ${c.report ? `<details class="exp-plan"><summary>Report</summary><div class="exp-plan-body">${esc(report)}</div></details>` : ""}`;
    el.appendChild(card);
  }
  el.querySelectorAll(".camp-run").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api(`/api/projects/${state.project}/campaigns/${b.dataset.id}/run`, {
          method: "POST", body: "{}",
        });
        toast("Campaign started in the background.");
        await loadCampaigns();
        startCampaignPoll();
      } catch (e) { toast("Failed to start campaign: " + e.message); }
    }));
  el.querySelectorAll(".camp-stop").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api(`/api/projects/${state.project}/campaigns/${b.dataset.id}/stop`, {
          method: "POST",
        });
        toast("Stop requested — it will halt at the next step boundary.");
      } catch (e) { toast("Failed to stop campaign: " + e.message); }
    }));
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
  // Re-render so card head summaries (best / progress) reflect the ranking.
  renderExpList();
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
    const target = rank.goal_target;
    const head = rank.metric
      ? `<summary class="exp-rank-sum">Leaderboard — <b>${esc(rank.metric.replace(/_/g, " "))}</b> ${rank.higher_better ? "↑" : "↓"} (best ${_fmtNum(rank.best)}${target != null ? ` · target ${_fmtNum(target)}` : ""})</summary>`
      : `<summary class="exp-rank-sum">Leaderboard — no numeric metric yet</summary>`;
    if (!rows.length) {
      host.innerHTML = `<details class="exp-rank">${head}<div class="exp-rank-body empty">No runs report the metric "${esc(rank.metric)}".</div></details>`;
      return;
    }
    let html = `<table class="exp-rank-table"><thead><tr><th>#</th><th>run</th><th>${esc(rank.metric.replace(/_/g, " "))}</th>${target != null ? `<th>to target</th>` : ""}<th>Δ best</th><th></th></tr></thead><tbody>`;
    for (const row of rows) {
      const medal = row.rank === 1 ? " 🏆" : "";
      const reached = target != null && ((row.metric >= target && rank.higher_better) || (row.metric <= target && !rank.higher_better));
      const toTarget = target != null
        ? (reached ? `<span class="rank-reached">✓ reached</span>` : `<span class="muted">${(row.to_target >= 0 ? "+" : "") + _fmtNum(row.to_target)}</span>`)
        : "";
      html += `<tr${row.rank === 1 ? ' class="rank-top"' : ""}>
        <td class="rank-pos">${row.rank}${medal}</td>
        <td>${esc(row.label || "#" + row.run_id)}</td>
        <td>${_fmtNum(row.metric)}</td>
        ${toTarget}
        <td class="muted">${row.rank === 1 ? "—" : (row.delta_best >= 0 ? "+" : "") + _fmtNum(row.delta_best)}</td>
        <td><button class="btn subtle small rank-revert" data-rid="${row.run_id}" title="Revert: rerun this run's prompt as a fresh turn">↶ revert</button></td>
      </tr>`;
    }
    html += "</tbody></table>";
    host.innerHTML = `<details class="exp-rank">${head}<div class="exp-rank-body">${html}</div></details>`;
    host.querySelectorAll(".rank-revert").forEach((b) =>
      b.addEventListener("click", () =>
        sendChat("", "rerun_run", { run_id: b.dataset.rid })));
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

async function openExpDetail(eid) {
  const meta = (state.expList || []).find((x) => x.id === eid) || {};
  $("exp-detail-title").textContent = meta.name || `Experiment #${eid}`;
  const body = $("exp-detail-body");
  body.innerHTML = '<div class="exp-loading">Loading…</div>';
  const exp = await loadExpDetail(eid);
  body.innerHTML = renderExpDetailBody(eid, meta, exp);
  body.querySelectorAll(".expd-close").forEach((b) =>
    b.addEventListener("click", () => $("exp-detail-modal").classList.add("hidden")));
  body.querySelectorAll(".expd-improve").forEach((b) =>
    b.addEventListener("click", () => {
      $("exp-detail-modal").classList.add("hidden");
      sendChat(`Improve the experiment "${b.dataset.name}" — run the next variant toward its goal.`,
               "improve_loop", { experiment_id: b.dataset.eid });
    }));
  body.querySelectorAll(".expd-edit").forEach((b) =>
    b.addEventListener("click", () => {
      $("exp-detail-modal").classList.add("hidden");
      openExpEdit(Number(b.dataset.eid));
    }));
  body.querySelectorAll(".expd-focus").forEach((b) =>
    b.addEventListener("click", async () => {
      const fid = state.focusExperiment === Number(b.dataset.eid) ? null : Number(b.dataset.eid);
      try {
        const r = await api(`/api/projects/${state.project}/experiments/focus`, {
          method: "POST", body: JSON.stringify({ id: fid }),
        });
        state.focusExperiment = r.focus_id;
        await loadExperiments();
        toast(fid ? "Experiment focused." : "Focus cleared.");
      } catch (e) { toast("Failed to set focus: " + e.message); }
    }));
  $("exp-detail-modal").classList.remove("hidden");
}

function renderExpDetailBody(eid, meta, exp) {
  const rank = (state.expRanking && state.expRanking[eid]) || null;
  const best = rank && rank.best != null ? rank.best : null;
  const higher = meta.higher_better !== false;
  const target = meta.goal_target != null ? meta.goal_target : null;
  const reached = best != null && target != null && (higher ? best >= target : best <= target);
  const runs = exp.runs || [];
  const ls = (state.learnings || {})[String(eid)] || [];
  let h = "";
  if (meta.hypothesis || meta.goal_metric || best != null || meta.model || meta.plan) {
    h += `<div class="run-detail-grid">`;
    if (meta.hypothesis) h += `<div><span class="rd-k">Hypothesis</span><span class="rd-v">${esc(meta.hypothesis)}</span></div>`;
    if (meta.goal_metric) h += `<div><span class="rd-k">Goal</span><span class="rd-v">${esc(meta.goal_metric)} ${higher ? "↑" : "↓"}${target != null ? " → " + _fmtNum(target) : ""}</span></div>`;
    if (best != null) h += `<div><span class="rd-k">Best</span><span class="rd-v">${_fmtNum(best)}${reached ? ' <span class="rank-reached">✓</span>' : ""}</span></div>`;
    if (meta.model) h += `<div><span class="rd-k">Model</span><span class="rd-v">${esc(meta.model)}</span></div>`;
    if (meta.plan) h += `<div><span class="rd-k">Plan</span><span class="rd-v">${esc(meta.plan)}</span></div>`;
    h += `</div>`;
  }
  h += `<div class="run-actions">
      <button class="btn subtle small expd-improve" data-eid="${eid}" data-name="${esc(meta.name || "")}">🔁 Improve</button>
      <button class="btn subtle small expd-edit" data-eid="${eid}">✎ Edit</button>
      <button class="btn subtle small expd-focus" data-eid="${eid}">${state.focusExperiment === eid ? "☆ Unfocus" : "★ Focus"}</button>
      <button class="btn subtle small expd-close">Close</button>
    </div>`;
  if (rank && (rank.rows || []).length) {
    h += `<div class="run-detail-sec">Leaderboard (${esc(rank.metric || "metric")})</div>` +
      `<table class="exp-rank-table"><thead><tr><th>#</th><th>run</th><th>${esc(rank.metric || "metric")}</th><th>Δ best</th></tr></thead><tbody>`;
    rank.rows.forEach((row) => {
      h += `<tr${row.rank === 1 ? ' class="rank-top"' : ""}><td class="rank-pos">${row.rank}</td><td>${esc(row.label || "#" + row.run_id)}</td><td>${_fmtNum(row.metric)}</td><td class="muted">${row.rank === 1 ? "—" : _fmtNum(row.delta_best)}</td></tr>`;
    });
    h += `</tbody></table>`;
  }
  if (ls.length) {
    h += `<div class="run-detail-sec">Learnings (${ls.length})</div>`;
    h += ls.map((l) => {
      const badge = l.improved === 1 ? '<span class="sug-badge ok">✓ improved</span>'
        : (l.improved === 0 ? '<span class="sug-badge warn">✗ no gain</span>' : "");
      return `<div class="learning-row">${badge}<span>${esc(l.summary)}</span></div>`;
    }).join("");
  }
  h += `<div class="run-detail-sec">Runs (${runs.length})</div>`;
  if (runs.length) {
    h += runs.slice(0, 50).map((r) => {
      const m = r.metrics || {};
      const mstr = Object.keys(m).length
        ? Object.keys(m).map((k) => `${k}=${_fmtNum(m[k])}`).join(", ") : "—";
      return `<div class="learning-row"><span class="run-id">#${r.id}</span><span>${esc((r.label || "").slice(0, 30))} · ${esc(mstr.slice(0, 140))}</span></div>`;
    }).join("");
  } else {
    h += `<div class="exp-empty">No runs yet — improve this experiment or run variants in chat.</div>`;
  }
  return h;
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
    updateComposerCtx(null, null);
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

const DEFAULT_PLACEHOLDER = "Ask Fox to run an analysis, e.g. 'Load the attached CSV and cluster the cells, then plot a UMAP.'";

// Reflect the active experiment's goal above the composer so the prompt stays
// pointed at the objective while the user types.
function updateComposerCtx(exp, best) {
  const chip = $("composer-ctx");
  const input = $("input");
  if (!chip) return;
  if (!exp || !exp.goal_metric) {
    chip.classList.add("hidden");
    if (input) input.placeholder = DEFAULT_PLACEHOLDER;
    return;
  }
  const target = exp.goal_target != null ? " → " + _fmtNum(exp.goal_target) : "";
  const dirn = exp.higher_better !== false ? "↑" : "↓";
  const bestTxt = best ? ` · best ${_fmtNum(best.v)}` : "";
  chip.classList.remove("hidden");
  chip.innerHTML = `<span class="composer-goal" title="Active experiment: ${esc(exp.name)}">
    🎯 ${esc(exp.name)} · ${esc(exp.goal_metric)} ${dirn}${esc(target)}${bestTxt}
    <span class="composer-goal-sub">click to open experiment controls</span></span>`;
  if (input) input.placeholder = `Work toward ${exp.goal_metric}${dirn}${target} — tell Fox what to try next`;
  chip.querySelector(".composer-goal").addEventListener("click", () => {
    const body = $("ec-body");
    if (body) {
      const open = body.classList.toggle("hidden");
      $("ec-toggle").classList.toggle("open", !open);
    }
  });
}

async function renderExpContext() {
  const ctx = $("exp-context");
  if (!ctx) return;
  const exps = state.expList || [];
  if (!exps.length) { ctx.classList.add("hidden"); return; }
  const sel = $("ec-select");
  const preferId = state.focusExperiment != null ? state.focusExperiment : state.activeExperiment;
  sel.innerHTML = exps.map((e) =>
    `<option value="${e.id}"${e.id === preferId ? " selected" : ""}>${esc(e.name)}${e.id === state.focusExperiment ? " ★" : ""}</option>`).join("");
  const eid = preferId != null ? preferId : (exps[0].id);
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

  const nameEl = $("ec-name");
  if (nameEl) nameEl.textContent = (exp && exp.name) || "experiment";
  const dotEl = $("ec-dot");
  if (dotEl) {
    const st = (exp && exp.status) || "active";
    dotEl.style.background = st === "completed" ? "var(--ok)"
      : st === "cancelled" ? "var(--warn)" : "var(--accent)";
    dotEl.style.boxShadow = `0 0 6px ${dotEl.style.background}`;
  }

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
  updateComposerCtx(exp, best);
  // Persisted last commit/push (survives page refresh).
  const mgmtEl = $("ec-mgmt-msg");
  if (mgmtEl) mgmtEl.innerHTML = mgmtActivityHtml(state.mgmtActivity);
}

function mgmtActivityHtml(a) {
  if (!a) return "";
  const link = repoCommitLinkHtml(a);
  if (a.action === "push") {
    const when = a.pushed_at || a.committed_at;
    return "Last: Pushed " + link + (when ? " on " + esc(fmtDt(when)) : "") + " ✓";
  }
  return "Last: Committed " + link +
    (a.committed_at ? " · " + esc(fmtDt(a.committed_at)) : "") + " ✓";
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
  expandSetOf(el);
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
  expandSetOf(pick);
  pick.classList.add("exp-flash");
  setTimeout(() => pick.classList.remove("exp-flash"), 1600);
}

async function focusExperiment(eid) {
  state.activeExperiment = eid;
  await renderExpContext();
}

let ecMgmtMsgTimer = null;

async function ecManagementAction(kind) {
  const btn = kind === "commit" ? $("ec-commit") : $("ec-push");
  const orig = btn.textContent;
  btn.textContent = kind === "commit" ? "Committing…" : "Pushing…";
  btn.disabled = true;
  const msg = $("ec-mgmt-msg");
  clearTimeout(ecMgmtMsgTimer);
  try {
    const endpoint = kind === "commit" ? "commit" : "push";
    const r = await api(`/api/projects/${state.project}/management/${endpoint}`, {
      method: "POST", body: JSON.stringify({}),
    });
    if (r.ok) {
      state.mgmtActivity = { action: kind, ...r };
      const link = repoCommitLinkHtml(r);
      msg.innerHTML = kind === "commit"
        ? "Committed " + link + (r.committed_at ? " · " + esc(fmtDt(r.committed_at)) : "") + " ✓"
        : "Pushed " + link + (r.pushed_at ? " on " + esc(fmtDt(r.pushed_at)) : "") + " ✓";
      msg.classList.remove("err");
    } else {
      msg.textContent = r.message || "failed";
      msg.classList.add("err");
      ecMgmtMsgTimer = setTimeout(() => { msg.textContent = ""; msg.classList.remove("err"); }, 8000);
    }
  } catch (e) {
    msg.textContent = "Failed: " + e.message;
    msg.classList.add("err");
    ecMgmtMsgTimer = setTimeout(() => { msg.textContent = ""; msg.classList.remove("err"); }, 8000);
  }
  btn.textContent = orig;
  btn.disabled = false;
}

$("ec-select").addEventListener("change", (e) => focusExperiment(parseInt(e.target.value, 10)));
$("ec-toggle").addEventListener("click", () => {
  const toggle = $("ec-toggle");
  const body = $("ec-body");
  const open = body.classList.toggle("hidden");
  toggle.classList.toggle("open", !open);
});
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
  const all = state.agentRuns || [];
  // Keep the experiment filter dropdown in sync.
  const filterSel = $("runs-exp-filter");
  if (filterSel && (state.expList || []).length) {
    const cur = filterSel.value;
    const opts = `<option value="">all experiments</option>` +
      state.expList.map((e) =>
        `<option value="${e.id}"${String(cur) === String(e.id) ? " selected" : ""}>${esc(e.name)}</option>`).join("");
    if (filterSel.innerHTML !== opts) filterSel.innerHTML = opts;
  }
  const eid = state.runsExpFilter || "";
  const q = (state.runsSearch || "").toLowerCase().trim();
  const runs = all.filter((r) => {
    if (eid && String(r.experiment_id) !== String(eid)) return false;
    if (q && !((r.prompt || "") + " " + (r.label || "") + " " + (r.kind || "")).toLowerCase().includes(q)) return false;
    return true;
  });
  if (!all.length) {
    el.innerHTML = '<div class="exp-empty">No agent runs yet in this project.</div>';
    return;
  }
  if (!runs.length) {
    el.innerHTML = '<div class="exp-empty">No runs match the current filter.</div>';
    return;
  }
  const RUNS_CHUNK = 40;
  const ordered = runs.slice().reverse();
  const shown = ordered.slice(0, state.runsChunk || RUNS_CHUNK);
  el.innerHTML = "";
  for (const r of shown) {
    const rev = r.review || {};
    const nf = (rev.findings || []).length;
    const ns = (rev.suggestions || []).length;
    const meta = [r.status];
    if (r.metrics && Object.keys(r.metrics).length) meta.push(Object.keys(r.metrics).length + " metric(s)");
    if (nf) meta.push(nf + " finding(s)");
    if (ns) meta.push(ns + " suggestion(s)");
    const goalExp = expOf(r.experiment_id);
    const goalMetric = goalExp && goalExp.goal_metric;
    const gv = goalMetric && r.metrics && r.metrics[goalMetric];
    const gvNum = gv != null && !Number.isNaN(Number(gv)) ? Number(gv) : null;
    const rankRec = goalExp ? (state.expRanking && state.expRanking[goalExp.id]) : null;
    const bestV = rankRec && rankRec.best != null ? rankRec.best : null;
    const dBest = (gvNum != null && bestV != null) ? gvNum - bestV : null;
    const d = document.createElement("div");
    d.className = "run-row";
    d.dataset.id = r.id;
    const lbl = r.label ? `<span class="run-label">${esc(r.label)}</span> ` : "";
    d.innerHTML = `<div class="run-row-head" role="button" tabindex="0" aria-expanded="false" aria-controls="run-detail-${r.id}" title="Expand / collapse run details">
        <span class="run-caret">▶</span>
        <span class="run-id">#${r.id}</span>
        <span class="run-prompt">${lbl}${esc((r.prompt || "").slice(0, 80))}</span>
        ${gvNum != null && goalMetric ? `<span class="run-goal" title="goal metric ${esc(goalMetric)}">${esc(goalMetric.replace(/_/g, " "))} ${_fmtNum(gvNum)}</span>` : ""}
        ${deltaBestChip(dBest)}
        <span class="run-meta muted">${esc(meta.join(" · "))}</span>
        ${r.git_commit ? `<span class="run-commit" title="Management-repo snapshot commit">${esc(String(r.git_commit).slice(0, 8))}</span>` : ""}
        ${r.integrity_hash ? `<span class="run-commit" title="Content hash (integrity)">✓ ${esc(String(r.integrity_hash).slice(0, 8))}</span>` : ""}
        <button class="btn subtle small run-report" data-id="${r.id}" title="Generate the lab-notebook report">Report</button>
      </div>
      <div class="run-row-detail hidden" id="run-detail-${r.id}">${runDetailHtml(r)}</div>`;
    el.appendChild(d);
  }
  el.querySelectorAll(".run-row-head").forEach((h) =>
    h.addEventListener("click", () => {
      const row = h.parentElement;
      const det = row.querySelector(".run-row-detail");
      if (!det) return;
      const open = det.classList.toggle("hidden");
      row.classList.toggle("open", !open);
      h.setAttribute("aria-expanded", open ? "false" : "true");
      setExpParam("run", open ? "" : String(row.dataset.id));
    }));
  el.querySelectorAll(".run-row-head").forEach((h) =>
    h.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); h.click(); }
    }));
  el.querySelectorAll(".run-report").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
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
  el.querySelectorAll(".run-revert").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      sendChat("", "rerun_run", { run_id: b.dataset.rid });
    }));
  el.querySelectorAll(".run-improve-exp").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      sendChat(`Improve the experiment "${b.dataset.name}" — run the next variant toward its goal.`,
               "improve_loop", { experiment_id: b.dataset.eid });
    }));
  if (ordered.length > shown.length) {
    const more = document.createElement("div");
    more.className = "exp-more";
    const remaining = ordered.length - shown.length;
    more.innerHTML = `<button class="btn subtle small exp-more-btn">Show ${remaining} more run(s)…</button>`;
    more.querySelector("button").addEventListener("click", () => {
      state.runsChunk = (state.runsChunk || RUNS_CHUNK) + RUNS_CHUNK;
      renderRuns();
    });
    el.appendChild(more);
  }
}

function runDetailHtml(r) {
  const goalExp = (state.expList || []).find((e) => String(e.id) === String(r.experiment_id));
  const goalMetric = goalExp && goalExp.goal_metric;
  const mkeys = Object.keys(r.metrics || {});
  let h = "";
  if (mkeys.length) {
    h += `<div class="run-detail-sec">Metrics</div><div class="run-detail-grid">`;
    for (const k of mkeys) {
      const isGoal = goalMetric === k;
      h += `<span class="rd-k">${esc(k)}${isGoal ? " ★" : ""}</span><span class="rd-v">${_fmtNum(r.metrics[k])}</span>`;
    }
    h += `</div>`;
  }
  const cfg = r.config && typeof r.config === "object" && Object.keys(r.config).length;
  if (cfg) {
    h += `<div class="run-detail-sec">Config</div><div class="run-detail-grid">`;
    for (const [k, v] of Object.entries(r.config)) {
      h += `<span class="rd-k">${esc(k)}</span><span class="rd-v">${esc(String(v))}</span>`;
    }
    h += `</div>`;
  }
  const tools = r.tool_sequence || [];
  if (tools.length) {
    h += `<div class="run-detail-sec">Tool trail</div><div class="run-tools">`;
    for (const t of tools.slice(0, 12)) {
      const name = (t && t.name) || "?";
      h += `<span class="run-tool-chip${t && t.ok === false ? " fail" : ""}">${esc(name)}${t && t.ok === false ? " ✗" : ""}</span>`;
    }
    h += `</div>`;
  }
  if (r.prompt) {
    h += `<div class="run-detail-sec">Prompt</div><div class="run-prompt-full">${esc(r.prompt)}</div>`;
  }
  const acts = [];
  if (r.kind !== "restore") acts.push(
    `<button class="btn subtle small run-revert" data-rid="${r.id}" title="Re-run this run's prompt as a fresh turn">↶ revert</button>`);
  if (r.experiment_id != null) {
    const nm = expName(r.experiment_id);
    acts.push(`<button class="btn subtle small run-improve-exp" data-eid="${r.experiment_id}" data-name="${esc(nm)}" title="Improve the owning experiment">🔁 improve</button>`);
  }
  if (acts.length) h += `<div class="run-actions">${acts.join("")}</div>`;
  return h;
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
    renderExpKpis();
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
    const prog = goalProgress(g);
    let progressHtml = "";
    let statHtml = `<span class="goal-stat">${prog.best != null ? "best " + _fmtNum(prog.best) : "no data"}</span>`;
    if (prog.best != null) {
      const pct = Math.max(0, Math.min(100, prog.pct));
      progressHtml = `<div class="goal-progress"><div class="goal-progress-fill${prog.reached ? " reached" : ""}" style="width:${pct}%"></div></div>`;
      if (prog.reached) statHtml = `<span class="rank-reached">✓ reached</span>`;
      else statHtml = `<span class="goal-stat">best ${_fmtNum(prog.best)} · ${prog.delta} to go</span>`;
    }
    d.innerHTML = `<b>${esc(g.label || g.metric)}</b>
      <span class="muted">${esc(g.metric)} ${g.higher_better ? "↑" : "↓"} target ${_fmtNum(g.target)} · ${esc(expName(g.experiment_id))}</span>
      ${progressHtml}
      ${statHtml}
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
  renderExpKpis();
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
  const all = state.expList || [];
  const q = (state.expSearch || "").toLowerCase().trim();
  let exps = q
    ? all.filter((e) =>
        (e.name || "").toLowerCase().includes(q) ||
        (e.hypothesis || "").toLowerCase().includes(q) ||
        (e.goal_metric || "").toLowerCase().includes(q))
    : all.slice();
  exps = sortedExps(exps);
  if ($("exp-count")) $("exp-count").textContent = q
    ? `${exps.length} / ${all.length}`
    : `(${all.length})`;
  if (!all.length) {
    el.innerHTML = '<div class="exp-empty">No experiments yet — ask Fox to plan and run one in chat, or create one below.</div>';
    return;
  }
  if (!exps.length) {
    el.innerHTML = '<div class="exp-empty">No experiments match “' + esc(q) + '”.</div>';
    return;
  }
  const EXP_CHUNK = 30;
  const shown = exps.slice(0, state.expChunk || EXP_CHUNK);
  el.innerHTML = "";
  for (const e of shown) {
    const card = document.createElement("div");
    card.className = "exp-card";
    card.dataset.id = e.id;
    const rank = (state.expRanking && state.expRanking[e.id]) || null;
    const best = rank && rank.best != null ? rank.best : null;
    const target = e.goal_target != null ? e.goal_target : null;
    const higher = e.higher_better !== false;
    const pct = (best != null && target)
      ? Math.max(0, Math.min(100, (best / target) * 100)) : 0;
    const reached = best != null && target != null &&
      (higher ? best >= target : best <= target);
    const goalLine = e.goal_metric
      ? `goal ${esc(e.goal_metric)} ${higher ? "↑" : "↓"}` +
        (best != null ? ` · best ${_fmtNum(best)}` : "") +
        (target != null ? ` / ${_fmtNum(target)}` : "")
      : (best != null ? `best ${_fmtNum(best)}` : "");
    const series = e.goal_metric ? expSeries(e.id, e.goal_metric) : [];
    const deltaBest = (best != null && series.length)
      ? series[series.length - 1] - best : null;
    const modelPin = e.model
      ? `<span class="muted exp-model-pin" title="Pinned model for this experiment">◈ ${esc(e.model)}</span>`
      : "";
    let planHtml = "";
    if (e.plan) {
      planHtml = `<details class="exp-plan"><summary>Plan</summary><div class="exp-plan-body">${esc(e.plan)}</div></details>`;
    }
    const status = e.status || "active";
    const active = status === "active";
    const badgeCls = active ? "det" : (status === "completed" ? "ok" : "warn");
    const focused = state.focusExperiment === e.id;
    card.innerHTML = `<div class="exp-card-head">
        <b class="exp-card-name" data-id="${e.id}" title="Open experiment detail">${esc(e.name)}</b>
        <span class="exp-badge ${badgeCls}">${esc(status)}</span>
        ${reached ? `<span class="rank-reached" title="Goal reached">✓</span>` : ""}
        <span class="muted exp-card-runs">${e.runs} run(s)</span>
        <span class="spacer"></span>
        <button class="btn subtle small exp-focus${focused ? " focus-on" : ""}" data-id="${e.id}" title="${focused ? "Unfocus this experiment" : "Focus — steer the agent toward this objective"}">${focused ? "★" : "☆"}</button>
        <button class="btn subtle small exp-edit" data-id="${e.id}" title="Edit hypothesis, goal or plan">✎</button>
        <select class="exp-status" data-id="${e.id}" title="lifecycle status">
          <option value="active"${active ? " selected" : ""}>active</option>
          <option value="completed"${status === "completed" ? " selected" : ""}>completed</option>
          <option value="cancelled"${status === "cancelled" ? " selected" : ""}>cancelled</option>
        </select>
        <button class="btn subtle small exp-improve" data-id="${e.id}" data-name="${esc(e.name)}"${active ? "" : " disabled title=\"reopen the experiment first\""}>Improve</button>
        <button class="btn subtle small exp-export" data-id="${e.id}" title="Export this experiment's runs as CSV">⬇</button>
        <button class="btn subtle small exp-details" data-id="${e.id}" title="Toggle details">Details ▸</button>
      </div>
      ${goalLine ? `<div class="exp-card-sum muted">${goalLine}</div>` : ""}
      ${(series.length >= 2 || deltaBest != null) ? `<div class="exp-card-row">${sparklineSvg(series, 110, 26, expColor(e.id))}${deltaBestChip(deltaBest)}</div>` : ""}
      ${(best != null && target) ? `<div class="exp-goal-bar"><div class="exp-goal-fill ${reached ? "reached" : ""}" style="width:${pct}%"></div></div>` : ""}
      <div class="exp-card-detail hidden">
        ${e.hypothesis ? `<div class="exp-card-hyp muted">${esc(e.hypothesis)}</div>` : ""}
        ${modelPin}
        ${planHtml}
        ${learningsHtml(e.id)}
        <div class="exp-rank-host"></div>
      </div>`;
    el.appendChild(card);
  }
  el.querySelectorAll(".exp-improve").forEach((b) =>
    b.addEventListener("click", () => {
      sendChat(`Improve the experiment "${b.dataset.name}" — run the next variant toward its goal.`,
               "improve_loop", { experiment_id: b.dataset.id });
    }));
  el.querySelectorAll(".exp-focus").forEach((b) =>
    b.addEventListener("click", async () => {
      const fid = state.focusExperiment === Number(b.dataset.id) ? null : Number(b.dataset.id);
      try {
        const r = await api(`/api/projects/${state.project}/experiments/focus`, {
          method: "POST",
          body: JSON.stringify({ id: fid }),
        });
        state.focusExperiment = r.focus_id;
        await loadExperiments();
        toast(fid ? "Experiment focused — the agent will steer toward it." : "Focus cleared.");
      } catch (e) { toast("Failed to set focus: " + e.message); }
    }));
  el.querySelectorAll(".exp-edit").forEach((b) =>
    b.addEventListener("click", () => openExpEdit(Number(b.dataset.id))));
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
  el.querySelectorAll(".exp-details").forEach((b) =>
    b.addEventListener("click", () => {
      const card = el.querySelector(`.exp-card[data-id="${b.dataset.id}"]`);
      if (!card) return;
      const detail = card.querySelector(".exp-card-detail");
      if (!detail) return;
      const open = !detail.classList.contains("hidden");
      detail.classList.toggle("hidden", open);
      b.textContent = open ? "Details ▸" : "Details ▾";
    }));
  el.querySelectorAll(".exp-card-name").forEach((b) =>
    b.addEventListener("click", () => openExpDetail(Number(b.dataset.id))));
  el.querySelectorAll(".exp-export").forEach((b) =>
    b.addEventListener("click", () => {
      const eid = Number(b.dataset.id);
      const runs = (state.agentRuns || []).filter((r) => String(r.experiment_id) === String(eid));
      exportRunsCsv(runs, `experiment-${eid}-runs.csv`);
    }));
  if (exps.length > shown.length) {
    const more = document.createElement("div");
    more.className = "exp-more";
    const remaining = exps.length - shown.length;
    more.innerHTML = `<button class="btn subtle small exp-more-btn">Show ${remaining} more experiment(s)…</button>`;
    more.querySelector("button").addEventListener("click", () => {
      state.expChunk = (state.expChunk || EXP_CHUNK) + EXP_CHUNK;
      renderExpList();
    });
    el.appendChild(more);
  }
}

function sortedExps(list) {
  const s = state.expSort || "recent";
  const arr = (list || []).slice();
  if (s === "name") arr.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  else if (s === "runs") arr.sort((a, b) => (b.runs || 0) - (a.runs || 0));
  else if (s === "best") arr.sort((a, b) => {
    const ra = state.expRanking && state.expRanking[a.id];
    const rb = state.expRanking && state.expRanking[b.id];
    const ba = ra && ra.best != null ? ra.best : -Infinity;
    const bb = rb && rb.best != null ? rb.best : -Infinity;
    return bb - ba;
  });
  else arr.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  return arr;
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

function openExpEdit(eid) {
  const e = (state.expList || []).find((x) => x.id === eid);
  if (!e) return;
  $("exp-edit-id").value = eid;
  $("exp-edit-name").value = e.name || "";
  $("exp-edit-hypothesis").value = e.hypothesis || "";
  $("exp-edit-goal-metric").value = e.goal_metric || "";
  $("exp-edit-goal-target").value = e.goal_target != null ? String(e.goal_target) : "";
  $("exp-edit-hb").checked = e.higher_better !== false;
  $("exp-edit-plan").value = e.plan || "";
  $("exp-edit-model").value = e.model || "";
  $("exp-edit-modal").classList.remove("hidden");
}

async function saveExpEdit() {
  const eid = Number($("exp-edit-id").value);
  const body = {
    name: $("exp-edit-name").value.trim(),
    hypothesis: $("exp-edit-hypothesis").value.trim(),
    goal_metric: $("exp-edit-goal-metric").value.trim(),
    goal_target: null,
    higher_better: $("exp-edit-hb").checked,
    plan: $("exp-edit-plan").value.trim(),
    model: $("exp-edit-model").value.trim(),
  };
  const t = $("exp-edit-goal-target").value.trim();
  if (t !== "") {
    body.goal_target = parseFloat(t);
    if (Number.isNaN(body.goal_target)) { toast("Goal target must be a number"); return; }
  }
  try {
    await api(`/api/projects/${state.project}/experiments/${eid}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    $("exp-edit-modal").classList.add("hidden");
    await loadExperiments();
    toast("Experiment updated.");
  } catch (e) { toast("Failed to update experiment: " + e.message); }
}

function expMetric() { return state.expMetric || ""; }
function expNodeValue(node, metric) {
  const v = (node.metrics && node.metrics[metric] != null) ? node.metrics[metric] : node[metric];
  return (v == null || Number.isNaN(Number(v))) ? null : Number(v);
}
function _fmtAxis(v) { return String(Math.round(Number(v) * 1000) / 1000); }

// Prefer `accuracy` as the default metric whenever it is available.
function preferMetricDefault(opts) {
  if (!opts.length) return "";
  const exact = opts.find((k) => String(k).toLowerCase() === "accuracy");
  if (exact) return exact;
  const like = opts.find((k) => String(k).toLowerCase().includes("accuracy"));
  return like || opts[0];
}

function populateExpMetrics() {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  // only list metrics that actually have a value in the runs
  const withValue = new Set();
  nodes.forEach((n) => {
    for (const k of Object.keys(n.metrics || {})) {
      if (expNodeValue(n, k) != null) withValue.add(k);
    }
  });
  const opts = [...withValue].sort();
  if (!opts.includes(state.expMetric)) state.expMetric = preferMetricDefault(opts);
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

/* ---- trend sparklines + deltas (experiments tab) ---- */

// Chronological series of a metric's values for one experiment, from the runs
// already loaded in state.agentRuns (id-ordered → time-ordered).
function expSeries(eid, metric) {
  if (eid == null || !metric) return [];
  const out = [];
  for (const r of state.agentRuns || []) {
    if (String(r.experiment_id) !== String(eid)) continue;
    const v = r.metrics && r.metrics[metric];
    if (v == null || Number.isNaN(Number(v))) continue;
    out.push(Number(v));
  }
  return out;
}

// Tiny inline SVG sparkline (monotonic x, value y), colored per experiment.
function sparklineSvg(values, w = 96, h = 24, color = "#a974ff") {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values), max = Math.max(...values);
  const span = (max - min) || 1;
  const pad = 2;
  let pts = "", lastY = h / 2;
  for (let i = 0; i < values.length; i++) {
    const x = (i * (w - 2 * pad) / (values.length - 1) + pad).toFixed(1);
    const y = (pad + (1 - (values[i] - min) / span) * (h - 2 * pad)).toFixed(1);
    pts += `${x},${y} `;
    if (i === values.length - 1) lastY = Number(y);
  }
  const lx = (w - pad).toFixed(1);
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="metric trend">` +
    `<polyline points="${pts.trim()}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>` +
    `<circle cx="${lx}" cy="${lastY}" r="2.6" fill="${color}"/></svg>`;
}

// Sign-colored "Δ best +0.31 / −0.02" chip; empty when the delta is zero.
function deltaBestChip(delta) {
  if (delta == null || !isFinite(Number(delta)) || Math.abs(Number(delta)) < 1e-12) return "";
  const up = Number(delta) > 0;
  return `<span class="exp-delta ${up ? "up" : "down"}" title="vs the experiment's best">Δ best ${up ? "+" : ""}${_fmtNum(delta)}</span>`;
}

/* ---- CSV export (runs) ---- */

function csvEscape(v) {
  const s = String(v == null ? "" : v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function downloadText(filename, text, mime = "text/csv") {
  const blob = new Blob([text], { type: mime + ";charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 300);
}

// Flat CSV of runs: identity columns + union of all numeric/string metric keys.
function runsCsv(runs) {
  const rows = runs || [];
  const metricKeys = [];
  const seen = new Set();
  for (const r of rows) for (const k of Object.keys(r.metrics || {})) {
    if (!seen.has(k)) { seen.add(k); metricKeys.push(k); }
  }
  const head = ["id", "experiment", "kind", "status", "created_at", "label",
    "prompt", "git_commit", "integrity_hash"].concat(metricKeys);
  const lines = [head.map(csvEscape).join(",")];
  for (const r of rows) {
    const row = [
      r.id, expName(r.experiment_id), r.kind, r.status, r.created_at, r.label,
      r.prompt, r.git_commit, r.integrity_hash,
    ].concat(metricKeys.map((k) => (r.metrics || {})[k]));
    lines.push(row.map(csvEscape).join(","));
  }
  return lines.join("\n");
}

function exportRunsCsv(runs, filename) {
  if (!(runs || []).length) { toast("Nothing to export — no runs match."); return; }
  downloadText(filename || "runs.csv", runsCsv(runs));
}

// The "Fox - <Model> - <MCP> - <Action>" label for a run node in the
// Experiments charts and detail panel.
function runFoxLabel(n) {
  const parts = ["Fox"];
  if (n && n.model) parts.push(n.model);
  if (n && n.mcp && n.action) {
    parts.push(n.mcp);
    parts.push(n.action);
  }
  return parts.length > 1 ? parts.join(" - ") : "";
}

// Compact tool-trail summary ("github/push, eda_profiler/profile_data") for
// tooltips / detail panels.
function runToolsSummary(n) {
  const tools = (n && n.tools) || [];
  if (!tools.length) return "";
  return tools
    .map((t) => (t.mcp && t.action ? `${t.mcp}/${t.action}` : t.name || "tool"))
    .join(", ");
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

    // Verdict on the owning experiment's goal metric.
    const exp = expOf(ra && ra.experiment_id) || expOf(rb && rb.experiment_id);
    if (exp && exp.goal_metric) {
      const va = ra && ra.metrics ? ra.metrics[exp.goal_metric] : null;
      const vb = rb && rb.metrics ? rb.metrics[exp.goal_metric] : null;
      if (va != null && vb != null) {
        const na = Number(va), nb = Number(vb);
        const higher = exp.higher_better !== false;
        const verdict = Math.abs(nb - na) < 1e-12
          ? "tie"
          : ((nb > na) === higher ? "b" : "a");
        const who = verdict === "a" ? esc(c.a) : verdict === "b" ? esc(c.b) : "both";
        const word = verdict === "tie" ? "tied on" : `wins on ${esc(exp.goal_metric.replace(/_/g, " "))} (${_fmtNum(nb)} vs ${_fmtNum(na)})`;
        h += `<div class="cmp-verdict"><b>${who}</b> ${word}</div>`;
      }
    }

    // Side-by-side config (union of keys, differences highlighted).
    const fullA = (state.agentRuns || []).find((r) => String(r.id) === String(a));
    const fullB = (state.agentRuns || []).find((r) => String(r.id) === String(b));
    const cfgA = (fullA && fullA.config) || (ra && ra.config) || {};
    const cfgB = (fullB && fullB.config) || (rb && rb.config) || {};
    const cfgKeys = [...new Set([...Object.keys(cfgA), ...Object.keys(cfgB)])];
    if (cfgKeys.length) {
      let ch = `<tr><th>config</th><th>${esc(c.a)}</th><th>${esc(c.b)}</th></tr>`;
      for (const k of cfgKeys) {
        const va = cfgA[k], vb = cfgB[k];
        const sa = String(va == null ? "—" : va), sb = String(vb == null ? "—" : vb);
        const cls = sa !== sb ? "cmp-changed" : "";
        ch += `<tr><td>${esc(k)}</td><td class="${cls}">${esc(sa)}</td><td class="${cls}">${esc(sb)}</td></tr>`;
      }
      h += `<div class="cmp-sec">Configuration</div><table class="cmp-table"><tbody>${ch}</tbody></table>`;
    }

    // Tool trails side by side.
    const toolSeqA = (fullA && fullA.tool_sequence) || (ra && ra.tool_sequence) || [];
    const toolSeqB = (fullB && fullB.tool_sequence) || (rb && rb.tool_sequence) || [];
    const toolsA = toolSeqA.map((t) => (typeof t === "object" && t ? t.name : t)).filter(Boolean);
    const toolsB = toolSeqB.map((t) => (typeof t === "object" && t ? t.name : t)).filter(Boolean);
    if (toolsA.length || toolsB.length) {
      const chips = (list) => list.length
        ? list.map((n) => `<span class="run-tool-chip">${esc(n)}</span>`).join("")
        : '<span class="muted">none</span>';
      h += `<div class="cmp-sec">Tool trail</div>
        <div class="cmp-tools"><div class="cmp-col">${chips(toolsA)}</div><div class="cmp-vs">vs</div><div class="cmp-col">${chips(toolsB)}</div></div>`;
    }

    el.innerHTML = h;
  } catch (e) {
    el.innerHTML = `<div class="empty">Comparison failed: ${esc(e.message || e)}</div>`;
  }
}

// --------------------------------------------------------- timeline chart ----

function buildTimelineSvg(metric, W, opts) {
  const visibleIds = (opts && opts.visibleIds) || null;
  const inView = (id) => !visibleIds || visibleIds.has(id);
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
    + `<stop offset="0%" stop-color="${cssVar("--chart-line", "#a974ff")}" stop-opacity="0.35"/>`
    + `<stop offset="100%" stop-color="${cssVar("--chart-line", "#a974ff")}" stop-opacity="0"/></linearGradient></defs>`;

  for (let k = 0; k <= 4; k++) {
    const v = min + span * k / 4, yy = y(v);
    out += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="${cssVar("--chart-grid", "#332d44")}" stroke-width="0.5"></line>`;
    out += `<text x="${padL - 8}" y="${yy + 3}" text-anchor="end" font-size="10" fill="${cssVar("--chart-muted", "#9b93ab")}">${_fmtNum(v)}</text>`;
  }
  // Goal lines.
  for (const g of goalLines) {
    const gy = Math.max(padT, Math.min(H - padB, y(g.v)));
    out += `<g><line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="${g.color}" stroke-width="1.6" stroke-dasharray="7 5" opacity="0.9"></line>`
      + `<text x="${W - padR}" y="${gy - 5}" text-anchor="end" font-size="9.5" fill="${g.color}">goal ${esc(g.name)} ${_fmtNum(g.v)}</text></g>`;
  }

  const pts = nodes.map((n, i) => (vals[i] == null || !inView(n.id))
    ? null : `${xs[i]},${y(vals[i])}`).filter(Boolean);
  if (pts.length) {
    const linePts = pts.join(" ");
    const firstX = parseFloat(pts[0].split(",")[0]);
    const lastX = parseFloat(pts[pts.length - 1].split(",")[0]);
    const area = `${firstX},${y(min)} ${linePts} ${lastX},${y(min)}`;
    out += `<polygon points="${area}" fill="url(#tlfill)"></polygon>`;
    out += `<polyline points="${linePts}" fill="none" stroke="${cssVar("--chart-line", "#a974ff")}" stroke-width="2" filter="drop-shadow(0 0 6px ${cssVar("--chart-line-soft", "rgba(169,116,255,.5)")})"></polyline>`;
  }

  nodes.forEach((n, i) => {
    if (vals[i] == null || !inView(n.id)) return;
    const eid = n.experiment_id;
    const color = eid != null ? expColor(eid) : (n.fresh ? "#d29922" : "#b98cff");
    const sel = state.expSelected === n.id ? " selected" : "";
    const isBest = best && String(best.id) === String(n.id);
    const sug = reviewSuggestionsFor(n.id).length > 0;
    const fx = runFoxLabel(n);
    const tip = `Run #${i + 1} · ${n.label || ""}${n.fresh ? " (fresh)" : ""}\n${metric}: ${_fmtNum(vals[i])}\n${fx}\ntools: ${runToolsSummary(n) || "—"}\n${expOf(eid) ? "experiment: " + expOf(eid).name : ""}\n${sug ? "💡 reviewer suggestions available" : ""}\n${n.timestamp ? new Date(n.timestamp).toLocaleString() : ""}`;
    let mark = "";
    if (isBest) {
      mark = `<circle r="12" fill="none" stroke="${cssVar("--chart-title", "#f3f0fa")}" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.9"></circle>`;
    }
    out += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${xs[i]},${y(vals[i])})">`
      + `<title>${esc(tip)}</title>`
      + mark
      + `<circle r="7" fill="${color}" stroke="#19132b" stroke-width="2" filter="drop-shadow(0 0 6px ${color}aa)"></circle>`
      + (isBest ? `<text y="-20" text-anchor="middle" font-size="10">★</text>` : "")
      + (sug ? `<text y="-28" text-anchor="middle" font-size="10">💡</text>` : "")
      + `<text y="-12" text-anchor="middle" font-size="10" font-weight="700" fill="${color}">${_fmtNum(vals[i])}</text>`
      + `<text y="22" text-anchor="middle" font-size="9" fill="${cssVar("--chart-muted", "#9b93ab")}">#${i + 1}${n.label ? " " + esc(n.label.slice(0, 12)) : ""}</text>`
      + (fx ? `<text y="35" text-anchor="middle" font-size="8" fill="${color}" opacity="0.9">${esc(fx)}</text>` : "")
      + `</g>`;
  });

  out += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="${cssVar("--chart-title", "#f3f0fa")}">${metric.replace(/_/g, " ")} — evolution across runs (★ best · dashed = goal)</text>`;
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
  if (run.mcp && run.action) tag(run.mcp + "/" + run.action);
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
    p.x = Math.max(130, Math.min(W - 130, p.x));
    // keep the top-most nodes (and their sub-node spokes/labels) clear of the header
    p.y = Math.max(150, Math.min(H - 120, p.y));
  }
  return pos;
}

function buildGraphSvg(metric, W, opts) {
  const visibleIds = (opts && opts.visibleIds) || null;
  const inView = (id) => !visibleIds || visibleIds.has(id);
  const gnodes = (state.expGraph && state.expGraph.nodes) || [];
  const runs = state.expRuns || [];
  const edges = (state.expGraph && state.expGraph.edges) || [];
  const H = 580;
  if (!gnodes.length) return '<div class="empty">No runs yet.</div>';
  // Resolve each node's run by id (robust to experiment/time-slice filtering);
  // falls back to positional lookup for the unfiltered path.
  const runById = {};
  for (const r of runs) if (r && r.id != null) runById[r.id] = r;
  const nodes = gnodes.map((g, i) => ({
    id: g.id, seed: g.seed, fresh: g.fresh, index: i, run: runById[g.id] || runs[i] || {}, g,
  }));
  const drawNodes = visibleIds ? nodes.filter((n) => inView(n.id)) : nodes;
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
    if (!inView(e.source) || !inView(e.target)) continue;
    const sim = e.similarity || 0, ov = e.overlap || 0;
    const x1 = pos[a].x, y1 = pos[a].y, x2 = pos[b].x, y2 = pos[b].y;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    out += `<g class="exp-edge-wrap"><line class="exp-edge" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" `
      + `stroke-width="${(0.7 + sim * 3).toFixed(2)}" opacity="${(0.35 + sim * 0.45).toFixed(2)}"></line>`
      + `<text x="${mx}" y="${my - 4}" text-anchor="middle" font-size="8.5" fill="${cssVar("--chart-muted", "#9b93ab")}" paint-order="stroke" stroke="${cssVar("--chart-halo", "#0a0a0d")}" stroke-width="2.5">sim ${(sim * 100).toFixed(0)}% · ov ${(ov * 100).toFixed(0)}%</text></g>`;
  }

  // sub-node spokes (drawn under experiment nodes)
  for (const n of drawNodes) {
    const subs = expSubNodes(n.run);
    const R = 92;
    subs.forEach((s, i) => {
      const a = -Math.PI / 2 + i / subs.length * 2 * Math.PI;
      const sx = pos[n.index].x + Math.cos(a) * R;
      const sy = pos[n.index].y + Math.sin(a) * R;
      out += `<line x1="${pos[n.index].x}" y1="${pos[n.index].y}" x2="${sx}" y2="${sy}" `
        + `stroke="${cssVar("--chart-spoke", "#463a66")}" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.9"></line>`;
    });
  }

  // experiment nodes + sub-nodes
  drawNodes.forEach((n) => {
    const i = n.index;
    const v = vals[i];
    const color = v == null ? "#9b93ab" : _metricColor(v, vmin, vmax);
    const ec = expColor(n.g.experiment_id);
    const sel = state.expSelected === n.id ? " selected" : "";
    const bestForMetric = bestRunForMetric(metric);
    const isBest = bestForMetric && String(bestForMetric.id) === String(n.id);
    const sug = reviewSuggestionsFor(n.id).length > 0;
    const tip = `Run #${i + 1} · ${n.run.label || ""}${n.fresh ? " (fresh)" : ""}\n${metric}: ${_fmtNum(v)}\n${runFoxLabel(n.g)}\ntools: ${runToolsSummary(n.g) || "—"}\n${expOf(n.g.experiment_id) ? "experiment: " + expOf(n.g.experiment_id).name : ""}\n${sug ? "💡 reviewer suggestions available" : ""}\nclick for full summary`;
    out += `<g class="exp-node${sel}" data-id="${esc(n.id)}" transform="translate(${pos[i].x},${pos[i].y})">`
      + `<title>${esc(tip)}</title>`
      + (n.g.experiment_id != null
        ? `<circle r="21" fill="none" stroke="${ec}" stroke-width="2" opacity="0.75"></circle>` : "")
      + (isBest ? `<circle r="25" fill="none" stroke="${cssVar("--chart-title", "#f3f0fa")}" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"></circle>` : "")
      + `<circle r="16" fill="${color}" stroke="${cssVar("--chart-node-stroke", "#19132b")}" stroke-width="2.5" filter="drop-shadow(0 0 10px ${color}99)"></circle>`
      + (v != null ? `<text y="4" text-anchor="middle" font-size="11" font-weight="700" fill="#0a0a0d">${_fmtNum(v)}</text>` : "")
      + (isBest ? `<text y="-34" text-anchor="middle" font-size="11">★</text>` : "")
      + (sug ? `<text y="-46" text-anchor="middle" font-size="11">💡</text>` : "")
      + `<text y="-28" text-anchor="middle" font-size="11" font-weight="700" fill="${cssVar("--chart-title", "#f3f0fa")}">Run #${i + 1}</text>`
      + `<text y="34" text-anchor="middle" font-size="9" fill="${cssVar("--chart-muted", "#9b93ab")}">${esc((n.run.label || "run " + n.id).slice(0, 18))}</text></g>`;

    const subs = expSubNodes(n.run);
    const R = 92;
    subs.forEach((s, k) => {
      const a = -Math.PI / 2 + k / subs.length * 2 * Math.PI;
      const sx = pos[i].x + Math.cos(a) * R;
      const sy = pos[i].y + Math.sin(a) * R;
      out += `<g class="exp-subnode" data-id="${esc(n.id)}" transform="translate(${sx},${sy})">`
        + `<title>${esc(`${n.seed}: ${s.kind} — ${s.label}`)}</title>`
        + `<circle r="6.5" fill="${s.color}" stroke="${cssVar("--chart-node-stroke", "#19132b")}" stroke-width="1.2" filter="drop-shadow(0 0 5px ${s.color}aa)"></circle></g>`;
      // tiny visible label on the sub-node
      const lx = sx + Math.cos(a) * 14, ly = sy + Math.sin(a) * 14 + 3;
      out += `<text x="${lx}" y="${ly}" text-anchor="middle" font-size="8" fill="${s.color}" opacity="0.95" paint-order="stroke" stroke="${cssVar("--chart-halo", "#0a0a0d")}" stroke-width="2">${esc(s.label)}</text>`;
    });
  });

  // legend
  out += `<g transform="translate(${W - 290}, 12)">`
    + `<text x="0" y="-4" font-size="9" fill="${cssVar("--chart-muted", "#9b93ab")}">${metric.replace(/_/g, " ")}</text>`;
  for (let i = 0; i < 70; i++) {
    const t = i / 69;
    out += `<rect x="${i}" y="0" width="2" height="9" fill="${_metricColor(vmin + t * (vmax - vmin), vmin, vmax)}"></rect>`;
  }
  out += `<text x="0" y="20" font-size="8.5" fill="${cssVar("--chart-muted", "#9b93ab")}">${_fmtNum(vmin)}</text>`
    + `<text x="69" y="20" text-anchor="end" font-size="8.5" fill="${cssVar("--chart-muted", "#9b93ab")}">${_fmtNum(vmax)}</text>`
    + `<text x="78" y="9" font-size="9" fill="#d29922">● tag</text>`
    + `<text x="78" y="20" font-size="9" fill="#b98cff">● finding</text>`
    + `<text x="78" y="31" font-size="9" fill="#a974ff">● artifact</text></g>`;

  out += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="${cssVar("--chart-title", "#f3f0fa")}"><title>spokes = tags · findings · artifacts · edge labels = similarity/overlap · ring = experiment</title>experiment graph — ${metric.replace(/_/g, " ")}</text>`;
  out += `</svg>`;
  const legend = expLegend();
  return out + (legend ? `<div class="exp-chart-legend">${legend}</div>` : "");
}

function renderExperiments() {
  const runs = state.expRuns || [];
  const empty = '<div class="exp-empty">No runs yet in this project. Ask Fox to run an analysis or experiment in chat and each turn will appear here.</div>';
  const metric = expMetric();
  // Git-style run-kind filter: only chart runs whose kind matches.
  const kind = state.expKindFilter || "";
  let visibleIds = null;
  if (kind) {
    visibleIds = new Set((state.expGraph && state.expGraph.nodes || [])
      .filter((n) => (n.kind || "") === kind).map((n) => n.id));
  }
  const opts = visibleIds ? { visibleIds } : undefined;
  const charts = [
    ["expmain-timeline", "timeline", 1240, 330],
    ["expmain-graph", "graph", 1240, 580],
  ];
  for (const [id, kind_, w, h] of charts) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = runs.length
      ? (kind_ === "timeline" ? buildTimelineSvg(metric, w, opts) : buildGraphSvg(metric, w, opts))
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
  $("rkg-panel").classList.toggle("hidden", view !== "rkg");
  $("audit-panel").classList.toggle("hidden", view !== "audit");
  document.querySelectorAll(".mainview-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mainview === view));
  const fab = $("branch-toggle");
  if (fab) fab.classList.toggle("hidden", view !== "chat" && view !== "experiments");
  const ov = $("branch-overlay");
  if (ov && !ov.classList.contains("hidden")) {
    ov.classList.add("hidden");
    if (fab) fab.classList.remove("active");
  }
  const app = document.getElementById("app");
  if (view === "experiments" || view === "agent" || view === "editor" || view === "rkg" || view === "audit") {
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
  if (view === "rkg") loadRkg();
  if (view === "audit") loadAudit();
}

/* ============================ research knowledge graphs ============================ */

async function loadRkg() {
  const frame = $("rkg-frame");
  if (!frame) return;
  frame.src = (window.FOX_BASE || "") + "/rkg/dashboard";
  $("rkg-landscape-link").href = (window.FOX_BASE || "") + "/rkg/landscape";
  $("rkg-refresh").onclick = () => { frame.src = frame.src; };
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

/* ---- experiment run insights: reviewer suggestions + best-run compare ---- */

function reviewOf(runId) {
  return (state.agentRuns || []).find((r) => String(r.id) === String(runId)) || null;
}

function reviewSuggestionsFor(runId) {
  const r = reviewOf(runId);
  return (r && r.review && r.review.suggestions) || [];
}

function reviewFindingsFor(runId) {
  const r = reviewOf(runId);
  return (r && r.review && r.review.findings) || [];
}

function bestRunForMetric(metric) {
  const runs = state.expRuns || [];
  if (!metric) return null;
  let best = null;
  for (const r of runs) {
    const v = r.metrics && r.metrics[metric];
    if (v == null) continue;
    if (best === null || v > best.v) best = { id: r.id, v };
  }
  return best;
}

function suggestionLabel(s) {
  if (typeof s === "string") return s;
  if (!s) return "";
  return s.title || s.action || (typeof s.prompt === "string" ? s.prompt : JSON.stringify(s));
}

async function compareRunVsBest(runId, outEl) {
  const metric = expMetric();
  const best = bestRunForMetric(metric);
  if (!best) { outEl.textContent = "no runs with the active metric"; return; }
  if (String(best.id) === String(runId)) {
    outEl.textContent = "this is the best run for " + metric.replace(/_/g, " ") + " 🏆";
    return;
  }
  outEl.textContent = "Comparing…";
  try {
    const r = await api(`/api/projects/${state.project}/compare?run_a=${encodeURIComponent(runId)}&run_b=${encodeURIComponent(best.id)}`);
    const c = r.comparison;
    if (!c.rows.length) { outEl.textContent = "no shared metrics vs best"; return; }
    const rows = c.rows.map((row) => {
      const cls = row.delta > 0 ? "delta-up" : row.delta < 0 ? "delta-down" : "";
      const arrow = row.delta > 0 ? "▲" : row.delta < 0 ? "▼" : "—";
      return `<tr><td>${esc(row.metric)}</td><td>${_fmtNum(row.a)}</td><td>${_fmtNum(row.b)}</td>
        <td class="${cls}">${arrow} ${_fmtNum(row.delta)}</td>
        <td class="${cls}">${row.pct > 0 ? "+" : ""}${_fmtNum(row.pct)}%</td></tr>`;
    }).join("");
    outEl.innerHTML = `<div class="vs-head">this run vs <b>best</b> (run #${best.id})</div>
      <table class="cmp-table"><tbody>${rows}</tbody></table>`;
  } catch (e) { outEl.textContent = "compare failed: " + (e.message || e); }
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
  const fx = runFoxLabel(run);
  let h = `<div class="ed-head">${esc(run.label || ("Run #" + run.id))} ${badge}</div>`;
  h += `<div class="ed-meta">${esc(time)}</div>`;
  if (fx) h += `<div class="ed-fox">${esc(fx)}</div>`;
  h += `<div class="ed-actions">
    ${run.experiment_id != null
      ? `<button class="btn subtle small ed-improve" data-eid="${esc(run.experiment_id)}" data-rid="${esc(run.id)}" title="Run the improve loop for this experiment">🔁 Improve from here</button>` : ""}
    <button class="btn subtle small ed-vs-best" data-rid="${esc(run.id)}" title="Compare this run against the best run for the active metric">⇄ Compare vs best</button>
    <span class="ed-vs-out muted"></span>
  </div>`;

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
  if ((run.tools || []).length) {
    h += `<div class="ed-sec">Tool trail (MCP · action)</div>`;
    for (const t of run.tools) {
      const label = (t && t.mcp && t.action) ? `${t.mcp} · ${t.action}` : (t && t.name);
      h += `<div class="ed-find">${esc(label || "tool")}${t.ok ? "" : ' <span class="ed-fail">✗</span>'}</div>`;
    }
  }
  const sugs = reviewSuggestionsFor(run.id);
  if (sugs.length) {
    h += `<div class="ed-sec">💡 Suggested improvements</div>`;
    for (const s of sugs.slice(0, 4)) {
      h += `<div class="ed-find ed-sug">💡 ${esc(String(suggestionLabel(s)).replace(/\s+/g, " ").slice(0, 160))}</div>`;
    }
  }
  if ((run.findings || []).length) {
    h += `<div class="ed-sec">Findings</div>`;
    for (const f of run.findings) h += `<div class="ed-find">${esc(f)}</div>`;
  }
  const revFindings = reviewFindingsFor(run.id);
  if (revFindings.length && !(run.findings || []).length) {
    h += `<div class="ed-sec">Reviewer findings</div>`;
    for (const f of revFindings.slice(0, 4)) {
      const t = typeof f === "object" ? (f.message || JSON.stringify(f)) : String(f);
      h += `<div class="ed-find">${esc(String(t).replace(/\s+/g, " ").slice(0, 160))}</div>`;
    }
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
  expPan[id] = attachGraphPan(wrap, id, getSvg, ".exp-node, .exp-subnode", { noWheel: true });
  attachGraphControls(wrap, id, getSvg, 1240, id === "expmain-timeline" ? 330 : 580);
});
["expmain-timeline", "expmain-graph"].forEach((id) => {
  $(id).addEventListener("click", (e) => {
    if (expPan[id].drag && expPan[id].drag.moved) { expPan[id].drag.moved = false; return; }
    const n = e.target.closest(".exp-node, .exp-subnode");
    if (n && n.dataset.id) handleExpNodeClick(n.dataset.id);
  });
});

// Custom styled hover tooltip for experiment chart nodes.
let _expTipEl = null;
function expChartTip() {
  if (!_expTipEl) {
    _expTipEl = document.createElement("div");
    _expTipEl.className = "exp-chart-tip hidden";
    document.body.appendChild(_expTipEl);
  }
  return _expTipEl;
}
$("exp-panel").addEventListener("mouseover", (e) => {
  const tip = expChartTip();
  const n = e.target.closest(".exp-node");
  if (!n) { tip.classList.add("hidden"); return; }
  const t = n.querySelector("title");
  tip.textContent = t ? t.textContent : "";
  tip.classList.remove("hidden");
  tip.style.left = (e.clientX + 12) + "px";
  tip.style.top = (e.clientY + 12) + "px";
});
$("exp-panel").addEventListener("mousemove", (e) => {
  if (_expTipEl && !_expTipEl.classList.contains("hidden")) {
    _expTipEl.style.left = (e.clientX + 12) + "px";
    _expTipEl.style.top = (e.clientY + 12) + "px";
  }
});
["exp-detail", "expmain-detail"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", (e) => {
    const sim = e.target.closest(".ed-sim-link");
    if (sim) { selectRun(sim.dataset.id); return; }
    const improve = e.target.closest(".ed-improve");
    if (improve) {
      const eid = improve.dataset.eid;
      const rid = improve.dataset.rid;
      sendChat(`Improve the experiment toward its goal, continuing from run #${rid}.`,
               "improve_loop", { experiment_id: eid });
      return;
    }
    const vsBest = e.target.closest(".ed-vs-best");
    if (vsBest) {
      const out = vsBest.parentElement.querySelector(".ed-vs-out");
      if (out) compareRunVsBest(vsBest.dataset.rid, out);
      return;
    }
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
$("runs-export").addEventListener("click", () => {
  const eid = state.runsExpFilter || "";
  const q = (state.runsSearch || "").toLowerCase().trim();
  const runs = (state.agentRuns || []).filter((r) => {
    if (eid && String(r.experiment_id) !== String(eid)) return false;
    if (q && !((r.prompt || "") + " " + (r.label || "") + " " + (r.kind || "")).toLowerCase().includes(q)) return false;
    return true;
  });
  exportRunsCsv(runs, `runs-${state.project || "project"}.csv`);
});
$("exp-new-toggle").addEventListener("click", () => $("exp-new-form").classList.toggle("hidden"));
$("exp-new-create").addEventListener("click", createExp);
$("exp-search").addEventListener("input", (e) => { state.expSearch = e.target.value; state.expChunk = 0; renderExpList(); });
$("exp-sort").addEventListener("change", (e) => { state.expSort = e.target.value; state.expChunk = 0; renderExpList(); });
$("runs-search").addEventListener("input", (e) => { state.runsSearch = e.target.value; state.runsChunk = 0; renderRuns(); });
$("runs-exp-filter").addEventListener("change", (e) => { state.runsExpFilter = e.target.value; state.runsChunk = 0; renderRuns(); });
$("exp-chart-toggle").addEventListener("click", () => {
  const body = $("exp-chart-body");
  if (!body) return;
  const collapsed = body.classList.toggle("hidden");
  $("exp-chart-toggle").textContent = collapsed ? "Expand" : "Collapse";
});
initExpSectionNav();
$("campaign-new").addEventListener("click", () => $("campaign-new-form").classList.toggle("hidden"));
$("campaign-new-create").addEventListener("click", async () => {
  const name = ($("campaign-new-name").value || "Campaign").trim();
  const question = $("campaign-new-question").value.trim();
  const metric = $("campaign-new-metric").value.trim();
  try {
    const r = await api(`/api/projects/${state.project}/campaigns`, {
      method: "POST",
      body: JSON.stringify({ name, research_question: question, goal_metric: metric, higher_better: true }),
    });
    const cid = r.campaign.id;
    await api(`/api/projects/${state.project}/campaigns/${cid}/run`, { method: "POST", body: "{}" });
    $("campaign-new-form").classList.add("hidden");
    $("campaign-new-name").value = $("campaign-new-question").value = $("campaign-new-metric").value = "";
    await loadCampaigns();
    startCampaignPoll();
    toast("Campaign started in the background.");
  } catch (e) { toast("Failed to start campaign: " + e.message); }
});
$("exp-compare-refresh").addEventListener("click", loadCompareExperiments);
$("exp-next").addEventListener("click", async () => {
  try {
    await api(`/api/projects/${state.project}/next/post`, { method: "POST" });
    await refreshState();
    toast("Next-research agenda posted to chat.");
  } catch (e) { toast("Failed to load next-research agenda: " + e.message); }
});
$("exp-report").addEventListener("click", async () => {
  try {
    const r = await api(`/api/projects/${state.project}/report`, { method: "POST", body: "{}" });
    await refreshState();
    toast(`Project report generated (artifact ${r.artifact_id.slice(0, 8)}).`);
  } catch (e) { toast("Failed to generate report: " + e.message); }
});
$("exp-export").addEventListener("click", async () => {
  try {
    const res = await fetch(B(`/api/projects/${state.project}/export`), { method: "POST" });
    if (!res.ok) { toast("Export failed: HTTP " + res.status); return; }
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${state.project}-export.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    toast("Project exported as a zip bundle.");
  } catch (e) { toast("Export failed: " + e.message); }
});
$("eval-new").addEventListener("click", () => $("eval-new-form").classList.toggle("hidden"));
$("eval-new-create").addEventListener("click", async () => {
  const name = ($("eval-new-name").value || "Eval").trim();
  const metric = $("eval-new-metric").value.trim();
  const models = ($("eval-new-models").value || "").split(",").map((s) => s.trim()).filter(Boolean);
  const prompt = $("eval-new-prompt").value.trim();
  if (!models.length) { toast("Enter at least one model."); return; }
  try {
    const r = await api(`/api/projects/${state.project}/evals`, {
      method: "POST",
      body: JSON.stringify({ name, prompt, models, goal_metric: metric, higher_better: true }),
    });
    await api(`/api/projects/${state.project}/evals/${r.eval.id}/run`, { method: "POST" });
    $("eval-new-form").classList.add("hidden");
    $("eval-new-name").value = $("eval-new-metric").value = $("eval-new-models").value = $("eval-new-prompt").value = "";
    await loadEvals();
    startEvalPoll();
    toast("Model benchmark started in the background.");
  } catch (e) { toast("Failed to start eval: " + e.message); }
});
$("exp-edit-close").addEventListener("click", () => $("exp-edit-modal").classList.add("hidden"));
$("exp-detail-close").addEventListener("click", () => $("exp-detail-modal").classList.add("hidden"));
$("exp-edit-save").addEventListener("click", saveExpEdit);

function setExpMetric(v) {
  state.expMetric = v;
  if ($("exp-metric")) $("exp-metric").value = v;
  if ($("exp-metric-main")) $("exp-metric-main").value = v;
  renderExperiments();
}
if ($("exp-metric")) $("exp-metric").addEventListener("change", (e) => setExpMetric(e.target.value));
$("exp-metric-main").addEventListener("change", (e) => setExpMetric(e.target.value));
$("exp-branches").addEventListener("click", toggleBranches);
$("exp-kind-filter").addEventListener("change", (e) => {
  state.expKindFilter = e.target.value;
  renderExperiments();
});

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

/* ====================== agent audit trail view ====================== */

const AUDIT_SEV_COLOR = { info: "#9b93ab", warning: "#d29922", critical: "#e06c6c" };
const AUDIT_SOURCE_LABEL = { coordinator: "coordinator", mcp_proxy: "MCP", approval: "approval", middleware: "middleware", os_monitor: "OS", system: "system", policy: "policy", deviation: "deviation" };

function auditApi(path) { return api(`/api/projects/${encodeURIComponent(state.project)}/audit${path}`); }

function auditFilters() {
  return new URLSearchParams({
    agent: $("audit-agent").value || "",
    severity: $("audit-severity").value || "",
    source: $("audit-source").value || "",
    range: $("audit-range").value || "86400",
  });
}

async function loadAudit() {
  const panel = $("audit-panel");
  if (!panel) return;
  const view = document.querySelector(".audit-tab.active");
  const which = view ? view.dataset.auditview : "overview";
  try {
    const f = auditFilters();
    const range = f.get("range");
    const since = range && range !== "0" ? Date.now() / 1000 - Number(range) : "";
    const [summ, timeline, agents, chain] = await Promise.all([
      auditApi("/summary" + (since ? `?since=${since}` : "")).catch(() => ({ summary: {}, agents: [], tool_usage: [] })),
      auditApi("/timeline" + (since ? `?since=${since}` : "") + `&agent=${f.get("agent")}&severity=${f.get("severity")}&source=${f.get("source")}&limit=800`).catch(() => ({ events: [] })),
      auditApi("/agents").catch(() => ({ agents: [] })),
      auditApi("/verify").catch(() => ({ chain: {} })),
    ]);
    state.audit = { summary: summ.summary || {}, toolUsage: summ.tool_usage || [], agents: agents.agents || [], timeline: timeline.events || [], chain: chain.chain || {} };
    renderAuditKpis();
    renderAuditChain();
    populateAuditAgentSelect();
    if (which === "overview") renderAuditOverview();
  } catch (e) { /* silent */ }
}

function renderAuditChain() {
  const el = $("audit-chain-status");
  if (!el) return;
  const ch = (state.audit && state.audit.chain) || {};
  el.textContent = ch.events == null ? "chain —" : `🔗 ${ch.ok ? "✓" : "✗"} ${ch.events} chained`;
  el.title = JSON.stringify(ch);
}

function renderAuditKpis() {
  const el = $("audit-kpis");
  if (!el) return;
  const s = (state.audit && state.audit.summary) || {};
  const cards = [
    ["total", "Events", s.total || 0, ""],
    ["critical", "Critical", s.critical || 0, s.critical ? "sev-critical" : ""],
    ["overrides", "Overrides", s.overrides || 0, s.overrides ? "sev-warning" : ""],
    ["denials", "Denials", s.denials || 0, ""],
    ["data_access", "Data access", s.data_access || 0, ""],
    ["network", "Network", s.network || 0, ""],
    ["filesystem", "Filesystem", s.filesystem || 0, ""],
    ["deviations", "Open deviations", s.open_deviations || 0, s.open_deviations ? "sev-warning" : ""],
    ["agents", "Active agents", s.active_agents ? s.active_agents.length : 0, ""],
  ];
  el.innerHTML = cards.map(([k, v, n, cls]) =>
    `<div class="audit-kpi ${cls}${state.auditKpi === k ? " active" : ""}" data-kpi="${k}" title="Filter the list by ${esc(v)}"><div class="audit-kpi-v">${n}</div><div class="audit-kpi-k">${esc(v)}</div></div>`).join("");
  el.querySelectorAll(".audit-kpi").forEach((c) =>
    c.addEventListener("click", () => kpiFilterClick(c.dataset.kpi)));
}

function kpiFilterClick(kpi) {
  if (kpi === "deviations") { switchAuditView("deviations"); return; }
  if (kpi === "agents") { switchAuditView("agents"); return; }
  state.auditKpi = state.auditKpi === kpi ? "" : kpi;
  renderAuditKpis();
  renderAuditOverview();
}

function populateAuditAgentSelect() {
  const sel = $("audit-agent");
  if (!sel || !state.audit) return;
  const agents = (state.audit.agents || []).map((a) => a.agent_id);
  const prev = sel.value;
  sel.innerHTML = '<option value="">all agents</option>' +
    agents.map((a) => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
  sel.value = agents.includes(prev) ? prev : "";
}

function renderAuditOverview() {
  const f = auditFilters();
  const q = ($("audit-q").value || "").toLowerCase();
  let events = (state.audit && state.audit.timeline) || [];
  if (q) events = events.filter((e) => JSON.stringify(e).toLowerCase().includes(q));
  const kpi = state.auditKpi;
  if (kpi) {
    events = events.filter((e) => {
      switch (kpi) {
        case "critical": return e.severity === "critical";
        case "overrides": return e.policy === "OVERRIDE";
        case "denials": return e.policy === "DENY";
        case "data_access": return !!e.data_access;
        case "network": return !!e.network;
        case "filesystem": return !!e.filesystem;
        default: return true;
      }
    });
  }
  $("audit-count").textContent = `${events.length} event(s)`;
  // ordered event ids for the overlay's prev/next navigation
  state.auditEventIds = events.map((e) => e.event_id);
  $("audit-timeline").innerHTML = buildAuditTimeline(events);
  $("audit-timeline").querySelectorAll("[data-eid]").forEach((r) =>
    r.addEventListener("click", () => showAuditEvent(r.dataset.eid)));
}

function buildAuditTimeline(events) {
  const el = $("audit-timeline");
  if (!el) return "";
  if (!events.length) return '<div class="empty">No audit events match the filters. Ask Fox to run an analysis in chat and every tool call will appear here.</div>';

  // Distribution strip: event count per bucket (hour when range >= 1 day).
  const range = Number($("audit-range").value || 86400);
  const bucket = range >= 604800 ? 86400 : range >= 86400 ? 3600 : 60;
  const buckets = {};
  let t0 = Infinity, t1 = -Infinity;
  events.forEach((e) => {
    const ts = new Date(e.timestamp).getTime();
    const b = Math.floor(ts / 1000 / bucket) * bucket;
    buckets[b] = (buckets[b] || 0) + 1;
    t0 = Math.min(t0, ts); t1 = Math.max(t1, ts);
  });
  const keys = Object.keys(buckets).map(Number).sort((a, b) => a - b);
  const maxB = Math.max(1, ...Object.values(buckets));
  const W = 1200, H = 74, bw = keys.length ? (W - 20) / keys.length : 20;
  let strip = `<svg viewBox="0 0 ${W} ${H}" class="audit-strip">`;
  keys.forEach((k, i) => {
    const bh = Math.max(3, (buckets[k] / maxB) * (H - 26));
    strip += `<rect x="${10 + i * bw}" y="${H - 12 - bh}" width="${Math.max(2, bw - 2)}" height="${bh}" fill="#a974ff" opacity="${0.35 + 0.65 * (buckets[k] / maxB)}" rx="1.5"><title>${buckets[k]} event(s) at ${new Date(k * 1000).toLocaleString()}</title></rect>`;
  });
  strip += `</svg>`;

  let h = strip + '<div class="audit-tl">';
  let lastDay = "";
  events.forEach((e) => {
    const dt = new Date(e.timestamp);
    const day = dt.toLocaleDateString();
    if (day !== lastDay) {
      lastDay = day;
      h += `<div class="audit-tl-day">${esc(day)}</div>`;
    }
    const sev = e.severity || "info";
    const color = AUDIT_SEV_COLOR[sev] || "#9b93ab";
    const time = dt.toLocaleTimeString();
    const icon = e.network ? "🌐" : e.filesystem ? "📁" : e.policy ? (e.policy === "OVERRIDE" ? "⚠️" : "🔒") : "";
    const flags = [e.source ? AUDIT_SOURCE_LABEL[e.source] || e.source : "", sev, e.policy && e.policy !== "ALLOW" ? e.policy : "", ...(e.tags || []).filter((t) => t === "critical" || t === "high")].filter(Boolean).map((x) => `<span class="audit-tag tag-${x.toLowerCase().replace(/[^a-z0-9]/g, "")}">${esc(x)}</span>`).join("");
    const dur = e.duration_ms != null ? ` · ${e.duration_ms.toFixed(0)}ms` : "";
    h += `<div class="audit-tl-row" data-eid="${esc(e.event_id)}" data-sev="${sev}">`
      + `<div class="audit-tl-time">${esc(time)}${dur}</div>`
      + `<div class="audit-tl-track"><span class="audit-tl-dot" style="background:${color}"></span><span class="audit-tl-line"></span></div>`
      + `<div class="audit-tl-body">`
      + `<div class="audit-tl-title"><span class="audit-agent-badge">${esc(e.agent_id || "?")}</span> ${icon} ${esc(e.tool_name || e.method || "event")}</div>`
      + `<div class="audit-tl-meta">${flags}</div>`
      + `</div></div>`;
  });
  h += "</div>";
  return h;
}

async function showAuditEvent(eid) {
  const ov = $("audit-event-overlay");
  if (!ov) { return; }
  try {
    const r = await auditApi(`/event/${encodeURIComponent(eid)}`);
    const ev = r.event || {};
    state.auditEventId = eid;
    const pd = ev.policy_decision || {};
    const rs = ev.result_summary || {};
    let rows = "";
    const row = (k, v) => `<div class="ad-row"><span class="ad-k">${esc(k)}</span><span class="ad-v">${v == null ? "—" : esc(typeof v === "string" ? v : JSON.stringify(v, null, 2))}</span></div>`;
    rows += row("event_id", ev.event_id);
    rows += row("timestamp", ev.timestamp);
    rows += row("agent", ev.agent_id);
    rows += row("session / trace", (ev.session_id || "—") + " / " + (ev.trace_id || "—"));
    rows += row("source", ev.source + (ev.mcp_server ? ` (${ev.mcp_server})` : ""));
    rows += row("tool", ev.tool_name || ev.method);
    rows += row("severity", ev.severity);
    rows += row("duration_ms", ev.duration_ms);
    rows += row("arguments (redacted)", ev.arguments_redacted);
    rows += row("result summary", rs);
    if (ev.network) rows += row("network", ev.network);
    if (ev.filesystem) rows += row("filesystem", ev.filesystem);
    if (pd.outcome) rows += row("policy decision", pd);
    rows += row("chain", `prev ${(ev.prev_hash || "").slice(0, 16)}… → ${(ev.event_hash || "").slice(0, 16)}…`);
    // flag chips: source, severity, policy outcome, tags, network/filesystem,
    // duration — mirroring the timeline row badges.
    const sev = ev.severity || "info";
    const flags = [];
    if (ev.source) flags.push({ t: AUDIT_SOURCE_LABEL[ev.source] || ev.source, cls: "tag-" + ev.source.replace(/[^a-z0-9]/g, "") });
    if (sev) flags.push({ t: sev, cls: "tag-" + sev });
    if (pd.outcome) flags.push({ t: pd.outcome, cls: "tag-" + String(pd.outcome).toLowerCase() });
    if (ev.mcp_server) flags.push({ t: "MCP " + ev.mcp_server, cls: "tag-mcp" });
    for (const tag of ev.tags || []) {
      if (["critical", "high", "warning", "denied", "override", "permissions", "approval", "network", "filesystem", "middleware", "mcp"].includes(String(tag).toLowerCase())) {
        flags.push({ t: String(tag).toLowerCase(), cls: "tag-" + String(tag).toLowerCase() });
      }
    }
    if (ev.network) flags.push({ t: "network", cls: "tag-network" });
    if (ev.filesystem) flags.push({ t: "filesystem", cls: "tag-filesystem" });
    if (ev.duration_ms != null) flags.push({ t: ev.duration_ms.toFixed(0) + "ms", cls: "" });
    const flagsHtml = `<div class="audit-eo-flags">${flags.map((f) =>
      `<span class="audit-tag ${f.cls}">${esc(f.t)}</span>`).join("")}</div>`;
    $("audit-eo-body").innerHTML = flagsHtml + `<div class="ad-body">${rows}</div>`;
    $("audit-eo-id").textContent = eid;
    const ids = state.auditEventIds || [];
    const idx = ids.indexOf(eid);
    const prevBtn = $("audit-eo-prev"), nextBtn = $("audit-eo-next");
    if (prevBtn) prevBtn.disabled = idx <= 0;
    if (nextBtn) nextBtn.disabled = idx < 0 || idx >= ids.length - 1;
    ov.classList.remove("hidden");
  } catch (e) { /* silent */ }
}

function closeAuditEventOverlay() {
  const ov = $("audit-event-overlay");
  if (ov) ov.classList.add("hidden");
}

function auditEventNav(dir) {
  const ids = state.auditEventIds || [];
  const idx = ids.indexOf(state.auditEventId);
  const target = idx + dir;
  if (target >= 0 && target < ids.length) showAuditEvent(ids[target]);
}

function switchAuditView(view) {
  document.querySelectorAll(".audit-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.auditview === view));
  ["overview", "agents", "deviations", "permissions", "search"].forEach((v) => {
    const el = $("audit-" + v);
    if (el) el.classList.toggle("hidden", v !== view);
  });
  if (view === "overview") renderAuditOverview();
  if (view === "agents") loadAuditAgents();
  if (view === "deviations") loadAuditDeviations();
  if (view === "permissions") loadAuditPermissions();
  if (view === "search") loadAuditSearch();
}

/* ---------- agents ---------- */
async function loadAuditAgents() {
  const el = $("audit-agents");
  if (!el) return;
  const agents = (state.audit && state.audit.agents) || [];
  let h = '<div class="agent-card"><div class="agent-card-head">Agents</div>';
  if (!agents.length) { h += '<div class="empty">No audited agents yet.</div></div>'; el.innerHTML = h; return; }
  h += agents.map((a) => {
    const pct = a.criticals ? ` · <span class="sev-critical">${a.criticals} critical</span>` : "";
    return `<div class="audit-agent-row"><button class="audit-agent-btn" data-agent="${esc(a.agent_id)}">${esc(a.agent_id)}</button>`
      + `<span class="muted">${a.events} events${pct}</span>`
      + ` · <span class="muted">last ${a.last_ts ? new Date(a.last_ts * 1000).toLocaleString() : "—"}</span></div>`;
  }).join("");
  h += "</div>";
  el.innerHTML = h;
  el.querySelectorAll(".audit-agent-btn").forEach((b) =>
    b.addEventListener("click", () => showAuditAgent(b.dataset.agent)));
}

async function showAuditAgent(agentId) {
  const el = $("audit-agents");
  if (!el) return;
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const [hist, perms] = await Promise.all([
      auditApi(`/agents/${encodeURIComponent(agentId)}/history?limit=500`).catch(() => ({})),
      auditApi(`/agents/${encodeURIComponent(agentId)}/permissions`).catch(() => ({})),
    ]);
    const evs = hist.events || [];
    const usage = hist.tool_usage || [];
    const maxU = Math.max(1, ...usage.map((u) => u.count));
    let h = `<button class="btn subtle small audit-back">← agents</button>`;
    h += `<div class="agent-card"><div class="agent-card-head">${esc(agentId)} — audit history (${evs.length})</div>`;
    h += `<div class="audit-agent-metrics muted">data classes: ${(hist.data_classes || []).join(", ") || "—"} · network: ${(hist.network_destinations || []).join(", ") || "—"}</div>`;
    h += `<div class="audit-agent-chart">${usage.map((u) => {
      const w = Math.max(2, (u.count / maxU) * 100);
      return `<div class="audit-usage-row"><span class="audit-usage-label">${esc(u.tool)}</span><span class="audit-usage-bar-wrap"><span class="audit-usage-bar ${u.flags ? "sev-warning-bg" : ""}" style="width:${w}%"></span></span><span class="audit-usage-n">${u.count}</span></div>`;
    }).join("") || '<div class="empty">No tool usage recorded.</div>'}</div>`;
    h += "</div>";
    const p = perms || {};
    h += `<div class="agent-card"><div class="agent-card-head">Permission vs observed (${agentId})</div>`;
    h += `<div class="audit-perm-grid">`;
    h += `<div class="audit-perm-col"><div class="audit-perm-head">Granted / overridden</div>` + ((p.grants || []).length ? (p.grants || []).map((g) =>
      `<div class="audit-perm-item">${esc(g.kind || g.pattern)}<span class="muted">${esc((g.pattern || "").slice(0, 60))}</span>${g.overrides ? ` <span class="sev-warning">· ${g.overrides} override(s)</span>` : ""}</div>`).join("") : '<div class="muted">no grants recorded</div>') + `</div>`;
    h += `<div class="audit-perm-col"><div class="audit-perm-head">Observed tools</div>` + ((p.observed_tools || []).length ? (p.observed_tools || []).slice(0, 30).map((t) =>
      `<div class="audit-perm-item">${esc(t.tool)}<span class="muted">· ${t.count}</span></div>`).join("") : '<div class="muted">none observed</div>') + `</div>`;
    h += `</div></div>`;
    // recent events for this agent
    h += `<div class="agent-card"><div class="agent-card-head">Recent events</div>`;
    h += evs.slice(0, 200).map((e) => auditMiniEvent(e)).join("");
    h += "</div>";
    el.innerHTML = h;
    const back = el.querySelector(".audit-back");
    if (back) back.addEventListener("click", loadAuditAgents);
    el.querySelectorAll("[data-eid]").forEach((r) => r.addEventListener("click", () => showAuditEvent(r.dataset.eid)));
  } catch (e) { el.innerHTML = '<div class="empty">failed to load agent audit history</div>'; }
}

function auditMiniEvent(e) {
  const sev = e.severity || "info";
  const color = AUDIT_SEV_COLOR[sev] || "#9b93ab";
  const dt = new Date(e.timestamp);
  return `<div class="audit-mini" data-eid="${esc(e.event_id)}"><span class="audit-tl-dot" style="background:${color}"></span>`
    + `<span class="muted">${esc(dt.toLocaleTimeString())}</span> ${esc(e.tool_name || e.method || "event")}`
    + (e.policy ? ` <span class="audit-tag tag-${esc(e.policy.toLowerCase())}">${esc(e.policy)}</span>` : "")
    + `</div>`;
}

/* ---------- deviations ---------- */
async function loadAuditDeviations() {
  const el = $("audit-deviations");
  if (!el) return;
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const r = await auditApi("/deviations?limit=200");
    const devs = r.deviations || [];
    const open = devs.filter((d) => !d.reviewed);
    const closed = devs.filter((d) => d.reviewed);
    let h = `<div class="agent-card"><div class="agent-card-head">Open deviations (${open.length})</div>`;
    h += open.length ? open.map((d) => auditDeviationCard(d)).join("") : '<div class="empty">No open deviations. Run a scan after more activity.</div>';
    h += "</div>";
    h += `<div class="agent-card"><div class="agent-card-head">Reviewed / false positives (${closed.length})</div>`;
    h += closed.length ? closed.map((d) => auditDeviationCard(d)).join("") : '<div class="empty">None.</div>';
    h += "</div>";
    el.innerHTML = h;
    bindDeviationButtons(el);
  } catch (e) { el.innerHTML = '<div class="empty">failed to load deviations</div>'; }
}

function auditDeviationCard(d) {
  const color = AUDIT_SEV_COLOR[d.severity] || "#d29922";
  return `<div class="audit-dev" style="border-left-color:${color}">`
    + `<div class="audit-dev-head"><span class="audit-dev-sev" style="color:${color}">${esc(d.severity.toUpperCase())}</span> ${esc(d.rule)} <span class="muted">· ${esc(d.agent_id)} · ${esc(new Date(d.created_at * 1000).toLocaleString())}</span></div>`
    + `<div class="audit-dev-exp">${esc(d.explanation)}</div>`
    + (Object.keys(d.detail || {}).length ? `<pre class="audit-dev-detail">${esc(JSON.stringify(d.detail, null, 2))}</pre>` : "")
    + `<div class="audit-dev-actions"><span class="muted">${(d.event_ids || []).length} linked event(s)</span>`
    + (d.reviewed ? `<span class="muted">${d.false_positive ? "false positive" : "reviewed"}${d.reviewed_by ? " by " + esc(d.reviewed_by) : ""}</span>`
        : `<span class="spacer"></span><button class="btn subtle small audit-dev-fp" data-id="${esc(d.deviation_id)}">False positive</button>`
        + `<button class="btn subtle small audit-dev-done" data-id="${esc(d.deviation_id)}">Mark reviewed</button>`)
    + `</div></div>`;
}

function bindDeviationButtons(root) {
  root.querySelectorAll(".audit-dev-done").forEach((b) => b.addEventListener("click", () =>
    auditReview(b.dataset.id, { reviewed: true, reviewed_by: "ui" })));
  root.querySelectorAll(".audit-dev-fp").forEach((b) => b.addEventListener("click", () =>
    auditReview(b.dataset.id, { reviewed: true, false_positive: true, reviewed_by: "ui" })));
}

async function auditReview(id, body) {
  try { await auditApi(`/deviations/${encodeURIComponent(id)}/review`, { method: "POST", body: JSON.stringify(body) }); }
  catch (e) { /* silent */ }
  loadAuditDeviations();
}

/* ---------- permissions ---------- */
async function loadAuditPermissions() {
  const el = $("audit-permissions");
  if (!el) return;
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const agents = (state.audit && state.audit.agents) || [];
    if (!agents.length) { el.innerHTML = '<div class="empty">No audited agents yet.</div>'; return; }
    let h = "";
    for (const a of agents) {
      const p = await auditApi(`/agents/${encodeURIComponent(a.agent_id)}/permissions`).catch(() => ({}));
      h += `<div class="agent-card"><div class="agent-card-head">${esc(a.agent_id)} — permissions</div>`;
      h += `<div class="audit-perm-grid">`;
      h += `<div class="audit-perm-col"><div class="audit-perm-head">Granted / overridden</div>` +
        ((p.grants || []).length ? (p.grants || []).map((g) =>
          `<div class="audit-perm-item">${esc(g.kind || g.pattern)} <span class="muted">${esc((g.pattern || "").slice(0, 70))}</span>${g.overrides ? ` <span class="sev-warning">· ${g.overrides} override(s)</span>` : ""}</div>`).join("")
          : '<div class="muted">no grants recorded yet</div>') + `</div>`;
      h += `<div class="audit-perm-col"><div class="audit-perm-head">Observed tools</div>` +
        ((p.observed_tools || []).slice(0, 40).map((t) =>
          `<div class="audit-perm-item">${esc(t.tool)} <span class="muted">· ${t.count}</span></div>`).join("")
          || '<div class="muted">none observed</div>') + `</div>`;
      h += `</div></div>`;
    }
    el.innerHTML = h;
  } catch (e) { el.innerHTML = '<div class="empty">failed to load permissions</div>'; }
}

/* ---------- investigation / search ---------- */
async function loadAuditSearch() {
  const el = $("audit-search");
  if (!el) return;
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const f = auditFilters();
    const range = f.get("range");
    const since = range && range !== "0" ? Date.now() / 1000 - Number(range) : "";
    const r = await auditApi(`/events?limit=1000` + (since ? `&since=${since}` : "") +
      `&agent=${f.get("agent")}&severity=${f.get("severity")}&source=${f.get("source")}` +
      (($("audit-q").value || "") ? `&q=${encodeURIComponent($("audit-q").value)}` : ""));
    const evs = r.events || [];
    let h = `<div class="agent-card"><div class="agent-card-head">Event search — ${evs.length} of ${r.total || evs.length}</div>`;
    h += evs.map((e) => auditMiniEvent(e)).join("");
    h += "</div>";
    el.innerHTML = h;
    el.querySelectorAll("[data-eid]").forEach((r2) => r2.addEventListener("click", () => showAuditEvent(r2.dataset.eid)));
  } catch (e) { el.innerHTML = '<div class="empty">failed to load events</div>'; }
}

/* ---------- audit view wiring ---------- */
document.querySelectorAll(".audit-tab").forEach((b) =>
  b.addEventListener("click", () => switchAuditView(b.dataset.auditview)));
["audit-agent", "audit-severity", "audit-source", "audit-range"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("change", () => { loadAudit(); });
});
const auditQ = $("audit-q");
if (auditQ) auditQ.addEventListener("input", () => { clearTimeout(auditQ._t); auditQ._t = setTimeout(() => { loadAudit(); renderAuditOverview(); }, 400); });
const auditRefresh = $("audit-refresh");
if (auditRefresh) auditRefresh.addEventListener("click", loadAudit);
const auditScan = $("audit-scan");
if (auditScan) auditScan.addEventListener("click", async () => {
  try {
    const r = await auditApi("/scan", { method: "POST", body: "{}" });
    toast(`Deviation scan recorded ${r.recorded || 0} new deviation(s)`);
    loadAudit();
  } catch (e) { toast("scan failed"); }
});
const auditExport = $("audit-export");
if (auditExport) auditExport.addEventListener("click", () => {
  window.open(B(`/api/projects/${encodeURIComponent(state.project)}/audit/export?fmt=json&limit=2000`), "_blank");
});

// Audit event detail overlay wiring.
const auditEoClose = $("audit-eo-close");
if (auditEoClose) auditEoClose.addEventListener("click", closeAuditEventOverlay);
const auditEoPrev = $("audit-eo-prev");
if (auditEoPrev) auditEoPrev.addEventListener("click", () => auditEventNav(-1));
const auditEoNext = $("audit-eo-next");
if (auditEoNext) auditEoNext.addEventListener("click", () => auditEventNav(1));
const auditEoOv = $("audit-event-overlay");
if (auditEoOv) auditEoOv.addEventListener("click", (e) => {
  if (e.target === auditEoOv) closeAuditEventOverlay();
});

/* ===================== experiment branch history (git-flow) ===================== */

let branchView = "timeline";
let branchExpChoice = null;  // explicit user choice; null = auto (active experiment)
let branchTimeIdx = null;    // runs revealed by the evolution slider; null = all
let branchStatusFilter = ""; // "" | "ok" | "error"
let branchCompact = true;    // collapse linear runs between forks
let branchPlayInt = 0;       // evolution play interval (1 run / second)
let branchMetricUserPicked = false;  // user explicitly changed the metric

async function loadBranches() {
  const graphEl = $("branch-graph");
  if (!graphEl) return;
  try {
    // always refetch the timeline/graph data too, so newly collected runs and
    // their metrics show up for both new and old runs (not a stale cache)
    state._branchGraphProject = null;
    const [r, st] = await Promise.all([
      api(`/api/projects/${encodeURIComponent(state.project)}/experiments/branches`),
      api(`/api/projects/${encodeURIComponent(state.project)}/state`),
    ]);
    state.branches = r || { nodes: [], edges: [], experiments: [], tips: [] };
    if (st && Array.isArray(st.artifacts)) state.artifacts = st.artifacts;
    await loadBranchGraphData();   // expList/expRuns for the insight cards
    await switchBranchView(branchView);
    renderBranchInsights();
    if (state.branchSelected) showBranchDetail(state.branchSelected);
  } catch (e) {
    graphEl.innerHTML = '<div class="empty">Could not load branch history.</div>';
  }
}

async function loadBranchGraphData() {
  // The timeline / graph views reuse the Experiments-panel renderers, so they
  // need the same state they do (/experiments/graph + /experiments + /runs).
  // Fetch fresh for the current project (the global exp* state is shared and
  // can be stale when the overlay is opened before the Experiments tab).
  const proj = state.project;
  if (state._branchGraphProject === proj) return;
  try {
    const [g, exps, runs, hist] = await Promise.all([
      api(`/api/projects/${encodeURIComponent(proj)}/experiments/graph`),
      api(`/api/projects/${encodeURIComponent(proj)}/experiments`),
      api(`/api/projects/${encodeURIComponent(proj)}/runs`),
      api(`/api/projects/${encodeURIComponent(proj)}/experiments/history`),
    ]);
    state.expGraph = g;
    state.expList = exps.experiments || [];
    state.agentRuns = runs.runs || [];
    state.expRuns = hist.experiments || [];
    state._branchGraphProject = proj;
    // make sure the overlay's experiment filter tracks the current experiment
    if (state.activeExperiment == null ||
        !state.expList.some((e) => String(e.id) === String(state.activeExperiment))) {
      detectActiveExperiment();
    }
  } catch (e) { /* silent */ }
}

function populateBranchMetric() {
  const nodes = (state.expGraph && state.expGraph.nodes) || [];
  // only list metrics that actually have a value in the runs being shown
  const withValue = new Set();
  nodes.forEach((n) => {
    for (const k of Object.keys(n.metrics || {})) {
      if (expNodeValue(n, k) != null) withValue.add(k);
    }
  });
  const opts = [...withValue].sort();
  const sel = $("branch-metric");
  if (!sel) return;
  sel.innerHTML = opts.map((k) => `<option value="${esc(k)}">${esc(k.replace(/_/g, " "))}</option>`).join("");
  // accuracy is the default whenever it's available, until the user picks one
  if (!branchMetricUserPicked || !opts.includes(state.branchMetric)) {
    state.branchMetric = preferMetricDefault(opts);
  }
  sel.value = state.branchMetric;
}

function populateBranchExpFilter() {
  const sel = $("branch-exp-filter");
  if (!sel) return;
  const exps = state.expList || [];
  sel.innerHTML = `<option value="">all experiments</option>` +
    exps.map((e) => `<option value="${e.id}">${esc(e.name)}</option>`).join("");
  let target;
  if (branchExpChoice !== null) {
    target = branchExpChoice;
    // stale choice (e.g. the project was switched) -> fall back to current exp
    if (target !== "" && !exps.some((e) => String(e.id) === target)) {
      branchExpChoice = null;
      target = (state.activeExperiment != null) ? String(state.activeExperiment) : "";
    }
  } else {
    // first open: default to the current experiment so timeline/graph show it
    target = (state.activeExperiment != null) ? String(state.activeExperiment) : "";
  }
  sel.value = (target === "" || exps.some((e) => String(e.id) === target)) ? target
    : (exps[0] ? String(exps[0].id) : "");
}

function filterGraphForExp(graph, eid) {
  if (!eid || !graph) return graph;
  const nodeIds = new Set((graph.nodes || []).filter((n) => String(n.experiment_id) === String(eid)).map((n) => n.id));
  return {
    nodes: (graph.nodes || []).filter((n) => nodeIds.has(n.id)),
    edges: (graph.edges || []).filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target)),
  };
}

async function switchBranchView(view) {
  const prev = branchView;
  branchView = view;
  if (prev !== view) branchTimeIdx = null;  // fresh evolution scrub per view
  document.querySelectorAll(".branch-view-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.branchview === view));
  const metricSel = $("branch-metric");
  const expSel = $("branch-exp-filter");
  const showCharts = view !== "branches";
  if (metricSel) metricSel.style.display = showCharts ? "" : "none";
  if (expSel) expSel.style.display = "";
  const filters = $("branch-filters");
  if (filters) filters.style.display = "";   // evolution slider + status filter in all views
  const compactBtn = $("branch-compact");
  if (compactBtn) compactBtn.style.display = view === "branches" ? "" : "none";
  if (view === "branches") { renderBranchGraph(); return; }
  await loadBranchGraphData();
  populateBranchExpFilter();
  populateBranchMetric();
  renderBranchGraphView();
}

// Visible run ids for the evolution slider + status filter on the Graph view.
// Orders graph nodes chronologically (started_at from the branches payload,
// falling back to the graph's index), then reveals the first `branchTimeIdx`.
function branchGraphVisibleIds(gnodes) {
  const bmap = {};
  for (const b of (state.branches && state.branches.nodes) || []) bmap[b.id] = b;
  const ordered = gnodes.map((g, i) => ({
    id: g.id, i,
    st: (bmap[g.id] && bmap[g.id].started_at) || null,
    status: (bmap[g.id] && bmap[g.id].status) || "",
  }));
  const statusOk = (r) => branchStatusFilter === "" ||
    (branchStatusFilter === "error" ? r.status === "error" : r.status !== "error");
  const filtered = ordered.filter(statusOk);
  syncBranchTimeControls(filtered.length);
  if (!filtered.length) return new Set();
  filtered.sort((a, b) => (a.st - b.st) || (a.i - b.i));
  const reveal = branchTimeIdx == null ? filtered.length
    : Math.max(1, Math.min(filtered.length, branchTimeIdx));
  const vis = new Set();
  for (let k = 0; k < reveal; k++) vis.add(filtered[k].id);
  return vis;
}

// Timeline / Graph branch views share the Experiments-panel builders; render
// them here with the current experiment filter, metric and (graph-only)
// evolution/status slice. Cheap enough to call on every slider input.
function renderBranchGraphView() {
  const eid = $("branch-exp-filter") ? $("branch-exp-filter").value : "";
  const graph = filterGraphForExp(state.expGraph, eid);
  const origGraph = state.expGraph;
  state.expGraph = graph;              // the builders read state.expGraph
  const el = $("branch-graph");
  if (!el) return;
  const gnodes = graph.nodes || [];
  if (!gnodes.length) { state.expGraph = origGraph; el.innerHTML = '<div class="empty">No runs yet in this project.</div>'; return; }
  const metric = state.branchMetric || "";
  const W = 1240;
  const H = branchView === "timeline" ? 330 : 580;
  let visibleIds = null;
  if (branchView === "graph" || branchView === "timeline") {
    visibleIds = branchGraphVisibleIds(gnodes);
    if (visibleIds && !visibleIds.size) {
      state.expGraph = origGraph;
      el.innerHTML = '<div class="empty">No runs match the current filters.</div>';
      return;
    }
  }
  el.innerHTML = branchView === "timeline"
    ? buildTimelineSvg(metric, W, { visibleIds })
    : buildGraphSvg(metric, W, { visibleIds });
  state.expGraph = origGraph;
  const svg = el.querySelector("svg");
  if (svg) {
    graphViewRestore(svg, "branch-" + branchView, W, H);
    attachGraphControls(el, "branch-" + branchView, () => el.querySelector("svg"), W, H);
  }
  // clicking a run shows it in the detail pane (no full re-render)
  el.querySelectorAll(".exp-node").forEach((nd) => nd.addEventListener("click", () => {
    const rid = Number(nd.dataset.id);
    state.expSelected = rid;
    state.branchSelected = rid;
    showBranchDetail(rid);
    el.querySelectorAll(".exp-node.selected").forEach((x) => x.classList.remove("selected"));
    nd.classList.add("selected");
  }));
}

function branchRerenderCurrent() {
  if (branchView === "branches") { renderBranchGraph(); return; }
  renderBranchGraphView();
}

// ----------------------------------------------------------------------------
// Below-the-graph insight cards: experiments, recent runs, goals, run
// comparison and artifacts, all drawn from the loaded branch/experiment state.

function renderBranchInsights() {
  const exps = $("bi-experiments"), recent = $("bi-recent"), goals = $("bi-goals"),
        cmp = $("bi-compare"), arts = $("bi-artifacts");
  if (!exps && !recent && !goals && !cmp && !arts) return;
  const branches = state.branches || {};
  const expData = (state.expList && state.expList.length) ? state.expList
    : (branches.experiments || []);
  const runData = (state.expRuns && state.expRuns.length) ? state.expRuns
    : (branches.nodes || []);
  const runCountOf = (eid) =>
    (branches.nodes || []).filter((n) => String(n.experiment_id) === String(eid)).length;
  const best = branchBestNodes(branches.nodes || []);

  // -- experiments -------------------------------------------------------
  if (exps) {
    if (!expData.length) {
      exps.innerHTML = '<div class="muted">No experiments yet.</div>';
    } else {
      exps.innerHTML = expData.slice(0, 12).map((e) => {
        const rc = e.run_count != null ? e.run_count : runCountOf(e.id);
        const goal = e.goal_metric
          ? `${esc(e.goal_metric)} ${e.higher_better ? "↑" : "↓"}${e.goal_target != null ? " → " + _fmtNum(e.goal_target) : ""}`
          : "no goal";
        const st = e.status || "active";
        const badge = st === "active" ? "det" : (st === "completed" ? "ok" : "warn");
        return `<button class="bi-exp" data-id="${esc(e.id)}">
          <span class="exp-legend-dot" style="background:${expColor(e.id)}"></span>
          <b>${esc(e.name)}</b>
          <span class="exp-badge ${badge}">${esc(st)}</span>
          <span class="muted">${rc} run(s) · goal ${goal}</span>
        </button>`;
      }).join("");
    }
    exps.querySelectorAll(".bi-exp").forEach((b) => b.addEventListener("click", () => {
      branchExpChoice = b.dataset.id;
      const sel = $("branch-exp-filter");
      if (sel && sel.options.length) sel.value = b.dataset.id;
      switchBranchView(branchView);
    }));
  }

  // -- recent runs -------------------------------------------------------
  if (recent) {
    const sorted = [...runData].sort(
      (a, b) => ((b.started_at || 0) - (a.started_at || 0)) || (b.id - a.id));
    if (!sorted.length) {
      recent.innerHTML = '<div class="muted">No runs yet.</div>';
    } else {
      recent.innerHTML = sorted.slice(0, 10).map((n) => {
        const failed = n.status === "error";
        const meta = [
          n.experiment_name ? "· " + n.experiment_name : "",
          n.goal_value != null ? `· ${n.goal_metric}=${_fmtNum(n.goal_value)}` : "",
          n.started_at ? `· ${new Date(n.started_at * 1000).toLocaleDateString()}` : "",
        ].filter(Boolean).join(" ");
        return `<button class="bi-run" data-id="${esc(n.id)}">
          <span class="bi-status ${failed ? "fail" : "ok"}"></span>
          <b>#${esc(n.id)}</b> <span>${esc((n.label || n.kind || "").slice(0, 26))}</span>
          <span class="muted">${meta}</span>
        </button>`;
      }).join("");
    }
    recent.querySelectorAll(".bi-run").forEach((b) => b.addEventListener("click", () => {
      state.branchSelected = Number(b.dataset.id);
      state.expSelected = state.branchSelected;
      showBranchDetail(state.branchSelected);
      branchRerenderCurrent();
    }));
  }

  // -- goals -------------------------------------------------------------
  if (goals) {
    const withGoal = expData.filter((e) => e.goal_metric);
    if (!withGoal.length) {
      goals.innerHTML = '<div class="muted">No goals defined — add a goal metric to an experiment.</div>';
    } else {
      goals.innerHTML = withGoal.map((e) => {
        const b = best[e.id];
        const cur = (b && b.goal_value != null)
          ? `${e.goal_metric}=${_fmtNum(b.goal_value)}` : "no best run yet";
        const target = e.goal_target != null ? "target " + _fmtNum(e.goal_target) : "no target";
        const reached = e.goal_target != null && b && b.goal_value != null &&
          (e.higher_better !== false
            ? Number(b.goal_value) >= Number(e.goal_target)
            : Number(b.goal_value) <= Number(e.goal_target));
        return `<div class="bi-goal">
          <span class="exp-legend-dot" style="background:${expColor(e.id)}"></span>
          <b>${esc(e.name)}</b> — ${esc(e.goal_metric)} ${e.higher_better ? "↑" : "↓"} ${target}
          <span class="muted">best: ${cur}</span>
          ${reached ? '<span class="exp-badge ok">✓ reached</span>' : ""}
        </div>`;
      }).join("");
    }
  }

  // -- run comparison ----------------------------------------------------
  if (cmp) {
    const runs = branches.nodes || [];
    if (!runs.length) {
      cmp.innerHTML = '<div class="muted">No runs yet to compare.</div>';
    } else {
      const opts = [...runs].sort((a, b) => (b.started_at || 0) - (a.started_at || 0))
        .map((n) => `<option value="${esc(n.id)}">#${esc(n.id)} ${esc((n.label || n.kind || "").slice(0, 40))}</option>`).join("");
      cmp.innerHTML = `<div class="bi-cmp">
          <select id="bi-cmp-a">${opts}</select>
          <span class="muted">vs</span>
          <select id="bi-cmp-b">${opts}</select>
          <button id="bi-cmp-go" class="btn subtle small">Compare</button>
          <div id="bi-cmp-result" class="bi-cmp-result"></div>
        </div>`;
      const selA = $("bi-cmp-a"), selB = $("bi-cmp-b");
      if (selA && selB && selB.options.length > 1) selB.selectedIndex = 1;
      $("bi-cmp-go").addEventListener("click", () => {
        renderBranchCompare(selA ? selA.value : "", selB ? selB.value : "");
      });
    }
  }

  // -- artifacts ---------------------------------------------------------
  if (arts) {
    const arr = state.artifacts || [];
    if (!arr.length) {
      arts.innerHTML = '<div class="muted">No artifacts yet — figures and saved tables appear here.</div>';
    } else {
      arts.innerHTML = arr.slice(0, 14).map((a) => `
        <button class="bi-art" data-id="${esc(a.id)}">
          ${a.data_type === "png"
            ? `<img class="bi-art-thumb" src="${B(`/artifacts/${a.id}`)}" alt="">`
            : `<span class="bi-art-ic">📄</span>`}
          <span class="bi-art-name">${esc(a.name)}</span>
          <span class="muted">${esc(a.kind || "")}</span>
        </button>`).join("");
    }
    arts.querySelectorAll(".bi-art").forEach((b) => b.addEventListener("click", () => {
      const a = (state.artifacts || []).find((x) => String(x.id) === String(b.dataset.id));
      if (a) openArtifact(a);
    }));
  }
}

async function renderBranchCompare(a, b) {
  const el = $("bi-cmp-result");
  if (!el) return;
  if (!a || !b) { el.innerHTML = ""; return; }
  el.innerHTML = '<div class="muted">Comparing…</div>';
  try {
    const r = await api(`/api/projects/${state.project}/compare?run_a=${encodeURIComponent(a)}&run_b=${encodeURIComponent(b)}`);
    const c = r.comparison;
    if (!c.rows.length) {
      el.innerHTML = '<div class="muted">No shared numeric metrics between the two runs.</div>';
      return;
    }
    let rows = `<tr><th>metric</th><th>${esc(c.a)}</th><th>${esc(c.b)}</th><th>Δ</th></tr>`;
    for (const row of c.rows) {
      const arrow = row.delta > 0 ? "▲" : row.delta < 0 ? "▼" : "—";
      rows += `<tr><td>${esc(row.metric)}</td><td>${_fmtNum(row.a)}</td><td>${_fmtNum(row.b)}</td>
        <td class="${row.delta > 0 ? "delta-up" : row.delta < 0 ? "delta-down" : ""}">${arrow} ${_fmtNum(row.delta)}</td></tr>`;
    }
    el.innerHTML = `<table class="cmp-table"><tbody>${rows}</tbody></table>`;
  } catch (e) {
    el.innerHTML = `<div class="muted">Comparison failed: ${esc(e.message || e)}</div>`;
  }
}

document.querySelectorAll(".branch-view-btn").forEach((b) =>
  b.addEventListener("click", () => switchBranchView(b.dataset.branchview)));
const branchMetricSel = $("branch-metric");
if (branchMetricSel) branchMetricSel.addEventListener("change", (e) => {
  state.branchMetric = e.target.value;
  branchMetricUserPicked = true;
  switchBranchView(branchView);
});
const branchExpSel = $("branch-exp-filter");
if (branchExpSel) branchExpSel.addEventListener("change", () => {
  branchExpChoice = $("branch-exp-filter").value;
  switchBranchView(branchView);
});

const branchStatusSel = $("branch-status-filter");
if (branchStatusSel) branchStatusSel.addEventListener("change", (e) => {
  branchStatusFilter = e.target.value;
  branchRerenderCurrent();
});
const branchCompactBtn = $("branch-compact");
if (branchCompactBtn) branchCompactBtn.addEventListener("click", () => {
  branchCompact = !branchCompact;
  branchCompactBtn.classList.toggle("active", branchCompact);
  branchRerenderCurrent();
});
const branchTimeSel = $("branch-time");
if (branchTimeSel) branchTimeSel.addEventListener("input", () => {
  stopBranchPlay();
  branchTimeIdx = Number(branchTimeSel.value);
  branchRerenderCurrent();
});
const branchTimeFull = $("branch-time-full");
if (branchTimeFull) branchTimeFull.addEventListener("click", () => {
  stopBranchPlay();
  branchTimeIdx = null;
  branchRerenderCurrent();
});
const branchPlayBtn = $("branch-play");
if (branchPlayBtn) branchPlayBtn.addEventListener("click", () => {
  if (branchPlayInt) { stopBranchPlay(); return; }
  const s = $("branch-time");
  if (!s) return;
  const max = Number(s.max);
  let cur = Number(s.value);
  if (cur >= max) cur = 1; // restart from the beginning
  branchPlayBtn.textContent = "⏸";
  branchPlayInt = setInterval(() => {
    if (cur >= max) { stopBranchPlay(); return; }
    cur += 1;                       // one run per second
    branchTimeIdx = cur;
    syncBranchTimeControls(max);
    branchRerenderCurrent();
    if (cur >= max) stopBranchPlay();
  }, 1000);
});

function toggleBranches() {
  const ov = $("branch-overlay");
  if (!ov) return;
  const show = ov.classList.contains("hidden");
  ov.classList.toggle("hidden", !show);
  $("branch-toggle").classList.toggle("active", show);
  if (show) {
    loadBranches();
  }
}

function assignBranchLanes(nodes, edges) {
  const parentOf = {};
  const childrenOf = {};
  for (const e of edges || []) {
    parentOf[e.child] = e.parent;
    (childrenOf[e.parent] = childrenOf[e.parent] || []).push(e.child);
  }
  const lane = {};
  const expLane = {};
  let freeLane = 0;
  const sorted = [...nodes].sort(
    (a, b) => (a.started_at || 0) - (b.started_at || 0) || (a.id - b.id));
  for (const n of sorted) {
    const p = parentOf[n.id];
    if (p !== undefined && lane[p] !== undefined) {
      const placedSibs = (childrenOf[p] || []).filter((s) => lane[s] !== undefined).length;
      lane[n.id] = lane[p] + placedSibs;
    } else if (n.experiment_id != null) {
      if (expLane[n.experiment_id] === undefined) expLane[n.experiment_id] = freeLane++;
      lane[n.id] = expLane[n.experiment_id];
    } else {
      lane[n.id] = freeLane++;
    }
    freeLane = Math.max(freeLane, lane[n.id] + 1);
  }
  return lane;
}

function branchBestNodes(nodes) {
  // best goal-value per experiment (star markers)
  const best = {};
  for (const n of nodes) {
    if (n.goal_value == null || n.experiment_id == null) continue;
    const cur = best[n.experiment_id];
    if (!cur || Number(n.goal_value) > Number(cur.goal_value)) best[n.experiment_id] = n;
  }
  return best;
}

function syncBranchTimeControls(len) {
  const s = $("branch-time");
  const lab = $("branch-time-label");
  if (!s) return;
  len = Math.max(1, len);
  s.max = len;
  const val = branchTimeIdx == null ? len : Math.max(1, Math.min(len, branchTimeIdx));
  s.value = val;
  if (lab) lab.textContent = `${val}/${len} runs`;
}

function stopBranchPlay() {
  if (branchPlayInt) { clearInterval(branchPlayInt); branchPlayInt = 0; }
  const p = $("branch-play");
  if (p) p.textContent = "▶";
}

function renderBranchGraph() {
  const el = $("branch-graph");
  if (!el) return;
  const { nodes: allNodes, edges: allEdges, experiments } = state.branches || {};
  if (!allNodes || !allNodes.length) {
    syncBranchTimeControls(0);
    el.innerHTML = '<div class="empty">No runs yet. Ask Fox to run an experiment in chat — each run becomes a node.</div>';
    return;
  }
  // ensure the experiment filter is populated from the branches payload
  const expSel = $("branch-exp-filter");
  if (expSel && !expSel.options.length && (experiments || []).length) {
    expSel.innerHTML = `<option value="">all experiments</option>` +
      experiments.map((e) => `<option value="${e.id}">${esc(e.name)}</option>`).join("");
    const target = branchExpChoice !== null ? branchExpChoice
      : (state.activeExperiment != null ? String(state.activeExperiment) : "");
    expSel.value = [...expSel.options].some((o) => o.value === target) ? target : "";
  }
  // 1) filter by experiment + status, ordered chronologically (stable lanes)
  const eid = $("branch-exp-filter") ? $("branch-exp-filter").value : "";
  const filtered = allNodes.filter((n) =>
    (!eid || String(n.experiment_id) === String(eid)) &&
    (branchStatusFilter === "" ||
     (branchStatusFilter === "error" ? n.status === "error" : n.status !== "error")))
    .sort((a, b) => (a.started_at || 0) - (b.started_at || 0) || (a.id - b.id));
  syncBranchTimeControls(filtered.length);
  if (!filtered.length) {
    el.innerHTML = '<div class="empty">No runs match the current filters.</div>';
    return;
  }
  // 2) time slice from the evolution slider
  const reveal = branchTimeIdx == null ? filtered.length
    : Math.max(1, Math.min(filtered.length, branchTimeIdx));
  const visible = filtered.slice(0, reveal);
  // 3) stable lanes over the filtered set (scrubbing doesn't jitter)
  const fset = new Set(filtered.map((n) => n.id));
  const fedges = (allEdges || []).filter((e) => fset.has(e.parent) && fset.has(e.child));
  const lane = assignBranchLanes(filtered, fedges);
  const best = branchBestNodes(allNodes);
  const parentOf = {};
  const childrenOf = {};
  for (const e of allEdges || []) {
    parentOf[e.child] = e.parent;
    (childrenOf[e.parent] = childrenOf[e.parent] || []).push(e.child);
  }
  const rowH = 56, laneW = 150, padL = 14, padT = 26;
  const nLanes = Math.max(1, ...Object.values(lane)) + 1;
  const W = padL + nLanes * laneW + 220;
  const H = padT + filtered.length * rowH + 24;
  const cx = (l) => padL + l * laneW + laneW / 2;
  const cy = (i) => padT + i * rowH + rowH / 2;
  const pos = {};
  filtered.forEach((n, i) => { pos[n.id] = { x: cx(lane[n.id]), y: cy(i) }; });
  // 4) edges to draw
  const vset = new Set(visible.map((n) => n.id));
  let drawEdges;
  if (branchCompact) {
    // skeleton: each visible node connects to its nearest visible ancestor
    const up = [];
    for (const n of visible) {
      let p = parentOf[n.id];
      while (p !== undefined && !vset.has(p)) p = parentOf[p];
      if (p !== undefined) up.push({ parent: p, child: n.id });
    }
    drawEdges = up;
  } else {
    drawEdges = (allEdges || []).filter((e) => vset.has(e.parent) && vset.has(e.child));
  }
  const hasVisibleChild = new Set();
  for (const e of drawEdges) hasVisibleChild.add(e.parent);

  // 5) svg
  let svg = `<svg viewBox="0 0 ${W} ${H}">`;
  for (const e of drawEdges) {
    const p = pos[e.parent], c = pos[e.child];
    if (!p || !c) continue;
    const same = lane[e.parent] === lane[e.child];
    const d = same
      ? `M ${p.x} ${p.y + 9} L ${c.x} ${c.y - 9}`
      : `M ${p.x} ${p.y + 9} L ${p.x} ${(p.y + c.y) / 2} L ${c.x} ${(p.y + c.y) / 2} L ${c.x} ${c.y - 9}`;
    svg += `<path class="branch-edge" d="${d}"></path>`;
  }
  const showLabels = visible.length <= 40;
  visible.forEach((n) => {
    const x = pos[n.id].x, y = pos[n.id].y;
    const color = n.experiment_id != null ? expColor(n.experiment_id) : "#9b93ab";
    const sel = state.branchSelected === n.id ? " selected" : "";
    const isBest = best[n.experiment_id] && best[n.experiment_id].id === n.id;
    const isTip = !hasVisibleChild.has(n.id);
    const failed = n.status === "error";
    const children = (childrenOf[n.id] || []).length;
    const tipText = `run #${n.id} · ${n.label || ""}\n${n.experiment_name ? "experiment: " + n.experiment_name : ""}\n` +
      (n.goal_value != null ? `${n.goal_metric}: ${Number(n.goal_value).toFixed(4)}\n` : "") +
      (children ? `branches: ${children}\n` : "") +
      `config: ${JSON.stringify(n.config || {})}\n${new Date(n.started_at * 1000).toLocaleString()}`;
    svg += `<g class="branch-node${sel}" data-id="${esc(n.id)}" transform="translate(${x},${y})">`
      + `<title>${esc(tipText)}</title>`
      + (isTip ? `<circle r="15" fill="none" stroke="${color}" stroke-dasharray="3 3" opacity=".5"></circle>` : "")
      + `<circle r="9" fill="${color}" stroke-dasharray="${failed ? "3 2" : "0"}" stroke="#e06c6c"></circle>`
      + (isBest ? `<text y="-12" text-anchor="middle" font-size="11">★</text>` : "")
      + (showLabels ? `<text x="14" y="4" class="branch-label">#${esc(n.id)} ${esc((n.label || n.kind || "").slice(0, 26))}</text>` : "")
      + `</g>`;
  });
  // experiment lane headers for experiments that have visible nodes
  const expVisible = {};
  visible.forEach((n) => {
    if (n.experiment_id != null && expVisible[n.experiment_id] === undefined)
      expVisible[n.experiment_id] = n;
  });
  for (const eid2 in expVisible) {
    const n = expVisible[eid2];
    svg += `<text x="${pos[n.id].x}" y="${padT - 8}" text-anchor="middle" font-size="11" font-weight="700" fill="${expColor(eid2)}">${esc(n.experiment_name || "experiment")}</text>`;
  }
  svg += `</svg>`;

  // 6) legend + count
  const leg = $("branch-legend");
  if (leg) {
    const shownExps = [...new Set(visible.map((n) => n.experiment_id).filter((x) => x != null))];
    const legExps = (experiments || []).filter((e) => shownExps.includes(e.id));
    leg.innerHTML = legExps.map((e) =>
      `<span class="exp-legend-item"><span class="exp-legend-dot" style="background:${expColor(e.id)}"></span>${esc(e.name)}`
      + (e.goal_metric ? ` <span class="muted">(goal ${esc(e.goal_metric)}${e.goal_target != null ? " → " + e.goal_target : ""})</span>` : "")
      + ` · ${e.run_count} run(s)</span>`).join("")
      + `<span class="exp-legend-item muted">${visible.length}/${filtered.length} runs shown · ★ best · ⦿ branch tip · dashed = failed</span>`;
  }

  el.innerHTML = svg;
  el.querySelectorAll(".branch-node").forEach((nd) =>
    nd.addEventListener("click", () => {
      state.branchSelected = Number(nd.dataset.id);
      renderBranchGraph();
      showBranchDetail(state.branchSelected);
    }));
}

function showBranchDetail(id) {
  const el = $("branch-detail");
  if (!el) return;
  const { nodes, edges, experiments } = state.branches || {};
  const n = (nodes || []).find((x) => x.id === id);
  if (!n) { el.innerHTML = ""; return; }
  const exp = (experiments || []).find((e) => e.id === n.experiment_id);
  const children = (edges || []).filter((e) => e.parent === id).map((e) => e.child);
  const parent = (edges || []).find((e) => e.child === id);
  const parentNode = parent ? (nodes || []).find((x) => x.id === parent.parent) : null;
  const cfg = n.config || {};
  const metrics = n.metrics || {};
  const paramKeys = Object.keys(cfg);
  const strip = (s, len) => {
    if (!s) return "";
    const t = String(s).replace(/\s+/g, " ").trim();
    return t.length > len ? t.slice(0, len) + "…" : t;
  };
  let h = `<h4>Run #${n.id}</h4>`;
  h += bdRow("label", n.label || "—");
  h += bdRow("kind", n.kind || "agent_run");
  h += bdRow("status", n.status || "—");
  if (n.experiment_name) h += bdRow("experiment", n.experiment_name);
  h += bdRow("when", new Date((n.started_at || 0) * 1000).toLocaleString());
  if (parentNode) h += bdRow("parent", `#${parentNode.id} ${parentNode.label || ""}`);
  if (children.length) h += bdRow("branches", children.map((c) => `#${c}`).join(", "));
  h += bdRow("tools", n.tools + " · artifacts: " + n.artifacts);

  // Objectives: experiment hypothesis / plan / goal + this run's prompt.
  if (exp && (exp.hypothesis || exp.plan)) {
    h += `<h4>Objectives</h4>`;
    if (exp.hypothesis) h += `<div class="bd-note">${esc(strip(exp.hypothesis, 500))}</div>`;
    if (exp.plan) h += `<div class="bd-note">${esc(strip(exp.plan, 600))}</div>`;
  }
  if (exp && exp.goal_metric) {
    h += bdRow("goal", `${exp.goal_metric} ${exp.higher_better ? "↑" : "↓"}${exp.goal_target != null ? " → " + exp.goal_target : ""}${n.goal_value != null ? " · current " + fmtVal(n.goal_value) : ""}`);
  }
  if (n.objective) h += bdRow("run objective", strip(n.objective, 400));

  // Parameters: this run's config.
  h += `<h4>Experiment parameters</h4>`;
  h += paramKeys.length
    ? `<div>${paramKeys.map((k) => `<span class="bd-param">${esc(k)}: ${esc(fmtVal(cfg[k]))}</span>`).join(" ")}</div>`
    : `<div class="muted">(none recorded)</div>`;

  // Actions: diff against the parent, revert, or restore from git.
  if (parentNode || true) {
    h += `<div class="bd-actions">` +
      (parentNode ? `<button class="btn subtle small bd-diff" data-rid="${n.id}">⇄ diff vs parent</button>` : "") +
      `<button class="btn subtle small bd-revert" data-rid="${n.id}" title="Revert: rerun this run's prompt as a fresh turn">↶ revert</button>` +
      `<button class="btn subtle small bd-restore" data-rid="${n.id}" title="Restore this run's artifacts from its git commit">↩ restore</button>` +
      `</div><div class="bd-diff-host"></div>`;
  }

  // Provenance: snapshot commit + run-time environment.
  if (n.git_commit || Object.keys(n.env || {}).length) {
    h += `<h4>Reproducibility</h4>`;
    if (n.git_commit) {
      h += `<div class="bd-row"><span class="bd-k">commit</span><span class="bd-v">${esc(n.git_commit)}</span></div>`;
    }
    const envKeys = Object.keys(n.env || {});
    if (envKeys.length) {
      h += envKeys.map((k) => `<div class="bd-row"><span class="bd-k">${esc(k)}</span><span class="bd-v">${esc(fmtVal(n.env[k]))}</span></div>`).join("");
    }
  }

  // Round-8: verifiability — content hash + audit trail.
  h += `<div class="bd-actions">
      <button class="btn subtle small bd-verify" data-rid="${n.id}">🔒 Verify integrity</button>
      <button class="btn subtle small bd-audit" data-rid="${n.id}">🛡 Audit trail</button>
    </div><div class="bd-audit-host"></div>`;

  // Metrics.
  if (Object.keys(metrics).length) {
    h += `<h4>Metrics</h4>`;
    h += Object.entries(metrics).map(([k, v]) => {
      const isGoal = n.goal_metric && k === n.goal_metric;
      return `<div class="bd-row"><span class="bd-k">${esc(k)}</span><span class="bd-v ${isGoal ? "branch-metric-goal" : ""}">${esc(fmtVal(v))}${isGoal ? " ★" : ""}</span></div>`;
    }).join("");
  }

  // Summary: the run's outcome text.
  if (n.summary) {
    h += `<h4>Summary</h4><div class="bd-note">${esc(strip(n.summary, 700))}</div>`;
  }

  // Findings.
  if ((n.findings || []).length) {
    h += `<h4>Findings</h4>`;
    h += `<ul class="bd-list">${n.findings.map((f) => `<li>${esc(String(f))}</li>`).join("")}</ul>`;
  } else {
    h += `<h4>Findings</h4><div class="muted">none flagged</div>`;
  }

  // Notes: reviewer suggestions applied for this run.
  if ((n.notes || []).length) {
    h += `<h4>Notes</h4>`;
    h += `<ul class="bd-list">${n.notes.map((s) => `<li>${esc(strip(s, 200))}</li>`).join("")}</ul>`;
  }

  el.innerHTML = h;
  const dbtn = el.querySelector(".bd-diff");
  if (dbtn) dbtn.addEventListener("click", async () => {
    const host = el.querySelector(".bd-diff-host");
    if (host.dataset.loaded) return;
    host.dataset.loaded = "1";
    host.innerHTML = '<div class="muted">Loading diff…</div>';
    await renderRunDiff(host, Number(dbtn.dataset.rid));
  });
  const rbtn = el.querySelector(".bd-revert");
  if (rbtn) rbtn.addEventListener("click", () =>
    sendChat("", "rerun_run", { run_id: rbtn.dataset.rid }));
  const rsbtn = el.querySelector(".bd-restore");
  if (rsbtn) rsbtn.addEventListener("click", async () => {
    const host = el.querySelector(".bd-diff-host");
    host.innerHTML = '<div class="muted">Restoring from git…</div>';
    try {
      const r = await api(`/api/projects/${state.project}/runs/${rsbtn.dataset.rid}/restore`, {
        method: "POST",
      });
      host.innerHTML = r.restored && r.restored.length
        ? `<div class="muted">Restored ${r.restored.length} artifact(s) from commit ${esc(r.commit || "")} → new run #${r.run_id}.</div>`
        : `<div class="muted">Run state restored from commit ${esc(r.commit || "")} → new run #${r.run_id}.</div>`;
      loadBranches();
    } catch (e) { host.innerHTML = '<div class="muted">Restore failed: ' + esc(e.message) + "</div>"; }
  });
  const vbtn = el.querySelector(".bd-verify");
  if (vbtn) vbtn.addEventListener("click", async () => {
    const host = el.querySelector(".bd-audit-host");
    host.innerHTML = '<div class="muted">Verifying…</div>';
    try {
      const r = await api(`/api/projects/${state.project}/runs/${vbtn.dataset.rid}/verify`);
      host.innerHTML = r.ok === true
        ? `<div class="bd-diff-sec"><b>Integrity</b><div class="bd-diff-add">✓ verified — ${esc(r.hash.slice(0, 12))}</div></div>`
        : (r.ok === null
            ? `<div class="bd-diff-sec"><b>Integrity</b><div class="muted">${esc(r.message || "no hash recorded")}</div></div>`
            : `<div class="bd-diff-sec"><b>Integrity</b><div class="bd-diff-del">✗ MISMATCH — record was altered</div></div>`);
    } catch (e) { host.innerHTML = '<div class="muted">Verify failed: ' + esc(e.message) + "</div>"; }
  });
  const abtn = el.querySelector(".bd-audit");
  if (abtn) abtn.addEventListener("click", async () => {
    const host = el.querySelector(".bd-audit-host");
    host.innerHTML = '<div class="muted">Loading audit trail…</div>';
    try {
      const r = await api(`/api/projects/${state.project}/runs/${abtn.dataset.rid}/audit`);
      const evs = r.events || [];
      const devs = r.deviations || [];
      let h = `<div class="bd-diff-sec"><b>Audit trail</b>` +
        (r.chain_verified ? `<span class="sug-badge ok">chain ✓</span>` : `<span class="sug-badge warn">chain —</span>`) +
        `</div>`;
      if (!evs.length && !devs.length) { h += '<div class="muted">No audit events for this run (recorded before round 8?).</div>'; }
      if (devs.length) {
        h += `<div class="bd-diff-sec"><b>Deviations</b>`;
        for (const d of devs.slice(0, 10)) {
          h += `<div class="bd-diff-fail">⚠ ${esc(String(d.rule || d.severity || "deviation"))} — ${esc(String(d.explanation || "").slice(0, 200))}</div>`;
        }
        h += `</div>`;
      }
      h += `<table class="bd-diff-table"><tr><th>when</th><th>tool</th><th>sev</th><th>duration</th></tr>`;
      for (const e of evs.slice(0, 30)) {
        const sev = (e.severity || "info").slice(0, 7);
        const cls = e.network ? "bd-diff-chg" : (e.filesystem ? "bd-diff-add" : "");
        h += `<tr class="${cls}"><td>${esc(String(e.timestamp || "").slice(0, 19))}</td>` +
          `<td>${esc(e.tool_name || e.method || "")}</td><td>${esc(sev)}</td>` +
          `<td>${e.duration_ms != null ? Math.round(e.duration_ms) + "ms" : "—"}</td></tr>`;
      }
      h += `</table>`;
      host.innerHTML = h;
    } catch (e) { host.innerHTML = '<div class="muted">Audit failed: ' + esc(e.message) + "</div>"; }
  });
}

async function renderRunDiff(host, runId) {
  try {
    const r = await api(`/api/projects/${state.project}/runs/${runId}/diff`);
    let h = "";
    if (!r.b) { host.innerHTML = '<div class="muted">No parent run to diff against.</div>'; return; }
    const chg = r.config.added.length || r.config.removed.length || r.config.changed.length;
    if (chg) {
      h += `<div class="bd-diff-sec"><b>Config</b>`;
      for (const k of r.config.added) h += `<div class="bd-diff-add">+ ${esc(k)}</div>`;
      for (const k of r.config.removed) h += `<div class="bd-diff-del">− ${esc(k)}</div>`;
      for (const [k, va, vb] of r.config.changed) h += `<div class="bd-diff-chg">~ ${esc(k)}: ${esc(fmtVal(va))} → ${esc(fmtVal(vb))}</div>`;
      h += `</div>`;
    }
    const t = r.tools || {};
    if (t.added.length || t.removed.length || t.failed.length) {
      h += `<div class="bd-diff-sec"><b>Tools</b>`;
      for (const k of t.added) h += `<div class="bd-diff-add">+ ${esc(k)}</div>`;
      for (const k of t.removed) h += `<div class="bd-diff-del">− ${esc(k)}</div>`;
      for (const k of t.failed) h += `<div class="bd-diff-fail">✗ ${esc(k)} failed</div>`;
      h += `</div>`;
    }
    const mrows = ((r.metrics || {}).rows) || [];
    if (mrows.length) {
      h += `<div class="bd-diff-sec"><b>Metrics</b><table class="bd-diff-table"><tr><th>metric</th><th>parent</th><th>this</th><th>Δ</th></tr>`;
      for (const row of mrows) {
        const cls = row.delta > 0 ? "add" : (row.delta < 0 ? "del" : "");
        h += `<tr><td>${esc(row.metric)}</td><td>${esc(fmtVal(row.a))}</td><td>${esc(fmtVal(row.b))}</td><td class="bd-diff-${cls}">${row.delta >= 0 ? "+" : ""}${esc(fmtVal(row.delta))}</td></tr>`;
      }
      h += `</table></div>`;
    }
    if (r.prompt && r.prompt.b) {
      h += `<div class="bd-diff-sec"><b>Objective changed</b><div class="bd-note">${esc(strip(r.prompt.b, 300))}</div></div>`;
    }
    const cds = (r.code || {}).diffs || [];
    if (cds.length) {
      h += `<div class="bd-diff-sec"><b>Code</b>`;
      for (const cd of cds) {
        h += `<div class="bd-diff-code">
          <div class="bd-diff-code-head">${esc(cd.tool)} <span class="muted">+${cd.added} −${cd.removed}</span></div>
          <pre>${esc(cd.patch)}</pre></div>`;
      }
      h += `</div>`;
    }
    host.innerHTML = h || '<div class="muted">No differences detected.</div>';
  } catch (e) { host.innerHTML = '<div class="muted">Diff failed: ' + esc(e.message) + "</div>"; }
}

function bdRow(k, v) {
  return `<div class="bd-row"><span class="bd-k">${esc(k)}</span><span class="bd-v">${esc(String(v == null ? "—" : v))}</span></div>`;
}

function fmtVal(v) {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Math.round(v * 1e4) / 1e4);
  return String(v);
}

$("branch-toggle").addEventListener("click", toggleBranches);
const quickBranches = $("quick-branches");
if (quickBranches) quickBranches.addEventListener("click", toggleBranches);
$("branch-close").addEventListener("click", () => {
  $("branch-overlay").classList.add("hidden");
  $("branch-toggle").classList.remove("active");
});
const branchChat = $("branch-chat");
if (branchChat) branchChat.addEventListener("click", () => switchMainView("chat"));
$("branch-refresh").addEventListener("click", loadBranches);

/* ---------- faded dgxtop-style server resource HUD ---------- */
let dgtopTimer = null;
const dgtopPollMs = 4000;

function dgtopToggle(show) {
  const panel = $("dgtop");
  const fab = $("dgtop-toggle");
  if (!panel || !fab) return;
  const on = show != null ? show : panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !on);
  fab.classList.toggle("active", on);
  try { localStorage.setItem("fox.dgtop", on ? "1" : "0"); } catch (e) { /* ignore */ }
  if (on) {
    dgtopLoad();
    if (!dgtopTimer) dgtopTimer = setInterval(dgtopLoad, dgtopPollMs);
  } else {
    clearInterval(dgtopTimer);
    dgtopTimer = null;
  }
}

async function dgtopLoad() {
  const body = $("dgtop-body");
  if (!body) return;
  try {
    const s = await api("/api/system/stats");
    const age = $("dgtop-age");
    if (age) age.textContent = new Date().toLocaleTimeString();
    renderDgtop(body, s);
  } catch (e) {
    body.innerHTML = `<div class="empty">Server stats unavailable: ${esc(e.message || e)}</div>`;
  }
}

function dgtopBar(pct) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<span class="bar-host"><span class="bar-fill" style="width:${p}%"></span></span>`;
}

function renderDgtop(el, s) {
  const host = s.host || {};
  const hn = $("dgtop-host");
  if (hn) hn.textContent = host.hostname || "—";
  const cpu = host.cpu || {};
  const mem = host.memory || {};
  const gpu = s.gpu || {};

  let h = `<div class="dgtop-sec">host · cpu</div>`;
  h += `<table><tbody>`;
  h += `<tr><th>cpu</th><td class="right">${fmtVal(cpu.usage_percent)}% ${dgtopBar(cpu.usage_percent)}</td></tr>`;
  if (cpu.per_core && Object.keys(cpu.per_core).length) {
    const coreCells = Object.keys(cpu.per_core).sort((a, b) => +a - +b)
      .map((c) => `<span title="core ${c}">${fmtVal(cpu.per_core[c])}%</span>`).join(" · ");
    h += `<tr><th>cores</th><td>${coreCells}</td></tr>`;
  }
  h += `<tr><th>load</th><td>${(host.loadavg || []).map(fmtVal).join("  ")}</td></tr>`;
  const memPct = mem.total_mb ? (mem.used_mb / mem.total_mb * 100) : 0;
  h += `<tr><th>mem</th><td class="right">${fmtVal(mem.used_mb)} / ${fmtVal(mem.total_mb)} MB (${fmtVal(memPct)}%) ${dgtopBar(memPct)}</td></tr>`;
  h += `</tbody></table>`;

  h += `<div class="dgtop-sec">gpu</div>`;
  if (gpu.available && gpu.devices.length) {
    h += `<table><tbody>`;
    for (const d of gpu.devices) {
      const u = d.utilization_percent || 0;
      const memP = d.memory_total_mb ? (d.memory_used_mb / d.memory_total_mb * 100) : 0;
      h += `<tr><th>${esc(d.index)}: ${esc(d.name)}</th>
        <td class="right">${u >= 95 ? '<span class="gpu-hot">' : ""}${fmtVal(u)}%${u >= 95 ? "</span>" : ""} ${dgtopBar(u)}</td>
        <td class="right">${fmtVal(d.temperature_c)}°C</td>
        <td class="right">${fmtVal(d.power_watts)}W</td>
        <td class="right">${fmtVal(d.memory_used_mb)}/${fmtVal(d.memory_total_mb)} MB ${dgtopBar(memP)}</td></tr>`;
    }
    h += `</tbody></table>`;
    if (gpu.processes.length) {
      h += `<table><tbody>` + gpu.processes.map((p) =>
        `<tr class="proc-row"><td>${esc(p.pid)}</td><td>${esc(p.name)}</td><td class="right">${fmtVal(p.gpu_memory_mb)} MB</td></tr>`).join("") + `</tbody></table>`;
    }
  } else {
    h += `<div class="dgtop-muted">no NVIDIA GPU available</div>`;
  }

  const procs = s.processes || [];
  h += `<div class="dgtop-sec">processes · top ${procs.length}</div>`;
  if (!procs.length) {
    h += `<div class="dgtop-muted">none</div>`;
  } else {
    h += `<table><tbody><tr><th>pid</th><th>user</th><th class="right">cpu%</th><th class="right">mem</th><th>command</th></tr>`;
    for (const p of procs) {
      h += `<tr class="proc-row"><td>${esc(p.pid)}</td><td>${esc(p.user)}</td>
        <td class="right">${fmtVal(p.cpu_percent)}</td><td class="right">${fmtVal(p.mem_mb)} MB</td>
        <td title="${esc(p.command)}">${esc((p.command || "").slice(0, 60))}</td></tr>`;
    }
    h += `</tbody></table>`;
  }
  el.innerHTML = h;
}

const dgtopFab = $("dgtop-toggle");
if (dgtopFab) dgtopFab.addEventListener("click", () => dgtopToggle());
const dgtopClose = $("dgtop-close");
if (dgtopClose) dgtopClose.addEventListener("click", () => dgtopToggle(false));
try {
  if (localStorage.getItem("fox.dgtop") === "1") dgtopToggle(true);
} catch (e) { /* ignore */ }

// Resizable split: drag the divider to widen the description/summary pane.
(function initBranchResizer() {
  const resizer = $("branch-resizer");
  const detail = $("branch-detail");
  if (!resizer || !detail) return;
  const main = resizer.parentElement;
  try {
    const saved = parseInt(localStorage.getItem("fox-branch-detail-w"), 10);
    if (saved >= 220) detail.style.width = saved + "px";
  } catch (e) { /* ignore */ }
  let dragging = false;
  resizer.addEventListener("mousedown", (e) => {
    dragging = true;
    resizer.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = main.getBoundingClientRect();
    const w = Math.max(220, Math.min(rect.right - e.clientX, rect.width * 0.7));
    detail.style.width = w + "px";
  });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    try {
      const w = parseInt(detail.style.width, 10);
      if (w >= 220) localStorage.setItem("fox-branch-detail-w", String(w));
    } catch (e) { /* ignore */ }
  });
})();

$("mainview-chat").addEventListener("click", () => switchMainView("chat"));
$("mainview-experiments").addEventListener("click", () => switchMainView("experiments"));
$("mainview-agent").addEventListener("click", () => switchMainView("agent"));
$("mainview-editor").addEventListener("click", () => switchMainView("editor"));
$("mainview-rkg").addEventListener("click", () => switchMainView("rkg"));
$("mainview-audit").addEventListener("click", () => switchMainView("audit"));
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
  fetchKernelStatus();
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
  loadCampaigns();
  loadEvals();
  connect();
  setupExpKeyboard();
  setTimeout(expDeepLink, 350);
})();
