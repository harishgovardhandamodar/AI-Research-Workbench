"""View rendering for the Fox TUI.

All renderers are pure: they take (state, theme, capabilities, width/height)
and return lines/strings. The app assembles the frame from these and writes it
once per tick, so they are also usable for `--render-preview`.
"""

from __future__ import annotations

from .components import (Header, List, Panel, StatusBar, TabBar, _pad,
                         _truncate, visible_width)
from .state import State
from .theme import Capabilities, Theme

# ------------------------------------------------------------------ chrome --

_VIEW_TABS = ["output", "logs", "settings"]
_SETTINGS_ROWS = [
    ("theme", "current theme (Ctrl+T to change)"),
    ("sidebar", "toggle sidebar (Ctrl+B)"),
    ("mouse", "mouse support"),
    ("reduce_motion", "disable spinner animation"),
]

# Shared sidebar definition: (label, action) — "view:X" switches view,
# "run:X" executes a CLI command, None is a separator.
SIDEBAR = [
    ("Output", "view:output"),
    ("Logs", "view:logs"),
    ("Settings", "view:settings"),
    (None, None),
    ("status", "run:status"),
    ("projects", "run:projects"),
    ("graph", "run:graph"),
    ("scheduler", "run:scheduler"),
    ("manual", "run:manual"),
]


def sidebar_width(w: int) -> int:
    return min(max(w // 4, 14), 30)


def render_header(state: State, theme: Theme, cap: Capabilities,
                  w: int) -> str:
    hints = [("Tab", "focus"), ("?", "help"), ("Ctrl+T", "theme")]
    mode = f"[{state.view}]"
    if state.running:
        mode = f"[running: {_truncate(state.running_label, 24)}]"
    return Header(theme, cap).render("fox", mode, hints, w)


def render_status(state: State, theme: Theme, cap: Capabilities, w: int,
                  t: float = 0.0) -> str:
    left = f"{state.view} · {state.status_msg}"
    if state.running:
        left = f"running {state.running_label}"
    right = f"{theme.name} · Ctrl+Q quit"
    return StatusBar(theme, cap, reduce_motion=False).render(
        left, right, running=state.running, t=t, width=w)


def render_input(state: State, theme: Theme, cap: Capabilities,
                 w: int) -> str:
    t, c = theme, cap
    prompt = t.paint("accent", "fox", c, bold=True) + t.paint("textMuted",
                                                              " >", c)
    prompt_w = visible_width(prompt)
    if state.running:
        body = t.paint("textSubtle",
                       "running — Ctrl+C to interrupt", c, dim=True)
        row = prompt + " " + _pad(body, max(w - prompt_w - 1, 0))
        return t.bg("inputBackground", c) + row + t.reset()
    text = state.buf.text
    avail = max(w - prompt_w - 2, 1)
    if len(text) > avail:
        text = text[len(text) - avail:]
    vis = t.paint("inputForeground", text, c)
    row = prompt + " " + vis
    row = t.bg("inputBackground", c) + _pad(row, w) + t.reset()
    # cursor handled by the app via absolute positioning
    return row


# ---------------------------------------------------------------- sidebar --

def _sidebar_items(state: State) -> list[tuple[str, str, str]]:
    items = []
    for i, (label, action) in enumerate(SIDEBAR):
        if label is None:
            items.append(("—", "", "sep"))
        else:
            items.append((label, action, f"idx:{i}"))
    return items


def render_sidebar(state: State, theme: Theme, cap: Capabilities,
                   w: int, h: int) -> list[str]:
    t, c = theme, cap
    items = _sidebar_items(state)
    width = max(w, 12)
    border = t.fg("borderMuted", c)
    out: list[str] = []
    out.append(border + "┌" + "─" * (width - 2) + "┐" + t.reset())
    out.append(border + "│" + t.paint("textSubtle", "  NAVIGATE", c, dim=True)
               + " " * max(width - 13, 0) + "│" + t.reset())
    sel = state.sidebar_idx
    for i, (label, _secondary, action) in enumerate(items):
        if action == "sep":
            out.append(border + "│" + t.paint("textSubtle",
                                              "─" * (width - 2), c) + "│"
                       + t.reset())
            continue
        selected = (state.focus == "sidebar" and i == sel)
        view_active = action.startswith("view:") and state.view == action[5:]
        if selected:
            row = t.paint_bg("sidebarSelectedForeground",
                             "sidebarSelectedBackground",
                             f"  {label}", c, bold=True)
        elif view_active:
            row = t.paint("sidebarForeground", f"  {label}", c, bold=True)
        else:
            row = t.paint("sidebarForeground", f"  {label}", c)
        out.append(border + "│" + _pad(row, width - 2) + "│" + t.reset())
    out.append(border + "└" + "─" * (width - 2) + "┘" + t.reset())
    return out


# ------------------------------------------------------------------ content --

def render_main(state: State, theme: Theme, cap: Capabilities, w: int,
                h: int) -> list[str]:
    if state.view == "logs":
        return _render_logs(state, theme, cap, w, h)
    if state.view == "settings":
        return _render_settings(state, theme, cap, w, h)
    return _render_output(state, theme, cap, w, h)


def _render_output(state: State, theme: Theme, cap: Capabilities,
                   w: int, h: int) -> list[str]:
    t, c = theme, cap
    panel = Panel(theme, cap)
    if not state.lines:
        return panel.render("fox", [t.paint("textMuted",
                                            "type a command and press Enter",
                                            c, dim=True)], w)
    total = len(state.lines)
    end = total - state.scroll
    start = max(0, end - (h - 2))
    chunk = state.lines[start:end]
    body = [_truncate(ln, w - 2) for ln in chunk]
    return panel.render("output", body, w)


def _render_logs(state: State, theme: Theme, cap: Capabilities,
                 w: int, h: int) -> list[str]:
    t, c = theme, cap
    panel = Panel(theme, cap)
    if not state.logs:
        return panel.render("logs", [t.paint("textMuted",
                                             "no commands run yet", c, dim=True)], w)
    rows = []
    for ts, cmd, code in reversed(state.logs[-40:]):
        mark = (t.paint("success", "✓", c) if code in (None, 0)
                else t.paint("error", f"✗ {code}", c))
        rows.append(f"{mark} {t.paint('textSubtle', ts, c, dim=True)}  "
                    f"{t.paint('text', cmd, c)}")
    return panel.render("logs", rows, w)


def _render_settings(state: State, theme: Theme, cap: Capabilities,
                     w: int, h: int) -> list[str]:
    t, c = theme, cap
    panel = Panel(theme, cap)
    body = []
    for i, (key, desc) in enumerate(_SETTINGS_ROWS):
        if key == "theme":
            value = theme.name
        elif key == "sidebar":
            value = "on" if state.sidebar_visible else "off"
        else:
            value = "on"  # mouse / reduce_motion from config shown as-is
        label = t.paint("text", f"  {key}", c)
        val = t.paint("accent", value, c)
        body.append(_pad(label + "  " + val, w - 2))
        body.append(t.paint("textSubtle", f"    {desc}", c, dim=True))
        body.append("")
    return panel.render("settings", body, w)


# ---------------------------------------------------------------- overlays --

def _modal(theme: Theme, cap: Capabilities, title: str, lines: list[str],
           w: int, h: int) -> list[str]:
    t, c = theme, cap
    body_w = min(max(len(lines) and max(visible_width(x) for x in lines),
                     0) + 4, w - 6)
    body_h = min(len(lines) + 2, h - 6)
    top = max(2, (h - body_h) // 2)
    left = max(1, (w - body_w) // 2)
    ch = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
    border = t.fg("borderActive", c)
    title_label = t.paint("accent", f" {title} ", c, bold=True)
    rows: list[str] = [""] * h
    header_line = (border + ch["tl"] + title_label
                   + border + ch["h"] * max(body_w - visible_width(title) - 2,
                                            0) + ch["tr"] + t.reset())
    rows[top] = header_line
    for i, ln in enumerate(lines[: body_h - 2], start=1):
        row = (border + ch["v"] + _pad(ln, body_w) + ch["v"] + t.reset())
        rows[top + i] = row
    rows[top + body_h - 1] = (border + ch["bl"] + ch["h"] * body_w + ch["br"]
                              + t.reset())
    # blank the rest of the modal column region
    return rows


def render_help(theme: Theme, cap: Capabilities, w: int, h: int) -> list[str]:
    lines = [
        "  Global",
        "    Ctrl+C / Ctrl+Q / Ctrl+D   quit",
        "    ?                          this help",
        "    Esc                        close overlay",
        "    Tab                        cycle focus (sidebar / input)",
        "    Ctrl+P                     command palette",
        "    Ctrl+T                     theme picker",
        "    Ctrl+B                     toggle sidebar",
        "    Ctrl+L                     clear output",
        "  Navigation",
        "    PgUp / PgDn / Home / End   scroll content",
        "    Up / Down                  sidebar select · input history",
        "  Input",
        "    Enter                      run command",
        "    Tab                        complete",
        "    Ctrl+U                     clear line",
        "    Ctrl+W                     delete word",
        "",
        "  press Esc to close",
    ]
    return _modal(theme, cap, "help", lines, w, h)


def render_theme_picker(state: State, theme: Theme, cap: Capabilities,
                        themes: list, current_name: str, w: int,
                        h: int) -> list[str]:
    t, c = theme, cap
    lines = []
    for i, th in enumerate(themes):
        sel = "❯" if i == state.overlay_idx else " "
        active = "  (active)" if th.name == current_name else ""
        name = t.paint("accent", th.name, c, bold=active) if active \
            else t.paint("text", th.name, c)
        swatches = "".join(th.swatch(tok, c) for tok in
                           ("accent", "success", "warning", "error"))
        row = (t.paint("text", f"{sel} ", c) + name + active
               + "  " + swatches)
        lines.append(row)
    lines.append("")
    lines.append("  Enter apply · Esc cancel")
    return _modal(theme, cap, "theme picker", lines, w, h)


def render_palette(state: State, theme: Theme, cap: Capabilities,
                   commands: list[str], w: int, h: int) -> list[str]:
    t, c = theme, cap
    q = state.overlay_query
    results = [cmd for cmd in commands
               if all(ch in cmd for ch in q.lower())] or commands[:12]
    lines = [t.paint("textMuted", f"  > {q}▌", c)]
    for i, cmd in enumerate(results[:10]):
        marker = "❯" if i == state.overlay_idx else " "
        lines.append(t.paint("text", f"  {marker} {cmd}", c))
    if len(results) > 10:
        lines.append(t.paint("textSubtle", f"  … {len(results) - 10} more", c))
    lines.append("")
    lines.append("  Enter run · Ctrl+P switch view · Esc cancel")
    return _modal(theme, cap, "command palette", lines, w, h)


# ------------------------------------------------------------------- frame --

def render_frame(state: State, theme: Theme, cap: Capabilities, w: int,
                 h: int, t: float = 0.0) -> str:
    """Assemble the complete terminal frame as one string."""
    header = render_header(state, theme, cap, w)
    content_h = max(h - 3, 1)
    if state.overlay:
        rows = render_main(state, theme, cap, w, content_h)
        overlay = _modal_overlay(state, theme, cap, w, h)
        body = []
        for i in range(content_h):
            base = rows[i] if i < len(rows) else ""
            ov = overlay[i + 1] if i + 1 < len(overlay) else ""
            body.append(ov if ov else base)
        body = [_pad(r, w) for r in body]
    elif state.sidebar_visible:
        sidebar_w = sidebar_width(w)
        sidebar = render_sidebar(state, theme, cap, sidebar_w, content_h)
        main = render_main(state, theme, cap, max(w - sidebar_w, 10),
                           content_h)
        body = []
        for i in range(content_h):
            left = sidebar[i] if i < len(sidebar) else ""
            right = main[i] if i < len(main) else ""
            body.append(_pad(left, sidebar_w) + _pad(right, w - sidebar_w))
    else:
        body = [_pad(r, w) for r in render_main(state, theme, cap, w, content_h)]
    while len(body) < content_h:
        body.append("")
    input_row = render_input(state, theme, cap, w)
    status = render_status(state, theme, cap, w, t=t)
    lines = [header] + body[:content_h] + [input_row, status]
    # Return the full-height row list; the app positions each row with an
    # absolute cursor escape so raw-mode output (LF without CR) stays aligned.
    return [_pad(r, w) for r in lines]


def _modal_overlay(state: State, theme: Theme, cap: Capabilities,
                   w: int, h: int) -> list[str]:
    if state.overlay == "help":
        return render_help(theme, cap, w, h)
    if state.overlay == "theme":
        from .themes import builtin_names, load_theme

        themes = [load_theme(n) for n in builtin_names()]
        return render_theme_picker(state, theme, cap, themes, theme.name,
                                   w, h)
    if state.overlay == "palette":
        from .app import PALETTE_ACTIONS

        return render_palette(state, theme, cap, PALETTE_ACTIONS, w, h)
    return [""] * h
