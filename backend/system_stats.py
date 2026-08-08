"""Live server resource stats for the dgxtop-style HUD overlay.

Collects host CPU/memory/load, per-GPU devices (via ``nvidia-smi``) and a
``ps``-style process table by reading ``/proc`` directly (the slim Docker base
image has no ``ps``/``psutil``). Results are cached with a short TTL so UI
polling never spawns subprocesses on every request.

In-container only: the workbench container already receives GPU passthrough, so
this reflects the resources the server has made available to it.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

_PROC = Path("/proc")

# Cache so a 3s UI poll loop doesn't re-run nvidia-smi / parse all of /proc each
# second. Guarded by a lock because FastAPI workers may call concurrently.
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 3.0

_HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_LAST_CPU = {"ts": 0.0, "total": 0, "idle": 0, "cores": {}}  # for usage deltas


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _host_stats() -> dict:
    loadavg = (_read(str(_PROC / "loadavg")).split() or ["0 0 0"])[:3]
    try:
        uptime = float(_read(str(_PROC / "uptime")).split()[0])
    except (IndexError, ValueError):
        uptime = 0.0
    cores = _cpu_cores()
    cpu_pct = _cpu_usage()
    mem = _mem_stats()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu": {
            "count": cores,
            "usage_percent": cpu_pct["total"],
            "per_core": cpu_pct["cores"],
        },
        "memory": mem,
        "loadavg": [float(x) if x.replace(".", "").isdigit() else 0.0 for x in loadavg],
        "uptime_s": uptime,
    }


def _cpu_cores() -> int:
    n = 0
    try:
        for line in _read(str(_PROC / "stat")).splitlines():
            if line.startswith("cpu") and line[3].isdigit():
                n += 1
    except Exception:  # noqa: BLE001
        pass
    return n or os.cpu_count() or 1


def _parse_cpu_line(line: str) -> tuple[int, int]:
    """Return (total_jiffies, idle_jiffies) for a /proc/stat cpu line."""
    parts = line.split()
    if len(parts) < 5:
        return 0, 0
    try:
        nums = [int(x) for x in parts[1:8]]
    except ValueError:
        return 0, 0
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    return sum(nums), idle


def _cpu_usage() -> dict:
    """Per-core + aggregate usage % using /proc/stat deltas between samples."""
    stat = _read(str(_PROC / "stat"))
    now = time.time()
    cores: dict[int, tuple[int, int]] = {}
    total = idle = 0
    for line in stat.splitlines():
        if not line.startswith("cpu"):
            continue
        if line[3] == " " or line == "cpu":  # aggregate "cpu " line
            total, idle = _parse_cpu_line(line)
        elif line[3].isdigit():
            try:
                idx = int(line[3:].split()[0])
                cores[idx] = _parse_cpu_line(line)
            except ValueError:
                continue
    prev = _LAST_CPU
    prev_total, prev_idle = prev["total"], prev["idle"]
    prev_cores = prev["cores"]
    interval = now - prev["ts"]
    prev["ts"], prev["total"], prev["idle"] = now, total, idle
    if interval <= 0 or prev_total == 0:
        # First sample: no baseline yet.
        prev["cores"] = cores
        return {"total": 0.0, "cores": {}}
    d_total = max(total - prev_total, 0)
    d_idle = max(idle - prev_idle, 0)
    pct = 100.0 * (d_total - d_idle) / d_total if d_total else 0.0
    per_core = {}
    for idx, (t, i) in cores.items():
        pt, pi = prev_cores.get(idx, (0, 0))
        dt, di = max(t - pt, 0), max(i - pi, 0)
        per_core[idx] = round(100.0 * (dt - di) / dt, 1) if dt else 0.0
    prev["cores"] = cores
    return {"total": round(pct, 1), "cores": per_core}


def _mem_stats() -> dict:
    mem = {}
    for line in _read(str(_PROC / "meminfo")).splitlines():
        m = re.match(r"^(MemTotal|MemAvailable|MemFree|SwapTotal|SwapFree):\s+(\d+)\s+kB", line)
        if m:
            mem[m.group(1)] = int(m.group(2)) // 1024  # MB
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    return {
        "total_mb": total,
        "used_mb": max(total - available, 0),
        "available_mb": available,
        "swap_total_mb": mem.get("SwapTotal", 0),
        "swap_free_mb": mem.get("SwapFree", 0),
    }


# --------------------------------------------------------------------------- #
# GPU (nvidia-smi)
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _gpu_devices() -> list[dict]:
    out = _run([
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ])
    if not out.strip():
        return []
    devices = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        try:
            def _num(v):
                try:
                    return float(v)
                except ValueError:
                    return 0.0
            devices.append({
                "index": int(_num(parts[0])),
                "name": parts[1],
                "utilization_percent": _num(parts[2]),
                "memory_total_mb": int(_num(parts[3])),
                "memory_used_mb": int(_num(parts[4])),
                "memory_free_mb": int(_num(parts[5])),
                "temperature_c": _num(parts[6]),
                "power_watts": _num(parts[7]),
            })
        except (IndexError, ValueError):
            continue
    return devices


def _gpu_processes() -> list[dict]:
    out = _run([
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ])
    procs = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            procs.append({"pid": int(float(parts[0])), "name": parts[1],
                          "gpu_memory_mb": int(float(parts[2]))})
        except ValueError:
            continue
    return procs


# --------------------------------------------------------------------------- #
# process table (via /proc — no ps/psutil dependency)
# --------------------------------------------------------------------------- #

_PROC_PREV: dict[int, tuple[float, int]] = {}  # pid -> (sample_time, jiffies)


def _jiffies_of(pid: int) -> int:
    st = _read(f"{_PROC}/{pid}/stat")
    # comm is field 2 (wrapped in parens, may contain spaces); utime=14, stime=15.
    try:
        end_paren = st.rfind(")")
        if end_paren < 0:
            return 0
        fields = st[end_paren + 1:].split()
        # fields[11] == field 14 (utime), fields[12] == field 15 (stime)
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return 0


def _processes(limit: int = 40) -> list[dict]:
    now = time.time()
    rows = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        status = _read(str(entry / "status"))
        name = comm = ""
        rss_kb = 0
        uid = None
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("VmRSS:"):
                try:
                    rss_kb = int(line.split()[1])
                except (IndexError, ValueError):
                    rss_kb = 0
            elif line.startswith("Uid:"):
                try:
                    uid = int(line.split()[1])
                except (IndexError, ValueError):
                    uid = None
        cmdline = _read(str(entry / "cmdline")).replace("\x00", " ").strip()
        if not cmdline:
            cmdline = name or comm
        j = _jiffies_of(pid)
        prev = _PROC_PREV.get(pid)
        cpu_pct = 0.0
        if prev:
            interval = max(now - prev[0], 0.001)
            cpu_pct = (j - prev[1]) / max(_HZ * interval, 1) * 100.0
        _PROC_PREV[pid] = (now, j)
        rows.append({
            "pid": pid,
            "user": _username(uid),
            "cpu_percent": round(min(max(cpu_pct, 0.0), 100 * os.cpu_count() or 100), 1),
            "mem_mb": round(rss_kb / 1024, 1),
            "command": cmdline[:160],
        })
    # keep the table bounded in memory
    if len(_PROC_PREV) > 4000:
        _PROC_PREV.clear()
    rows.sort(key=lambda r: r["cpu_percent"], reverse=True)
    return rows[:limit]


_USER_CACHE: dict[int, str] = {}


def _username(uid: int | None) -> str:
    if uid is None:
        return "?"
    if uid in _USER_CACHE:
        return _USER_CACHE[uid]
    try:
        import pwd
        _USER_CACHE[uid] = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError):
        _USER_CACHE[uid] = str(uid)
    return _USER_CACHE[uid]


# --------------------------------------------------------------------------- #
# public API (cached)
# --------------------------------------------------------------------------- #

def get_stats() -> dict:
    with _CACHE_LOCK:
        cached = _CACHE.get("stats")
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1]
    stats = {
        "host": _host_stats(),
        "gpu": {"available": False, "count": 0, "devices": [], "processes": []},
        "processes": _processes(),
        "collected_at": time.time(),
    }
    devices = _gpu_devices()
    if devices:
        stats["gpu"]["available"] = True
        stats["gpu"]["count"] = len(devices)
        stats["gpu"]["devices"] = devices
        stats["gpu"]["processes"] = _gpu_processes()
    with _CACHE_LOCK:
        _CACHE["stats"] = (time.time(), stats)
    return stats
