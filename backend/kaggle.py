"""Kaggle dataset import: download + extract a public dataset into a project.

Talks to the Kaggle public REST API (https://www.kaggle.com/api/v1) using HTTP
Basic auth from the workbench config (`kaggle.username` / `kaggle.key`) or the
standard KAGGLE_USERNAME / KAGGLE_KEY environment variables. No third-party
dependency — only the stdlib, so it works on any install.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .state import CONFIG

DATASET_RE = re.compile(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$")
DOWNLOAD_URL = "https://www.kaggle.com/api/v1/datasets/download/{owner}/{dataset}"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB safety cap
_CHUNK = 1 << 20


class KaggleError(RuntimeError):
    """Upstream/download failure surfaced to the API as a 5xx or 4xx."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def kaggle_credentials() -> tuple[str, str]:
    cfg = CONFIG.get("kaggle") or {}
    username = (cfg.get("username") or os.environ.get("KAGGLE_USERNAME") or "").strip()
    key = (cfg.get("key") or os.environ.get("KAGGLE_KEY") or "").strip()
    return username, key


def has_credentials() -> bool:
    u, k = kaggle_credentials()
    return bool(u and k)


def validate_slug(slug: str) -> tuple[str, str]:
    """Return (owner, dataset) or raise ValueError for a bad slug."""
    slug = (slug or "").strip().strip("/")
    if not DATASET_RE.match(slug):
        raise ValueError("invalid dataset slug — expected the form 'owner/dataset'")
    owner, dataset = slug.split("/", 1)
    return owner, dataset


def import_dataset(rt, slug: str) -> dict:
    """Download `slug` into the project's data/ dir and extract it.

    Returns {"dataset", "dir", "files": [relative paths]} where the paths are
    project-relative so both the file picker and the kernels can address them.
    """
    owner, dataset = validate_slug(slug)
    if not has_credentials():
        raise ValueError(
            "Kaggle credentials are not configured. Add your Kaggle username and "
            "API key in Settings (or set KAGGLE_USERNAME / KAGGLE_KEY).")

    dest_dir = rt.dir / "data" / f"{owner}__{dataset}"
    import shutil

    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = DOWNLOAD_URL.format(owner=owner, dataset=dataset)
    tmp = tempfile.NamedTemporaryFile(suffix=".kaggle-dl", delete=False).name
    try:
        _download(url, Path(tmp))
        files = _extract(Path(tmp), dest_dir, fallback_name=dataset)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return {"dataset": f"{owner}/{dataset}",
            "dir": f"data/{owner}__{dataset}",
            "files": sorted(files)}


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"Authorization": _basic_auth()})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
            size = 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise KaggleError("dataset exceeds the 2 GiB import cap")
                out.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise KaggleError("Kaggle rejected the credentials (401)", status=401)
        if e.code == 403:
            raise KaggleError("Kaggle denied access (403) — this dataset may be "
                              "private or require accepting its terms", status=403)
        if e.code == 404:
            raise KaggleError("Kaggle dataset not found (404)", status=404)
        raise KaggleError(f"Kaggle API error: HTTP {e.code}", status=502)
    except urllib.error.URLError as e:
        raise KaggleError(f"Could not reach Kaggle: {e.reason}")


def _basic_auth() -> str:
    u, k = kaggle_credentials()
    return "Basic " + base64.b64encode(f"{u}:{k}".encode()).decode()


def _extract(archive: Path, dest_dir: Path, fallback_name: str = "data") -> list[str]:
    """Extract a zip (or copy a single file) into dest_dir, guarding against
    zip-slip (member paths escaping dest_dir). Returns project-relative paths."""
    root = dest_dir.resolve()
    members = _zip_members(archive)
    if members is not None:
        with zipfile.ZipFile(archive) as zf:
            for name in members:
                target = (dest_dir / name).resolve()
                if root not in target.parents and target != root:
                    raise KaggleError(f"archive entry escapes the target dir: {name}")
            zf.extractall(dest_dir)
    else:
        # Not a zip: copy the file into dest_dir under the dataset name.
        import shutil

        shutil.copyfile(archive, dest_dir / fallback_name)

    project_root = dest_dir.parent.parent  # <project>/data/<owner>__<dataset>
    files = []
    for p in dest_dir.rglob("*"):
        if p.is_file():
            files.append(p.relative_to(project_root).as_posix())
    return files


def _zip_members(archive: Path) -> list[str] | None:
    """List zip member names, or None if the file isn't a zip archive."""
    try:
        with zipfile.ZipFile(archive) as zf:
            return zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
