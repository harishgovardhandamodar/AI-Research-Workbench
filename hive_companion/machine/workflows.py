"""User-defined workflows — end-to-end fulfillment, local-first.

3-tier per workflow (every workflow has):
  local-only      — no copies, no commits, no websearch, no web LLM, sensitive datasets, private reports, full logs (never committed, .gitignore *)
  core-workflow   — web allowed, commits allowed, logs except private datapoints (redacted)
  public          — all can be committed, public

Folder structure for every workflow <name>:
  ~/.hive/machine/workflows/<name>.yaml          # definition
  ~/.hive/machine/workflows/<name>/             # 3-tier definition outputs
    local-only/datasets, reports, logs (+ .gitignore *)
    core-workflow/reports, artifacts, logs, README
    public/reports, artifacts, logs, README
  ~/.hive/machine/workspace/<name>/             # 3-tier execution workspace (jailed)
    local-only/datasets, reports, artifacts, logs
    core-workflow/...
    public/...

Execution is fully audited (audit.log_event) with severity/impact, access tier, and hash chain.
Supports Nvidia-PAIR routing per llm step.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

from hive.config import load_config, AppConfig
from hive.llm import ChatMessage, chat, get_provider
from hive.machine import WORKFLOWS_DIR, MACHINE_DIR, workflow_dirs, workflow_base
from hive.machine.access import check_access, ensure_workflow_dirs, TIERS
from hive.machine.tools import dispatch_tool
from hive.machine.audit import log_event
from hive.machine.nvidia_pair import discover_all, pick_best, get_provider_for
from hive.machine.sensitive import redact

VAR_RE = re.compile(r"\$\{([^}]+)\}")

def _load_workflow(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("pyyaml not installed: pip install pyyaml")
        return yaml.safe_load(text)
    return json.loads(text)

def _resolve_vars(template: str, context: Dict[str, str]) -> str:
    def repl(m):
        key = m.group(1).strip()
        if key.startswith("steps."):
            key = key[6:]
        if ".outputs" in key:
            key = key.split(".")[0]
        return context.get(key, m.group(0))
    return VAR_RE.sub(repl, template)

def _resolve_args(args: dict, context: Dict[str, str]) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[k] = _resolve_vars(v, context)
        else:
            out[k] = v
    return out

def _tier_path(workflow: str, access: str, path: str) -> str:
    """Resolve path relative to workflow's tier folder. Returns string relative to WORKSPACE/<workflow>/<tier>.jail will handle."""
    # If path already contains tier prefix, keep as is
    if any(path.startswith(f"{t}/") for t in TIERS):
        return path
    # Default: map write_file/read_file to reports, others to tier root
    # For simplicity, put all files under <tier>/reports or <tier>/artifacts/logs as needed
    # Heuristic: .md -> reports, .log/.json -> logs, else artifacts
    tier = access or "core-workflow"
    if path.endswith(".md"):
        return f"{workflow}/{tier}/reports/{path.lstrip('/')}"
    if path.endswith((".log", ".json", ".jsonl")):
        return f"{workflow}/{tier}/logs/{path.lstrip('/')}"
    # datasets only for local-only
    if tier == "local-only" and ("dataset" in path.lower() or path.startswith("datasets/")):
        return f"{workflow}/{tier}/datasets/{Path(path).name}"
    return f"{workflow}/{tier}/artifacts/{path.lstrip('/')}"

def list_workflows() -> List[Path]:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(WORKFLOWS_DIR.glob("*.yaml")) + sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.json"))

def save_workflow(name: str, data: dict):
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKFLOWS_DIR / f"{name}.yaml"
    # ensure 3-tier structure for definition and workspace
    ensure_workflow_dirs(WORKFLOWS_DIR / name)
    ensure_workflow_dirs(workflow_base(name))
    if yaml:
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path

def run_workflow(
    workflow: str | Path,
    cfg: AppConfig | None = None,
    extra_vars: Dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run workflow file or dict. Returns {id: output} context + audit."""
    cfg = cfg or load_config()
    # resolve workflow source
    wf = None
    wf_id = "workflow"
    if isinstance(workflow, dict):
        wf = workflow
        wf_id = wf.get("name", "workflow")
    elif isinstance(workflow, (str, Path)):
        p = Path(workflow)
        if p.exists() and p.is_file():
            wf = _load_workflow(p)
            wf_id = p.stem
        else:
            name = str(workflow)
            for ext in (".yaml", ".yml", ".json"):
                cand = WORKFLOWS_DIR / f"{name}{ext}"
                if cand.exists():
                    wf = _load_workflow(cand)
                    wf_id = name
                    break
            if wf is None:
                cand2 = WORKFLOWS_DIR / name
                if cand2.exists() and cand2.is_file():
                    wf = _load_workflow(cand2)
                    wf_id = cand2.stem
            if wf is None:
                raise FileNotFoundError(f"workflow not found: {workflow} (looked in {WORKFLOWS_DIR})")
    if wf is None:
        raise FileNotFoundError(str(workflow))

    # ensure 3-tier folders for this workflow (definition + workspace)
    ensure_workflow_dirs(WORKFLOWS_DIR / wf_id)
    dirs = ensure_workflow_dirs(workflow_base(wf_id))
    # workflow default access (top-level)
    wf_access = wf.get("access", "core-workflow")
    if wf_access not in TIERS:
        wf_access = "core-workflow"

    steps = wf.get("steps", [])
    context: Dict[str, str] = dict(extra_vars or {})
    outputs: Dict[str, str] = {}
    audit_events = []
    start = time.time()

    for step in steps:
        sid = step.get("id", f"step{len(outputs)+1}")
        tool = step.get("tool")
        llm_cfg = step.get("llm")
        args = step.get("args", {})
        step_access = step.get("access", wf_access)
        if step_access not in TIERS:
            step_access = wf_access

        # resolve path for file tools to tiered location
        if tool in ("write_file", "read_file", "list_files", "delete_path") and "path" in args:
            # only rewrite if path is not already tiered and not absolute
            orig_path = args["path"]
            if not any(orig_path.startswith(f"{t}/") for t in TIERS) and not str(orig_path).startswith("/"):
                # map to tiered workspace path
                args = dict(args)
                args["path"] = _tier_path(wf_id, step_access, str(orig_path))

        args = _resolve_args(args, {**context, **outputs})

        # enforce access control before any tool/llm
        if tool:
            check_access(tool, step_access, args)
        if llm_cfg is not None:
            # check llm web allowance
            check_access("llm_chat", step_access, {"prompt": llm_cfg.get("prompt", "") + str(args)})

        if llm_cfg is not None:
            prompt = llm_cfg.get("prompt") or args.get("prompt") or step.get("prompt") or ""
            prompt = _resolve_vars(prompt, {**context, **outputs})
            provider_name = llm_cfg.get("provider", "auto")
            model_name = llm_cfg.get("model", "")
            if provider_name == "auto":
                best = pick_best(cfg, prefer="fastest")
                if best:
                    provider_name = best.provider
                    model_name = best.model
                else:
                    provider_name = cfg.llm.provider
            orig_provider = cfg.llm.provider
            orig_ollama = cfg.llm.ollama_model
            orig_lmstudio = cfg.llm.lmstudio_model
            try:
                if provider_name == "nvidia":
                    from hive.llm.nvidia import NvidiaProvider
                    prov = NvidiaProvider(model=model_name or "meta/llama3-8b-instruct")
                    resp = prov.chat([ChatMessage(role="user", content=prompt)], temperature=cfg.llm.temperature)
                    out = resp.content
                    outputs[sid] = out
                    context[sid] = out
                    ev = log_event(tool="llm_chat", args={"provider": provider_name, "model": model_name, "prompt": prompt[:500], "access": step_access}, result=out, bytes_out=len(prompt), bytes_in=len(out), workflow_id=wf_id, model=model_name)
                    audit_events.append(ev)
                    continue
                if provider_name in ("ollama", "auto"):
                    cfg.llm.provider = "ollama"
                    if model_name:
                        cfg.llm.ollama_model = model_name
                elif provider_name == "lmstudio":
                    cfg.llm.provider = "lmstudio"
                    if model_name:
                        cfg.llm.lmstudio_model = model_name
                resp = chat(cfg, [ChatMessage(role="user", content=prompt)])
                out = resp.content
                outputs[sid] = out
                context[sid] = out
                log_event(tool="llm_chat", args={"provider": provider_name, "model": model_name, "prompt": prompt[:500], "access": step_access}, result=out, bytes_out=len(prompt), bytes_in=len(out), workflow_id=wf_id, model=model_name)
            finally:
                cfg.llm.provider = orig_provider
                cfg.llm.ollama_model = orig_ollama
                cfg.llm.lmstudio_model = orig_lmstudio

        if tool:
            if dry_run:
                outputs[sid] = f"[dry-run:{step_access}] would call {tool} {args}"
                continue
            result = dispatch_tool(tool, **args)
            # redact for core-workflow/public logs if sensitive
            if step_access != "local-only":
                # log redacted version for non-local tiers
                from hive.machine.sensitive import redact
                log_preview = redact(result[:2000])
            else:
                log_preview = result[:2000]
            log_event(tool=tool, args={**args, "access": step_access}, result=log_preview, bytes_out=len(str(args)), bytes_in=len(result), workflow_id=wf_id)
            outputs[sid] = result
            context[sid] = result

    elapsed = time.time() - start
    return {"workflow": wf_id, "outputs": outputs, "context": context, "elapsed": elapsed, "audit": audit_events, "dirs": {k: str(v) for k, v in dirs.items()}, "access": wf_access}

EXAMPLE_AI_AGENTS = {
    "name": "ai_agents_report",
    "description": "AI Agents research — local-first, audited (core-workflow: web allowed)",
    "access": "core-workflow",
    "steps": [
        {"id": "search", "tool": "web_search", "args": {"query": "AI Agents", "top_k": 5}, "access": "core-workflow"},
        {"id": "summarize", "llm": {"provider": "auto", "prompt": "Summarize these papers on AI Agents into 5 bullet findings, methods, and references:\n${search}\nBe concise, cite DOI/URL."}, "access": "core-workflow"},
        {"id": "write", "tool": "write_file", "args": {"path": "ai_agents_report.md", "content": "# AI Agents — Research Report\n\n${summarize}\n\n---\n*Generated local-first via ${search} — audited*"}, "access": "core-workflow"},
        {"id": "verify", "tool": "read_file", "args": {"path": "ai_agents_report.md"}, "access": "core-workflow"},
    ],
}

EXAMPLE_LOCAL_ONLY = {
    "name": "local_only_example",
    "description": "Local-only: sensitive datasets, no web, no commits",
    "access": "local-only",
    "steps": [
        {"id": "ingest", "tool": "write_file", "args": {"path": "datasets/private.csv", "content": "id,secret\n1,my_secret_data"}, "access": "local-only"},
        {"id": "analyze", "tool": "run_python", "args": {"code": "import pathlib; p=pathlib.Path('~/.hive/machine/workspace/local_only_example/local-only/datasets/private.csv').expanduser(); print(p.read_text()[:100])"}, "access": "local-only"},
        {"id": "report", "tool": "write_file", "args": {"path": "reports/private_report.md", "content": "# Private Report\n\n${analyze}"}, "access": "local-only"},
    ],
}
DIABETES_MANAGEMENT = {
    "name": "diabetes_management",
    "description": "Diabetes-Management-system — Medtronic CareLink SG timeseries + local LLM report (local-only: sensitive health data, no web, no commits) — mg/dL ↔ mmol/L switch — 24h per day trend + frequency bands",
    "access": "local-only",
    "steps": [
        {
            "id": "check_dataset",
            "tool": "list_files",
            "args": {"path": "diabetes_management/local-only/datasets", "recursive": True},
            "access": "local-only"
        },
        {
            "id": "analyze_timeseries",
            "tool": "run_python",
            "args": {
                "code": """import pathlib, csv, json, statistics, collections, datetime
import sys
from pathlib import Path
MGDL_TO_MMOL = 18.01559
def mgdl_to_mmol(v): return round(v / MGDL_TO_MMOL, 1)

ws = Path.home() / '.hive' / 'machine' / 'workspace' / 'diabetes_management' / 'local-only'
data_dir = ws / 'datasets'
reports_dir = ws / 'reports'
logs_dir = ws / 'logs'
reports_dir.mkdir(parents=True, exist_ok=True)
logs_dir.mkdir(parents=True, exist_ok=True)

candidates = list(data_dir.glob('*.csv')) + list(data_dir.glob('*.CSV'))
if not candidates:
    print('[error] No CareLink CSV found in ' + str(data_dir) + '. Place Medtronic CareLink export as carelink.csv')
    Path(reports_dir / 'carelink_timeseries_summary.json').write_text(json.dumps({'error': 'No data', 'hint': 'Export CareLink CSV and place at ~/.hive/machine/workspace/diabetes_management/local-only/datasets/carelink.csv'}, indent=2))
    sys.exit(0)

carelink = data_dir / 'carelink.csv'
if not carelink.exists():
    carelink = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
print(f'Using CareLink file: {carelink}')

rows = []
with open(carelink, newline='', encoding='utf-8', errors='replace') as f:
    sample = f.read(2048)
    f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except:
        dialect = csv.excel
    reader = csv.DictReader(f, dialect=dialect)
    fieldnames = [h.strip() for h in (reader.fieldnames or [])]
    lower_map = {h.lower(): h for h in fieldnames}
    def pick(*names):
        for n in names:
            if n.lower() in lower_map:
                return lower_map[n.lower()]
        for n in names:
            for k in lower_map:
                if n.lower() in k:
                    return lower_map[k]
        return None
    sg_col = pick('Sensor Glucose (mg/dL)', 'Sensor Glucose', 'SG', 'Glucose', 'sensor')
    date_col = pick('Date', 'Timestamp', 'Datetime')
    time_col = pick('Time')
    print(f'Columns: {fieldnames}')
    print(f'Mapped SG={sg_col} Date={date_col} Time={time_col}')
    for rec in reader:
        r = {k.strip(): (v.strip() if isinstance(v,str) else v) for k,v in rec.items() if k}
        sg_val = None
        if sg_col and r.get(sg_col):
            raw = str(r.get(sg_col)).strip()
            if raw and raw not in ('---','', 'N/A'):
                try:
                    sg_val = float(raw.replace(',',''))
                except:
                    pass
        dt = None
        try:
            if date_col and time_col and r.get(date_col) and r.get(time_col):
                ds = r.get(date_col); ts = r.get(time_col)
                for fmt in ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M:%S', '%m/%d/%y %H:%M:%S'):
                    try:
                        dt = datetime.datetime.strptime(f'{ds} {ts}', fmt)
                        break
                    except:
                        continue
            elif date_col and r.get(date_col):
                ds = r.get(date_col)
                for fmt in ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%m/%d/%y %H:%M:%S', '%Y/%m/%d %H:%M'):
                    try:
                        dt = datetime.datetime.strptime(ds.strip(), fmt)
                        break
                    except:
                        continue
                if dt is None and 'T' in ds:
                    try:
                        dt = datetime.datetime.fromisoformat(ds.strip().replace('Z',''))
                    except:
                        pass
        except:
            pass
        if sg_val is not None and dt is not None:
            rows.append((dt, sg_val))
        elif sg_val is not None:
            rows.append((None, sg_val))
print(f'Parsed {len(rows)} SG readings')
if not rows:
    print('[error] No valid Sensor Glucose rows parsed — check CSV columns')
    sys.exit(0)
rows_with_dt = [r for r in rows if r[0] is not None]
rows_with_dt.sort(key=lambda x: x[0])
vals = [v for _,v in ([r for r in rows if r[0] is not None] or rows)]
if rows_with_dt:
    vals = [v for _,v in rows_with_dt]
    all_vals = [v for _,v in rows]
else:
    all_vals = vals
mean = statistics.mean(vals)
median = statistics.median(vals)
stdev = statistics.stdev(vals) if len(vals)>1 else 0
_min = min(vals)
_max = max(vals)
tir_70_180 = sum(1 for v in vals if 70 <= v <= 180)/len(vals)*100
below_70 = sum(1 for v in vals if v < 70)/len(vals)*100
below_54 = sum(1 for v in vals if v < 54)/len(vals)*100
above_180 = sum(1 for v in vals if v > 180)/len(vals)*100
above_250 = sum(1 for v in vals if v > 250)/len(vals)*100
_gmi = 3.31 + 0.02392 * mean
_ea1c = (mean + 46.7)/28.7
cv = stdev/mean*100 if mean else 0
from collections import defaultdict
daily = defaultdict(list)
hourly = defaultdict(list)
for dt, v in rows_with_dt:
    daily[dt.date().isoformat()].append(v)
    hourly[dt.hour].append(v)
daily_stats = {d: {'n': len(v), 'mean': round(statistics.mean(v),1), 'min': min(v), 'max': max(v), 'mean_mmol': mgdl_to_mmol(round(statistics.mean(v),1)), 'min_mmol': mgdl_to_mmol(min(v)), 'max_mmol': mgdl_to_mmol(max(v))} for d,v in sorted(daily.items())}
hourly_stats = {str(h): round(statistics.mean(v),1) for h,v in sorted(hourly.items())}
hourly_stats_mmol = {k: mgdl_to_mmol(v) for k,v in hourly_stats.items()}
summary = {
    'file': str(carelink),
    'readings': len(vals),
    'date_range': [rows_with_dt[0][0].isoformat() if rows_with_dt else None, rows_with_dt[-1][0].isoformat() if rows_with_dt else None],
    'mean_mgdl': round(mean,1),
    'mean_mmol': mgdl_to_mmol(round(mean,1)),
    'median_mgdl': round(median,1),
    'median_mmol': mgdl_to_mmol(round(median,1)),
    'stdev': round(stdev,1),
    'stdev_mmol': round(stdev / MGDL_TO_MMOL, 1),
    'cv_percent': round(cv,1),
    'min': _min, 'max': _max,
    'min_mmol': mgdl_to_mmol(_min),
    'max_mmol': mgdl_to_mmol(_max),
    'gmi_percent': round(_gmi,2),
    'ea1c_percent': round(_ea1c,2),
    'tir_70_180_percent': round(tir_70_180,1),
    'below_70_percent': round(below_70,1),
    'below_54_percent': round(below_54,1),
    'above_180_percent': round(above_180,1),
    'above_250_percent': round(above_250,1),
    'thresholds_mgdl': {'low': 70, 'high': 180, 'very_low': 54, 'very_high': 250},
    'thresholds_mmol': {'low': 3.9, 'high': 10.0, 'very_low': 3.0, 'very_high': 13.9},
    'daily': daily_stats,
    'hourly_mean': hourly_stats,
    'hourly_mean_mmol': hourly_stats_mmol,
}
print(json.dumps(summary, indent=2))
Path(reports_dir / 'carelink_timeseries_summary.json').write_text(json.dumps(summary, indent=2))
import csv as csv2
with open(reports_dir / 'carelink_daily_timeseries.csv','w',newline='') as f:
    w=csv2.writer(f)
    w.writerow(['date','n','mean_mgdl','mean_mmol','min_mgdl','min_mmol','max_mgdl','max_mmol'])
    for d,s in sorted(daily_stats.items()): w.writerow([d,s['n'],s['mean'],s['mean_mmol'],s['min'],s['min_mmol'],s['max'],s['max_mmol']])
with open(reports_dir / 'carelink_hourly_timeseries.csv','w',newline='') as f:
    w=csv2.writer(f)
    w.writerow(['hour','mean_mgdl','mean_mmol'])
    for h,m in sorted(hourly_stats.items(), key=lambda x: int(x[0])): w.writerow([h,m, mgdl_to_mmol(m)])
with open(reports_dir / 'carelink_raw_timeseries.csv','w',newline='') as f:
    w=csv2.writer(f)
    w.writerow(['timestamp','sg_mgdl','sg_mmol'])
    for dt,v in rows_with_dt: w.writerow([dt.isoformat(), v, mgdl_to_mmol(v)])
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from collections import defaultdict
    daily_groups = defaultdict(list)
    for dt, v in rows_with_dt:
        daily_groups[dt.date().isoformat()].append((dt, v))
    sorted_days = sorted(daily_groups.keys())
    cmap = plt.get_cmap('tab10')
    # 24h trend + frequency bands — mg/dL
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
    for idx, day in enumerate(sorted_days):
        pts = daily_groups[day]
        xs = [dt.hour + dt.minute/60 + dt.second/3600 for dt, _ in pts]
        ys = [v for _, v in pts]
        xy = sorted(zip(xs, ys))
        if xy:
            xs_s, ys_s = zip(*xy)
            ax1.plot(xs_s, ys_s, linewidth=1.0, alpha=0.85, label=day, color=cmap(idx % 10))
    ax1.axhspan(70, 180, color='green', alpha=0.08, label='TIR 70-180 mg/dL (3.9-10.0 mmol/L)')
    ax1.axhline(70, color='red', linestyle='--', linewidth=0.6)
    ax1.axhline(180, color='orange', linestyle='--', linewidth=0.6)
    ax1.set_title('Medtronic CareLink — 24h Trend per Day (mg/dL) + Frequency Bands')
    ax1.set_ylabel('mg/dL')
    ax1.set_xlim(0, 24)
    ax1.set_ylim(40, max(300, _max+20))
    ax1.grid(alpha=0.15)
    if len(sorted_days) <= 10:
        ax1.legend(loc='upper right', fontsize=8, ncol=2)
    else:
        ax1.legend(loc='upper right', fontsize=7, ncol=3)
    for idx, day in enumerate(sorted_days):
        pts = daily_groups[day]
        hourly_counts = [0]*24
        for dt, _ in pts:
            hourly_counts[dt.hour] += 1
        ax2.plot(range(24), hourly_counts, marker='o', linewidth=1.0, alpha=0.85, label=day, color=cmap(idx % 10))
    if sorted_days:
        avg_counts = []
        for h in range(24):
            vals_h = [sum(1 for dt,_ in daily_groups[d] if dt.hour == h) for d in sorted_days]
            avg_counts.append(sum(vals_h)/len(vals_h) if vals_h else 0)
        ax2.plot(range(24), avg_counts, linewidth=2.5, linestyle='--', color='black', label='avg', alpha=0.9)
        ax2.axhspan(0, 6, color='red', alpha=0.07, label='low freq')
        ax2.axhspan(6, 12, color='green', alpha=0.07, label='expected ~12/hr')
        ax2.axhspan(12, 14, color='orange', alpha=0.07)
    ax2.set_title('Frequency Bands — Readings per Hour per Day')
    ax2.set_xlabel('Hour of day')
    ax2.set_ylabel('Frequency')
    ax2.set_xlim(0, 23)
    ax2.set_xticks(range(0, 24, 2))
    ax2.set_ylim(0, 14)
    ax2.grid(alpha=0.15)
    plt.tight_layout()
    plt.savefig(reports_dir / 'carelink_timeseries.png', dpi=150)
    plt.close()
    # mmol version
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
    for idx, day in enumerate(sorted_days):
        pts = daily_groups[day]
        xs = [dt.hour + dt.minute/60 + dt.second/3600 for dt, _ in pts]
        ys = [mgdl_to_mmol(v) for _, v in pts]
        xy = sorted(zip(xs, ys))
        if xy:
            xs_s, ys_s = zip(*xy)
            ax1.plot(xs_s, ys_s, linewidth=1.0, alpha=0.85, label=day, color=cmap(idx % 10))
    ax1.axhspan(3.9, 10.0, color='green', alpha=0.08, label='TIR 3.9-10.0 mmol/L')
    ax1.axhline(3.9, color='red', linestyle='--', linewidth=0.6)
    ax1.axhline(10.0, color='orange', linestyle='--', linewidth=0.6)
    ax1.set_title('Medtronic CareLink — 24h Trend per Day (mmol/L) + Frequency Bands')
    ax1.set_ylabel('mmol/L')
    ax1.set_xlim(0, 24)
    ax1.set_ylim(2.2, max(16.5, mgdl_to_mmol(_max)+1))
    ax1.grid(alpha=0.15)
    if len(sorted_days) <= 10:
        ax1.legend(loc='upper right', fontsize=8, ncol=2)
    else:
        ax1.legend(loc='upper right', fontsize=7, ncol=3)
    for idx, day in enumerate(sorted_days):
        pts = daily_groups[day]
        hourly_counts = [0]*24
        for dt, _ in pts:
            hourly_counts[dt.hour] += 1
        ax2.plot(range(24), hourly_counts, marker='o', linewidth=1.0, alpha=0.85, label=day, color=cmap(idx % 10))
    if sorted_days:
        avg_counts = []
        for h in range(24):
            vals_h = [sum(1 for dt,_ in daily_groups[d] if dt.hour == h) for d in sorted_days]
            avg_counts.append(sum(vals_h)/len(vals_h) if vals_h else 0)
        ax2.plot(range(24), avg_counts, linewidth=2.5, linestyle='--', color='black', label='avg', alpha=0.9)
        ax2.axhspan(0, 6, color='red', alpha=0.07)
        ax2.axhspan(6, 12, color='green', alpha=0.07)
        ax2.axhspan(12, 14, color='orange', alpha=0.07)
    ax2.set_title('Frequency Bands — Readings per Hour per Day')
    ax2.set_xlabel('Hour of day')
    ax2.set_ylabel('Frequency')
    ax2.set_xlim(0, 23)
    ax2.set_xticks(range(0, 24, 2))
    ax2.set_ylim(0, 14)
    ax2.grid(alpha=0.15)
    plt.tight_layout()
    plt.savefig(reports_dir / 'carelink_timeseries_mmol.png', dpi=150)
    plt.close()
    print(f'24h trend + frequency bands saved: {reports_dir / "carelink_timeseries.png"} and _mmol')
    if daily_stats:
        plt.figure(figsize=(10, 3))
        ds = list(daily_stats.keys())
        ms = [daily_stats[d]['mean'] for d in ds]
        plt.plot(ds, ms, marker='o', linewidth=1)
        plt.axhspan(70, 180, color='green', alpha=0.08)
        plt.title('Daily Mean Glucose (mg/dL)')
        plt.xticks(rotation=30, ha='right')
        plt.tight_layout()
        plt.savefig(reports_dir / 'carelink_daily.png', dpi=150)
        plt.close()
        plt.figure(figsize=(10, 3))
        ms_mmol = [mgdl_to_mmol(m) for m in ms]
        plt.plot(ds, ms_mmol, marker='o', linewidth=1, color='#4f8cff')
        plt.axhspan(3.9, 10.0, color='green', alpha=0.08)
        plt.title('Daily Mean Glucose (mmol/L)')
        plt.xticks(rotation=30, ha='right')
        plt.tight_layout()
        plt.savefig(reports_dir / 'carelink_daily_mmol.png', dpi=150)
        plt.close()
        print(f'Daily charts saved')
except Exception as e:
    print(f'[chart skipped] {e}')
    try:
        bmin, bmax = 50, 300
        blocks=' ▁▂▃▄▅▆▇█'
        line=''.join(blocks[min(len(blocks)-1, max(0, int((v-bmin)/(bmax-bmin)*(len(blocks)-1))))] for v in vals[:120])
        Path(reports_dir / 'carelink_timeseries_ascii.txt').write_text(f'ASCII sparkline (first 120 readings, 50-300 mg/dL):\\n{line}\\n')
    except Exception:
        pass
print(f'Done — reports in {reports_dir} (mg/dL and mmol/L — 24h per day trend + frequency bands)')
"""
            },
            "access": "local-only"
        },
        {
            "id": "llm_report",
            "llm": {
                "provider": "ollama",
                "model": "",
                "prompt": "You are a diabetes educator assistant. Using ONLY the CareLink timeseries summary below, write a concise, non-diagnostic educational report in Markdown. Units: mg/dL and mmol/L (1 mmol/L = 18.01559 mg/dL). Include both units like 150 mg/dL (8.3 mmol/L).\n\nSummary JSON:\n${analyze_timeseries}\n\nRequired sections:\n# Diabetes Management — CareLink Timeseries Report\n## Overview (readings, date range, mean/median in both units, GMI/eA1c, CV)\n## Time in Ranges (TIR 70-180 mg/dL / 3.9-10.0 mmol/L, below 70/3.9, below 54/3.0, above 180/10.0, above 250/13.9 — with brief plain-English interpretation, include both units)\n## Daily & Hourly Patterns (what hours/days trend high or low; variability — cite both units, reference 24h per day trend + frequency bands overlay chart)\n## Observations (3-5 bullets, data-driven; flag hypo/hyper frequency and overnight patterns)\n## Next Steps (educational, non-prescriptive: logging, clinician discussion, sensor wear, pattern review)\n\nConstraints: Be concise, data-grounded, no invented values, include disclaimer that this is educational information not medical advice and to consult healthcare professional.\nIf summary shows error/no data, explain how to export CareLink CSV and place it at datasets/carelink.csv.\n"
            },
            "access": "local-only"
        },
        {
            "id": "write_report",
            "tool": "write_file",
            "args": {
                "path": "reports/carelink_report.md",
                "content": "# Diabetes Management — Medtronic CareLink Report\n\n> Local-only — generated via Hive-Machine with local Ollama model. Educational use only; not medical advice. Units: mg/dL ↔ mmol/L (÷18.01559) — 24h per day trend + frequency bands — dashboard toggle available.\n\n${llm_report}\n\n---\n\n## Appendix: Timeseries Data (dual units — 24h per day trend + frequency bands)\n\n- Summary JSON: `reports/carelink_timeseries_summary.json` (contains mgdl + mmol)\n- Daily timeseries: `reports/carelink_daily_timeseries.csv` (mgdl + mmol columns)\n- Hourly timeseries: `reports/carelink_hourly_timeseries.csv` (mgdl + mmol)\n- Raw timeseries: `reports/carelink_raw_timeseries.csv` (mgdl + mmol)\n- Charts: `reports/carelink_timeseries.png` (24h trend + frequency bands mg/dL), `reports/carelink_timeseries_mmol.png` (24h trend + frequency bands mmol/L), `reports/carelink_daily.png` / `carelink_daily_mmol.png`\n\nGenerated from `${analyze_timeseries}`\n"
            },
            "access": "local-only"
        },
        {
            "id": "verify",
            "tool": "read_file",
            "args": {"path": "reports/carelink_report.md"},
            "access": "local-only"
        }
    ]
}

