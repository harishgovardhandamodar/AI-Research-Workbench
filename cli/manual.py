"""Accessor for the embedded user manual (`manual.md`)."""

from __future__ import annotations

from pathlib import Path

_MD = Path(__file__).resolve().parent / "manual.md"

_SECTION_HEADERS = {
    "quickstart": "## Quick start",
    "status": "### `fox status`",
    "projects": "### `fox projects`",
    "research": "### `fox research`",
    "graph": "### `fox graph`",
    "manual": "## Subcommands",
}


class Manual:
    def __init__(self, path: Path = _MD) -> None:
        self.path = path
        self._text: str | None = None

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = self.path.read_text()
        return self._text

    def section(self, topic: str) -> str:
        from . import ui

        header = _SECTION_HEADERS.get(topic)
        if header is None:
            known = ", ".join(sorted(_SECTION_HEADERS))
            raise SystemExit(
                f"{ui.err()} {ui.dim('unknown manual section')} — "
                f"try: {known}")
        lines = self.text.splitlines()
        out, on = [], False
        for ln in lines:
            if ln.strip().startswith(header):
                on = True
            elif on and ln.strip().startswith("## "):
                break
            if on:
                out.append(ln)
        if not out:
            raise SystemExit(f"{ui.err()} {ui.dim('section not found')}")
        return "\n".join(out) + f"\n\n{ui.dim('see `fox manual` for the full manual')}"


MANUAL = Manual()
