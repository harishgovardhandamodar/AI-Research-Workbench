"""Jupyter Notebook (ipynb, nbformat v4) support.

Notebooks live in `<project>/notebooks/`. Code cells are executed in the project's
persistent Python kernel and their outputs (stdout streams, PNG figures, errors)
are written back into the notebook so results are held there with full provenance.

Executing a notebook from the shared `examples/notebooks/` directory runs the
experiment and saves the executed result into the project's notebook store.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .paths import ROOT

EXAMPLES_NOTEBOOKS_DIR = ROOT / "examples" / "notebooks"

ArtifactFn = Callable[[str, str], Awaitable[object]]  # (b64_png, source) -> artifact

# Scale tag from the numeric notebook prefix (00_tiny, 01/06-08 simple, ...).
_SCALE_BY_PREFIX = {
    "00": "tiny", "01": "simple", "06": "simple", "07": "simple", "08": "simple",
    "02": "mid", "04": "mid", "09": "mid", "10": "mid", "11": "mid", "12": "mid",
    "13": "mid", "14": "mid", "18": "mid", "19": "mid", "20": "mid", "21": "mid",
    "22": "mid", "23": "mid",
    "03": "large", "05": "large", "15": "large", "16": "large", "17": "large",
}
_DOMAIN_BY_KEYWORD = [
    ("obfuscation", "privacy"), ("privacy", "privacy"),
    ("differential", "privacy"), ("synthetic", "privacy"),
    ("clustering", "single-cell"), ("cell_", "single-cell"),
    ("volcano", "transcriptomics"),
    ("decay", "kinetics"), ("protein", "structural bio"),
    ("metabol", "metabolomics"), ("epidem", "epidemiology"),
    ("regression", "statistics"), ("quick", "statistics"),
    ("clt", "probability"), ("monte_carlo", "simulation"),
    ("lotka", "ecology"), ("double_pendulum", "physics"), ("heat", "physics"),
    ("forecast", "time series"), ("ar_", "time series"),
    ("hierarchical", "bioinformatics"), ("benchmark", "machine learning"),
    ("convolution", "image processing"), ("image_", "image processing"),
    ("logistic", "population"),
]


def default_tags(name: str) -> list[str]:
    """Derive [scale, domain] tags from a notebook name (examples naming)."""
    tags: list[str] = []
    m = re.match(r"(\d{2})_", name)
    if m and m.group(1) in _SCALE_BY_PREFIX:
        tags.append(_SCALE_BY_PREFIX[m.group(1)])
    low = name.lower()
    for kw, dom in _DOMAIN_BY_KEYWORD:
        if kw in low:
            if dom not in tags:
                tags.append(dom)
            break
    return tags


def notebook_tags(nb: dict, name: str) -> list[str]:
    """Tags from notebook metadata (fox.tags) or derived from the name."""
    stored = (nb.get("metadata") or {}).get("fox", {}).get("tags")
    if isinstance(stored, list) and stored:
        return [str(t) for t in stored]
    return default_tags(name)


def notebook_description(nb: dict) -> str:
    """First heading/text line of the first markdown cell, as a short blurb."""
    for c in nb.get("cells", []):
        if c.get("cell_type") == "markdown":
            src = "".join(_norm_source(c.get("source", "")))
            for line in src.splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    return line[:120]
            break
    return ""


class NotebookError(RuntimeError):
    pass


def _norm_source(source) -> list[str]:
    if isinstance(source, str):
        return source.splitlines(keepends=True)
    if isinstance(source, list):
        return [str(s) for s in source]
    return []


def new_notebook(cells: list | None = None, name: str = "untitled") -> dict:
    nb = {
        "cells": [],
        "metadata": {
            "kernelspec": {"display_name": "Python 3 (Fox)", "language": "python",
                           "name": "python3"},
            "language_info": {"name": "python", "version": "3", "mimetype": "text/x-python"},
            "fox": {"execution_count": 0, "name": name},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    for c in cells or []:
        cell_type = c.get("cell_type") or "code"
        source = _norm_source(c.get("source", ""))
        nb["cells"].append({
            "cell_type": cell_type,
            "id": c.get("id") or uuid.uuid4().hex[:12],
            "metadata": {},
            "source": source,
            "execution_count": None,
            "outputs": [] if cell_type == "code" else None,
        })
    return nb


def error_output(err: str) -> dict:
    lines = err.splitlines()
    first = lines[0] if lines else "Error"
    m = re.match(r"^([A-Za-z_][\w.]*)(?::\s*(.*))?$", first)
    ename = m.group(1) if m else "Error"
    evalue = (m.group(2) if m and m.group(2) else first).strip()
    return {"output_type": "error", "ename": ename, "evalue": evalue, "traceback": lines}


class NotebookService:
    def __init__(self, project_dir: Path, kernel):
        self.project_dir = project_dir
        self.notebooks_dir = project_dir / "notebooks"
        self.notebooks_dir.mkdir(parents=True, exist_ok=True)
        self.kernel = kernel

    # -- paths ------------------------------------------------------------
    @staticmethod
    def _safe(name: str) -> str:
        base = Path(name).stem  # strip .ipynb and any directories
        safe = re.sub(r"[^\w\-.]", "_", base).strip().strip(".")
        return safe or "untitled"

    def project_path(self, name: str) -> Path:
        return self.notebooks_dir / (self._safe(name) + ".ipynb")

    def resolve_path(self, name: str) -> tuple[Path, bool]:
        """Return (path, external). external=True when loaded from examples/."""
        p = self.project_path(name)
        if p.exists():
            return p, False
        alt = EXAMPLES_NOTEBOOKS_DIR / (self._safe(name) + ".ipynb")
        if alt.exists():
            return alt, True
        return p, False

    # -- CRUD ---------------------------------------------------------------
    def list(self) -> list[dict]:
        out = []
        seen = set()
        for base, external in ((self.notebooks_dir, False),
                               (EXAMPLES_NOTEBOOKS_DIR, True)):
            if not base.exists():
                continue
            for f in sorted(base.glob("*.ipynb")):
                if f.stem in seen:
                    continue
                seen.add(f.stem)
                try:
                    nb = self.load(f.stem)
                except NotebookError:
                    continue
                out.append({
                    "name": f.stem,
                    "cells": len(nb["cells"]),
                    "code_cells": sum(1 for c in nb["cells"] if c.get("cell_type") == "code"),
                    "executions": nb.get("metadata", {}).get("fox", {}).get("execution_count", 0),
                    "updated": f.stat().st_mtime,
                    "source": "examples" if external else "project",
                    "tags": notebook_tags(nb, f.stem),
                    "description": notebook_description(nb),
                })
        return out

    def load(self, name: str) -> dict:
        path, _ = self.resolve_path(name)
        if not path.exists():
            raise NotebookError(f"notebook '{name}' not found")
        try:
            nb = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise NotebookError(f"invalid notebook JSON for '{name}': {e}") from e
        nb.setdefault("metadata", {})
        nb["metadata"].setdefault("fox", {"execution_count": 0})
        if not isinstance(nb.get("cells"), list):
            raise NotebookError(f"notebook '{name}' has no cells")
        return nb

    def save(self, name: str, nb: dict) -> dict:
        path = self.project_path(name)
        nb.setdefault("metadata", {})
        nb["metadata"].setdefault("fox", {})
        path.write_text(json.dumps(nb, indent=1))
        return nb

    # -- execution ----------------------------------------------------------
    async def execute(self, name: str, indices: list[int] | None = None,
                      on_artifact: ArtifactFn | None = None,
                      prelude: str = "",
                      cell_timeout: float = 120.0) -> dict:
        """Execute cells of a notebook.

        `prelude` (optional) runs once in the kernel before the cells — used to
        inject a fresh seed for reruns. `cell_timeout` caps each code cell's
        wall-clock so a hung cell can't stall a whole notebook run.
        """
        nb = self.load(name)
        if prelude and prelude.strip():
            await self.kernel.run_code(prelude, timeout=cell_timeout)
        meta = nb["metadata"].setdefault("fox", {})
        counter = meta.get("execution_count", 0)
        cells = nb["cells"]
        target = list(range(len(cells))) if indices is None else [int(i) for i in indices]
        report: list[dict] = []
        for i in target:
            if not (0 <= i < len(cells)):
                continue
            cell = cells[i]
            if cell.get("cell_type") != "code":
                continue
            counter += 1
            result = await self._exec_cell(cell, counter, on_artifact, cell_timeout)
            report.append({
                "index": i,
                "ok": result["ok"],
                "outputs": len(result["outputs"]),
                "error": result.get("error", ""),
                "figures": result["figures"],
            })
        meta["execution_count"] = counter
        nb["metadata"]["fox"] = meta
        # Results are held in the project's copy of the notebook.
        self.save(name, nb)
        return {"notebook": nb, "report": report}

    async def _exec_cell(self, cell: dict, exec_count: int,
                         on_artifact: ArtifactFn | None,
                         cell_timeout: float = 120.0) -> dict:
        source = "".join(_norm_source(cell.get("source", ""))).strip()
        outputs: list[dict] = []
        ok, error = True, ""
        figures = 0
        if not source:
            pass
        else:
            resp = await self.kernel.run_code(source, timeout=cell_timeout)
            if resp.get("error"):
                ok, error = False, resp["error"]
                outputs.append(error_output(resp["error"]))
            else:
                out_text = resp.get("output") or ""
                if out_text:
                    lines = out_text.splitlines(keepends=True) or [""]
                    outputs.append({"output_type": "stream", "name": "stdout", "text": lines})
                for fig in resp.get("figures", []):
                    figures += 1
                    if on_artifact is not None:
                        try:
                            art = await on_artifact(fig, source)
                            note = f"artifact {getattr(art, 'id', '')}"
                        except Exception:  # noqa: BLE001
                            note = ""
                    else:
                        note = ""
                    outputs.append({
                        "output_type": "display_data",
                        "data": {"image/png": fig, "text/plain": f"[figure{(' — ' + note) if note else ''}]"},
                        "metadata": {},
                    })
        cell["outputs"] = outputs
        cell["execution_count"] = exec_count
        cell["metadata"]["fox"] = {
            "ok": ok,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"ok": ok, "outputs": outputs, "error": error, "figures": figures}
