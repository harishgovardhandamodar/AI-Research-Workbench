"""Kernel worker: a persistent Python subprocess that executes code over JSONL on
stdin/stdout. State (variables, loaded modules, matplotlib figures) persists across
`run_code` requests until `reset` is issued.

Protocol (line-delimited JSON on stdin -> same on stdout):
  request:  {"id": str, "op": "run_code"|"list_variables"|"get_env"|"reset"|"ping",
             "code": str, "timeout": float}
  response: {"id": str, "ok": bool, "output": str, "error": str,
             "figures": [b64png,...], "variables": {...}, "env": {...}}
"""

from __future__ import annotations

import ast
import base64
import io
import json
import os
import signal
import sys
import traceback

# ---------------------------------------------------------------- helpers -----

def _env_snapshot() -> dict:
    import platform
    key_pkgs = [
        "numpy", "scipy", "pandas", "matplotlib", "seaborn", "sklearn",
        "scikit-learn", "biopython", "Bio", "scanpy", "anndata", "rdkit",
        "pymol", "networkx", "statsmodels", "plotly", "pyarrow", "duckdb",
    ]
    out = {"python": platform.python_version(), "platform": platform.platform()}
    try:
        from importlib import metadata
        for name in key_pkgs:
            try:
                out[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return out


def _describe(value) -> str:
    t = type(value)
    if t.__module__ == "builtins":
        tname = t.__name__
    else:
        tname = f"{t.__module__}.{t.__name__}"
    try:
        if hasattr(value, "shape"):
            return f"{tname}(shape={list(value.shape)})"
        if hasattr(value, "__len__"):
            return f"{tname}(len={len(value)})"
    except Exception:  # noqa: BLE001
        pass
    return tname


def _list_variables(ns: dict) -> dict:
    vars_ = {}
    for name in list(ns.keys()):
        if name.startswith("_"):
            continue
        if name in ("In", "Out"):
            continue
        try:
            vars_[name] = _describe(ns[name])
        except Exception:  # noqa: BLE001
            vars_[name] = "<unreadable>"
        if len(vars_) >= 60:
            break
    return vars_


# ------------------------------------------------------------ matplotlib ------

os.environ.setdefault("MPLBACKEND", "Agg")
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _orig_show = plt.show

    def _silent_show(*args, **kwargs):
        for f in plt.get_fignums():
            plt.figure(f).canvas.draw()
        return None

    plt.show = _silent_show

    def _flush_figures() -> list[str]:
        figs = []
        for n in plt.get_fignums():
            fig = plt.figure(n)
            fig.canvas.draw()
            buf = io.BytesIO()
            try:
                fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
                figs.append(base64.b64encode(buf.getvalue()).decode("ascii"))
            finally:
                buf.close()
                plt.close(fig)
        return figs

except ImportError:
    plt = None

    def _flush_figures() -> list[str]:
        return []


# ----------------------------------------------------------------- kernel -----

def _compile(code: str):
    tree = ast.parse(code, mode="exec")
    if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr):
        expr = ast.Expression(body=tree.body[0].value)
        return "eval", compile(expr, "<kernel>", "eval")
    return "exec", compile(code, "<kernel>", "exec")


class _Timeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _Timeout("Kernel execution timed out")


def run_code(ns: dict, code: str, timeout: float) -> dict:
    out = io.StringIO()
    err = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout if timeout else 30.0)
        ok, error, value = True, "", None
        try:
            mode, compiled = _compile(code)
            if mode == "eval":
                value = eval(compiled, ns)  # noqa: S307 - intentional local eval
                if value is not None:
                    print(repr(value))
            else:
                exec(compiled, ns)  # noqa: S102 - intentional local exec
        except BaseException as e:  # noqa: BLE001
            ok, error = False, f"{type(e).__name__}: {e}"
            if isinstance(e, _Timeout):
                error = "Execution timed out"
            else:
                error += "\n" + traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    output = out.getvalue()
    if err.getvalue():
        output += err.getvalue()
    if len(output) > 50_000:
        output = output[-50_000:] + "\n...[truncated]"
    return {"ok": ok, "output": output, "error": error,
            "figures": _flush_figures(), "variables": _list_variables(ns)}


def main() -> None:
    ns = {"__name__": "__main__", "__builtins__": __builtins__}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id", "")
        op = req.get("op", "run_code")
        try:
            if op == "ping":
                resp = {"ok": True, "pong": True}
            elif op == "get_env":
                resp = {"ok": True, "env": _env_snapshot()}
            elif op == "list_variables":
                resp = {"ok": True, "variables": _list_variables(ns)}
            elif op == "reset":
                ns.clear()
                ns.update({"__name__": "__main__", "__builtins__": __builtins__})
                if plt is not None:
                    plt.close("all")
                resp = {"ok": True, "variables": {}}
            elif op == "run_code":
                resp = run_code(ns, req.get("code", ""), float(req.get("timeout", 30)))
            else:
                resp = {"ok": False, "error": f"unknown op {op}"}
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": f"kernel error: {e}"}
        resp["id"] = rid
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
