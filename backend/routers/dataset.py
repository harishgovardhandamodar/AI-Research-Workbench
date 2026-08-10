"""Dataset loading routes: load a project data file into the Python kernel.

Lets the chat window "begin a session and load a dataset" in one step: point at
a project file (CSV/TSV/JSON/JSONL/Parquet/XLSX), and the backend reads it into
the project's persistent Python kernel as a DataFrame variable, then returns a
schema/preview card the UI can render inline — no manual `pd.read_csv` needed.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..state import get_runtime

router = APIRouter()

_SCHEMA_READERS = {
    ".csv": "read_csv",
    ".tsv": "read_csv",
    ".txt": "read_csv",
    ".json": "read_json",
    ".jsonl": "read_json",
    ".parquet": "read_parquet",
    ".xlsx": "read_excel",
}

# Map pandas reader -> kwargs used to load into the kernel (mirrors the schema
# preview logic so the in-kernel DataFrame matches what was previewed).
_READ_KWARGS = {
    "read_csv": {"sep": ",", "low_memory": False},
    "read_json": {},
    "read_parquet": {},
    "read_excel": {},
}

_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_project_file(name: str, filename: str) -> Path:
    rt = get_runtime(name)
    rel = Path(filename)
    # Only top-level project files and the data/ subfolder are exposed; keep
    # path traversal impossible.
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="invalid filename")
    cand = rt.dir / rel
    if not cand.exists() or not cand.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return cand


@router.post("/api/projects/{name}/dataset/load")
async def dataset_load(name: str, body: dict):
    """Load a project data file into the Python kernel as a DataFrame.

    body: {filename, var?} — ``var`` defaults to the file stem (sanitized).
    Returns {var, ok, shape, columns, dtypes, preview, file, message} so the UI
    can render an inline "loaded dataset" card.
    """
    filename = (body.get("filename") or "").strip()
    if not filename:
        return JSONResponse({"error": "filename required"}, status_code=400)
    path = _resolve_project_file(name, filename)
    ext = path.suffix.lower()
    reader = _SCHEMA_READERS.get(ext)
    if reader is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported type '{ext}' (try csv/tsv/json/parquet/xlsx)")

    var = (body.get("var") or "").strip() or re.sub(r"[^A-Za-z0-9_]", "_",
                                                    path.stem)
    if not _VAR_RE.match(var):
        raise HTTPException(status_code=400,
                            detail=f"invalid variable name: {var}")
    if var in ("pd", "np", "plt", "sns", "os", "sys", "math", "json",
               "report_metric", "report_dataset", "save_artifact"):
        raise HTTPException(status_code=400,
                            detail=f"'{var}' is a reserved kernel name")

    rt = get_runtime(name)
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError:
        raise HTTPException(status_code=501,
                            detail="pandas not available on server")

    # Preview/schema for the card (mirrors the Files-tab schema logic).
    schema = None
    try:
        from .artifacts import _schema_from_file
        schema = _schema_from_file(path)
    except Exception:  # noqa: BLE001
        schema = None

    # Build + run the kernel snippet: read the file into `var`.
    fpath = str(path)
    if reader == "read_csv":
        code = (f"import pandas as pd\n"
                f"{var} = pd.read_csv({fpath!r}, low_memory=False)")
    else:
        code = (f"import pandas as pd\n"
                f"{var} = pd.{reader}({fpath!r})")

    try:
        result = await rt.kernels.python.run_code(code, timeout=120.0)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"kernel error: {type(e).__name__}: {e}")

    if not result.get("ok"):
        raise HTTPException(status_code=422,
                            detail=result.get("error") or "kernel failed to load")

    # Shape + a small preview from the kernel itself (authoritative). Print a
    # sentinel JSON line and parse it back out of the captured output.
    info_code = (
        "import json\n"
        f"__d = dict(shape=list({var}.shape), "
        f"cols=[str(c) for c in {var}.columns], "
        f"dtypes={{str(c): str({var}[c].dtype) for c in {var}.columns[:40]}}, "
        f"head={var}.head(5).to_dict('records'))\n"
        "print('__DATASET_INFO__' + json.dumps(__d, default=str))")
    shape, cols, dtypes, preview = None, [], {}, []
    try:
        info = await rt.kernels.python.run_code(info_code, timeout=30.0)
        out = info.get("output") or ""
        marker = "__DATASET_INFO__"
        idx = out.find(marker)
        if idx != -1:
            import json as _json
            payload = out[idx + len(marker):].splitlines()[0]
            data = _json.loads(payload)
            shape, cols, dtypes, preview = (
                data.get("shape"), data.get("cols") or [],
                data.get("dtypes") or {}, data.get("head") or [])
    except Exception:  # noqa: BLE001
        shape, cols, dtypes, preview = None, [], {}, []

    return {
        "var": var, "ok": True, "file": filename, "size": path.stat().st_size,
        "shape": list(shape) if shape else None,
        "columns": cols, "dtypes": dtypes, "preview": preview,
        "schema": schema,
        "message": (f"Loaded `{filename}` as `{var}` "
                    + (f"({shape[0]} rows × {shape[1]} columns)" if shape else "")
                    + ". Ask Fox to analyze it — the DataFrame is live in the kernel."),
    }


@router.get("/api/projects/{name}/dataset/list")
async def dataset_list(name: str):
    """List loadable tabular data files in the project (top level + data/)."""
    rt = get_runtime(name)
    out = []
    for sub in (rt.dir, rt.dir / "data"):
        if not sub.is_dir():
            continue
        for p in sorted(sub.iterdir()):
            if p.is_file() and p.suffix.lower() in _SCHEMA_READERS:
                out.append({"name": str(p.relative_to(rt.dir)),
                            "size": p.stat().st_size,
                            "suffix": p.suffix.lower()})
    return {"files": out}
