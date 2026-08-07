"""Kernel worker: a persistent Python subprocess that executes code over JSONL on
stdin/stdout. State (variables, loaded modules, matplotlib figures) persists across
`run_code` requests until `reset` is issued.

Protocol (line-delimited JSON on stdin -> same on stdout):
  request:  {"id": str, "op": "run_code"|"list_variables"|"get_env"|"reset"|"ping",
             "code": str, "timeout": float, "stream": bool}
            When `stream` is true the worker emits output frames as the code runs:
              {"frame": "output", "id": str, "text": str}
            before the final response frame.
  response: {"id": str, "ok": bool, "output": str, "error": str,
             "figures": [b64png,...], "variables": {...}, "env": {...},
             "metrics": {name: value}}
             (metrics collects values reported via report_metric() since the
             previous request, cleared on reset)
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
    _saved_figs: list[str] = []

    def _silent_show(*args, **kwargs):
        for f in plt.get_fignums():
            plt.figure(f).canvas.draw()
        return None

    plt.show = _silent_show

    def _capture_open_figs() -> list[str]:
        out = []
        for n in plt.get_fignums():
            fig = plt.figure(n)
            fig.canvas.draw()
            buf = io.BytesIO()
            try:
                fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
                out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
            finally:
                buf.close()
        return out

    def _savefig_wrapper(fname=None, *args, **kwargs):
        # If the model calls plt.savefig(...) we still record the rendered figure
        # as an artifact (it may then plt.close() the figure).
        try:
            _saved_figs.extend(_capture_open_figs())
        except Exception:  # noqa: BLE001
            pass
        if fname:
            return _orig_savefig(fname, *args, **kwargs)
        return None

    _orig_savefig = plt.savefig
    plt.savefig = _savefig_wrapper

    def _flush_figures() -> list[str]:
        figs = list(_saved_figs)
        _saved_figs.clear()
        for n in plt.get_fignums():
            fig = plt.figure(n)
            fig.canvas.draw()
            buf = io.BytesIO()
            try:
                fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
                b = base64.b64encode(buf.getvalue()).decode("ascii")
                if b not in figs:
                    figs.append(b)
            finally:
                buf.close()
                plt.close(fig)
        return figs

except ImportError:
    plt = None

    def _flush_figures() -> list[str]:
        return []


# ----------------------------------------------------------------- kernel -----

_METRICS: dict = {}


def _report_metric(name, value, *, step=None):
    """Structured metric reporting for experiments.

    Inside kernel code, call ``report_metric("acc", 0.91)`` (or with a ``step``
    for curves). Values must be real numbers/bools; everything else raises so a
    mislabeled call fails loudly instead of silently polluting run metrics.
    """
    if isinstance(value, bool):
        value = 1.0 if value else 0.0
    if not isinstance(value, (int, float)):
        raise TypeError(
            f"report_metric value for {name!r} must be a number, got "
            f"{type(value).__name__}")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("report_metric name must be a non-empty string")
    key = str(name).strip()
    if step is not None:
        key = f"{key}[step={int(step)}]"
    _METRICS[key] = float(value)
    return None


def _take_metrics() -> dict:
    out = dict(_METRICS)
    _METRICS.clear()
    return out


def _save_artifact_hint(**kwargs):
    raise RuntimeError(
        "save_artifact is an agent TOOL, not a Python function. Do not call it inside "
        "the kernel. To create an artifact, issue a separate save_artifact tool call, "
        "or just print/return the data. Figures are automatically saved as artifacts."
    )


def _base_ns() -> dict:
    return {"__name__": "__main__", "__builtins__": __builtins__,
            "save_artifact": _save_artifact_hint,
            "report_metric": _report_metric}


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


class _TeeStream:
    """A write-through stream: buffers output AND forwards chunks to a callback,
    so a headless kernel server can stream stdout to clients as code runs."""

    def __init__(self, buffer, on_chunk):
        self._buffer = buffer
        self._on_chunk = on_chunk

    def write(self, text):
        self._buffer.write(text)
        try:
            self._on_chunk(text)
        except Exception:  # noqa: BLE001
            pass
        return len(text)

    def flush(self):
        try:
            self._buffer.flush()
        except Exception:  # noqa: BLE001
            pass

    def writable(self):
        return True

    @property
    def encoding(self):
        return "utf-8"


def run_code(ns: dict, code: str, timeout: float, on_chunk=None) -> dict:
    out = io.StringIO()
    err = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    if on_chunk is not None:
        sys.stdout = _TeeStream(out, on_chunk)
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
            "figures": _flush_figures(), "variables": _list_variables(ns),
            "metrics": _take_metrics()}


def main() -> None:
    ns = _base_ns()
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
                ns.update(_base_ns())
                _METRICS.clear()
                if plt is not None:
                    plt.close("all")
                resp = {"ok": True, "variables": {}}
            elif op == "run_code":
                stream = bool(req.get("stream"))
                if stream:
                    # Capture the real JSONL stdout *before* run_code swaps in the
                    # tee stream, so output frames reach the parent channel
                    # instead of looping back through the tee (infinite recursion).
                    real_out = sys.stdout

                    def on_chunk(text, _rid=rid, _out=real_out):
                        _out.write(json.dumps(
                            {"frame": "output", "id": _rid, "text": text}) + "\n")
                        _out.flush()

                    resp = run_code(ns, req.get("code", ""),
                                    float(req.get("timeout", 30)),
                                    on_chunk=on_chunk)
                else:
                    resp = run_code(ns, req.get("code", ""),
                                    float(req.get("timeout", 30)))
            else:
                resp = {"ok": False, "error": f"unknown op {op}"}
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": f"kernel error: {e}"}
        resp["id"] = rid
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
