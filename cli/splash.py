"""Animated fox splash screen.

Renders a blinking fox face with a waving tail in a rounded panel, followed by
the styled product wordmark — an opencode-style opening animation.
"""

from __future__ import annotations

import sys
import time
from typing import Iterable

from .ui import ACCENT, AMBER, DIM, FADED, TEXT, bold, c, dim, italic, panel

# Fox face — ``{e}`` is the eyes slot animated across frames, ``{s}`` the tail sway.
_FOX = r"""
          /\              /\
         /  \    _-_     /  \
        /_,-' \ ( {e} ) /'-,_\
        /   _,-'-'---'-'-,_   \
       /  ,-'                '-.\
      /,-'   ___     ___        '-.
     |,-'   ( _ )   ( _ )          '\
     |   \__/,-' \__/,-'  \_        |
     |    \__/    \__/      '        |
      \          {s}               /
       '.___   ___   ____._____    ,'
           '--'---'--'--'  |  '--'
                            '
"""

# Tail sways as part of the ground line; ``{s}`` picks a few variants.
_TAILS = ["|", "╲", "╲", "╱", "╱", "|", "╱", "╱", "╲", "╲"]
_EYES = ["o o", "- -", "o o", "O O", "o o", ". .", "o o", "- -", "O o", "o O"]


def fox_frame(eye: str, tail: str) -> str:
    return _FOX.replace("{e}", eye).replace("{s}", tail).rstrip()


def fox_logo(style: str = "small") -> str:
    """Product wordmark, opencode-style."""
    if style == "small":
        return bold("fox", ACCENT) + c(" — research workbench", DIM)
    big = r"""  _____         _
 |  _  |___ ___| |_
 | | | | -_| . | '_|
 |_____|___|___|_,_|"""
    return big


def animate(steps: int = 6, delay: float = 0.18, stream=None) -> None:
    """Cycle the fox through eye/tail frames in place."""
    stream = stream or sys.stdout
    n = len(_EYES)
    for i in range(max(1, steps)):
        eye = _EYES[i % n]
        tail = _TAILS[i % n]
        frame = fox_frame(eye, tail)
        lines = frame.split("\n")
        styled = c(lines[0], FADED)
        for ln in lines[1:]:
            styled += "\n" + c(ln, ACCENT)
        rendered = "\n".join(dim(l) for l in [styled])  # keep color, dim-ish
        # Simpler: re-color each line so it stays legible
        body = c(lines[0], FADED) + "\n" + "\n".join(c(l, AMBER) for l in lines[1:])
        # clear previous block height
        height = len(lines)
        stream.write("\033[F" * height)
        stream.write(body + "\n")
        stream.flush()
        time.sleep(delay)


def splash(steps: int = 6, title: str = "fox", tagline: str | None = None,
           stream=None) -> str:
    """Show the full splash: animated fox + wordmark panel. Returns it as text."""
    stream = stream or sys.stdout
    out = c(_FOX.replace("{e}", "o o").replace("{s}", "|").rstrip(), ACCENT)
    lines = out.split("\n")
    fox_body = "\n".join(c(l, AMBER if i else FADED) for i, l in enumerate(lines))
    wordmark = bold(title.upper(), ACCENT)
    sub = italic(tagline) if tagline else dim("local experiment workbench")
    header = f"{wordmark}   {sub}"
    if stream:
        stream.write("\n")
        stream.write(fox_body + "\n")
        stream.write("\n")
        stream.write(header + "\n")
        stream.flush()
        if steps and steps > 0:
            animate(steps=steps, stream=stream)
        stream.write("\n")
        stream.flush()
    return fox_body


def animated_splash(stream=None, version: str = "") -> None:
    """opencode-style opening: animated fox, then a status panel."""
    stream = stream or sys.stdout
    lines = fox_frame("o o", "|").split("\n")
    for i, ln in enumerate(lines):
        stream.write(c(ln, FADED if i == 0 else AMBER) + "\n")
    stream.write("\n")
    stream.write(bold(" FOX", ACCENT) + c("  AI Research Workbench", TEXT)
                 + (c(f"  v{version}", DIM) if version else "") + "\n")
    stream.write(dim("  local-first · experiments · knowledge graphs\n"))
    stream.flush()
    time.sleep(0.25)
    animate(steps=6, stream=stream)
    stream.write("\n")
    stream.flush()


def render_splash_panel(version: str = "") -> str:
    """Static splash panel (no animation) for help/doc contexts."""
    fox = "\n".join(c(l, AMBER if i else FADED)
                    for i, l in enumerate(fox_frame("o o", "|").split("\n")))
    body = (fox + "\n\n"
            + bold("fox", ACCENT) + c(" — AI Research Workbench", TEXT)
            + (c(f"  v{version}", DIM) if version else "") + "\n"
            + dim("local-first · experiments · knowledge graphs"))
    return panel("fox", body, title_rgb=ACCENT)
