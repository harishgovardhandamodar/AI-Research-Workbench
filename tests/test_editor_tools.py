"""In-browser VS Code (code-server) editor tools: list/read/edit workspace files."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.agents.tools import (
    _editor_edit_file,
    _editor_list_files,
    _editor_read_file,
    _editor_safe,
)


class _FakeCtx:
    """Minimal stand-in: artifact store exposes project_dir; permissions allow."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir

    @property
    def artifacts(self):
        return self

    class _Perm:
        def check(self, kind, command):  # noqa: ARG002
            return "allow"

        def record(self, kind, command, decision):  # noqa: ARG002
            pass

    permissions = _Perm()
    approval = None


class TestEditorTools(unittest.IsolatedAsyncioTestCase):
    async def _make(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "artifacts").mkdir()
        (root / "knowledge_graphs").mkdir()
        (root / "artifacts" / "report.md").write_text(
            "# Draft report\n\nThe result is 0.50.\n")
        ctx = _FakeCtx(root)
        self.addCleanup(tmp.cleanup)
        return ctx

    async def test_list_files(self):
        ctx = await self._make()
        out = await _editor_list_files(ctx)
        self.assertIn("artifacts/report.md", out)

    async def test_read_file(self):
        ctx = await self._make()
        out = await _editor_read_file(ctx, "artifacts/report.md")
        self.assertIn("The result is 0.50", out)

    async def test_edit_file_replaces_once(self):
        ctx = await self._make()
        out = await _editor_edit_file(ctx, "artifacts/report.md",
                                      "0.50", "0.62")
        self.assertIn("Edited artifacts/report.md", out)
        text = (Path(ctx.project_dir) / "artifacts" / "report.md").read_text()
        self.assertIn("The result is 0.62", text)
        self.assertNotIn("0.50", text)

    async def test_edit_file_rejects_missing_text(self):
        ctx = await self._make()
        out = await _editor_edit_file(ctx, "artifacts/report.md",
                                      "does-not-exist", "x")
        self.assertIn("0 matches", out)

    async def test_edit_file_blocks_path_escape(self):
        ctx = await self._make()
        out = await _editor_edit_file(ctx, "../../etc/passwd", "root", "x")
        self.assertIn("file not found", out)
        self.assertIsNone(_editor_safe(ctx, "../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
