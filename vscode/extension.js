"use strict";

// Fox — Experiment Tracking (VS Code extension host)
// Talks to the Fox workbench REST API (no backend changes) and relays results
// to the webview dashboard. Also generates markdown documentation files
// (report / next research / summary of findings) opened in the editor.

const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const os = require("os");
const crypto = require("crypto");

/** @type {TrackingPanel | undefined} */
let activePanel = undefined;

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("fox.openTracking", () => TrackingPanel.createOrShow(context.extensionUri)),
    vscode.commands.registerCommand("fox.refresh", () => { if (activePanel) activePanel.refresh(); }),
    vscode.commands.registerCommand("fox.generateReport", () => docCommand(context, "Report", "report")),
    vscode.commands.registerCommand("fox.nextResearch", () => docCommand(context, "Next research", "next")),
    vscode.commands.registerCommand("fox.summary", () => docCommand(context, "Summary of findings", "summary"))
  );
}

function deactivate() {}

// ---------------------------------------------------------------- config / api

function baseUrl() {
  const cfg = vscode.workspace.getConfiguration("fox");
  return String(cfg.get("baseUrl") || "http://localhost:8765").replace(/\/+$/, "");
}

function projectName() {
  return String(vscode.workspace.getConfiguration("fox").get("project") || "").trim();
}

async function firstProject() {
  const data = await api("/api/projects");
  const list = (data && data.projects) || [];
  return list.length ? list[0].name : "";
}

async function api(path, method, body) {
  const res = await fetch(baseUrl() + path, {
    method: method || "GET",
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || data || "") || ("HTTP " + res.status));
  }
  return data;
}

function proj(name, rest) {
  return "/api/projects/" + encodeURIComponent(name) + (rest || "");
}

// ----------------------------------------------------------------- markdown docs

async function writeMarkdown(slug, title, content) {
  const folders = vscode.workspace.workspaceFolders;
  let dir;
  if (folders && folders.length) {
    dir = path.join(folders[0].uri.fsPath, "reports");
  } else {
    dir = path.join(os.tmpdir(), "fox-reports");
  }
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, slug + "-" + crypto.randomBytes(3).toString("hex") + ".md");
  fs.writeFileSync(file, "# " + title + "\n\n" + content + "\n");
  return vscode.Uri.file(file);
}

async function buildSummary(name) {
  let report = "";
  try { report = ((await api(proj(name, "/report?summary=false"))).report) || ""; } catch (_) {}
  const lines = ["## Executive summary", ""];
  const m = report.match(/## Executive summary\s+([\s\S]*?)\s+(?=##|$)/);
  lines.push(m ? m[1].trim() : report.split("\n").slice(0, 8).join("\n").trim());
  lines.push("", "---", "", report.replace(/^## Executive summary[\s\S]*?(?=## Experiments|$)/, "").trim());
  return lines.join("\n");
}

async function docCommand(_ctx, title, kind) {
  try {
    const name = projectName() || (await firstProject());
    if (!name) { vscode.window.showErrorMessage("No Fox project found. Start the workbench and set fox.project."); return; }
    let content = "";
    if (kind === "report") {
      const r = await api(proj(name, "/report?summary=true"));
      content = r.report || "(empty report)";
    } else if (kind === "next") {
      const r = await api(proj(name, "/next"));
      content = r.agenda || "(no agenda)";
    } else if (kind === "summary") {
      content = await buildSummary(name);
    }
    const uri = await writeMarkdown(kind, title, content);
    await vscode.commands.executeCommand("markdown.showPreviewToSide", uri);
    await vscode.window.showTextDocument(uri, { preview: false });
  } catch (e) {
    vscode.window.showErrorMessage("Fox: " + (e && e.message ? e.message : e));
  }
}

// --------------------------------------------------------------- webview panel

class TrackingPanel {
  constructor(extensionUri, panel) {
    this._panel = panel;
    this._extensionUri = extensionUri;
    this._disposables = [];
    this._panel.onDidDispose(() => this.dispose(), null, this._disposables);
    this._panel.webview.onDidReceiveMessage(async (msg) => {
      try {
        await this._handle(msg);
      } catch (e) {
        this._panel.webview.postMessage({ kind: "error", message: e && e.message ? e.message : String(e) });
      }
    }, null, this._disposables);
    this._render();
  }

  static createOrShow(extensionUri) {
    const col = vscode.window.activeTextEditor && vscode.window.activeTextEditor.viewColumn;
    if (activePanel) { activePanel._panel.reveal(col); return; }
    const panel = vscode.window.createWebviewPanel(
      "foxTracking", "Fox · Experiment Tracking",
      col || vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [vscode.Uri.joinPath(extensionUri, "media")] }
    );
    activePanel = new TrackingPanel(extensionUri, panel);
  }

  refresh() {
    if (this._panel) this._panel.webview.postMessage({ kind: "refresh" });
  }

  dispose() {
    activePanel = undefined;
    this._panel.dispose();
    while (this._disposables.length) this._disposables.pop().dispose();
  }

  _render() {
    const webview = this._panel.webview;
    const htmlPath = vscode.Uri.joinPath(this._extensionUri, "media", "tracking.html");
    let html = fs.readFileSync(htmlPath.fsPath, "utf8");
    const js = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, "media", "tracking.js"));
    const css = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, "media", "tracking.css"));
    html = html.replace("${jsUri}", js.toString()).replace("${cssUri}", css.toString());
    this._panel.webview.html = html;
  }

  async _handle(msg) {
    if (msg.kind === "api") {
      const path = msg.path || "";
      const data = await api(path, msg.method || "GET", msg.body);
      this._panel.webview.postMessage({ kind: "apiResult", id: msg.id, path, data });
      return;
    }
    if (msg.kind === "setProject") {
      await vscode.workspace.getConfiguration("fox").update("project", msg.name, vscode.ConfigurationTarget.Global);
      vscode.window.showInformationMessage("Fox project → " + msg.name);
      return;
    }
    if (msg.kind === "doc") {
      await docCommand(null, msg.title || "Fox document", msg.kind === "report" ? "report" : msg.kind === "next" ? "next" : "summary");
      this._panel.webview.postMessage({ kind: "toast", message: "Document opened in the editor." });
      return;
    }
    if (msg.kind === "toast") {
      vscode.window.showInformationMessage(msg.message || "");
      return;
    }
  }
}

module.exports = { activate, deactivate };
