"""Terminal theme engine.

Presentation is fully separated from agent logic: the runtime only emits
events; themes decide how they look. ``NO_COLOR`` (and non-TTY output)
collapses every theme to plain text, and 8-colour terminals degrade
gracefully because Classic uses only named ANSI colours.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Theme:
    name: str
    description: str
    styles: dict[str, str] = field(default_factory=dict)

    def style(self, role: str) -> str:
        return self.styles.get(role, "")

    @property
    def banner(self) -> str:
        return self.styles.get("banner", "bold")


OBSIDIAN = Theme(
    name="obsidian",
    description="Default Daemon theme: obsidian black with violet and cyan.",
    styles={
        "accent": "#a78bfa",           # violet
        "accent2": "#22d3ee",           # cyan
        "text": "",
        "muted": "grey58",
        "dim": "grey42",
        "error": "#f87171",
        "warn": "#fbbf24",
        "success": "#34d399",
        "border": "#5b21b6",
        "header": "bold #a78bfa",
        "tool": "#22d3ee",
        "tool_ok": "#34d399",
        "tool_err": "#f87171",
        "path": "#93c5fd",
        "banner": "bold #a78bfa on default",
        "prompt": "bold #22d3ee",
        "usage": "grey58",
        "thinking": "grey42 italic",
        "add": "#34d399",
        "del": "#f87171",
        "hunk": "#22d3ee",
    },
)

ARCHIVE = Theme(
    name="archive",
    description="Research theme: restrained slate with amber and verdigris.",
    styles={
        "accent": "#c9a227",           # archive amber
        "accent2": "#7f9e8f",          # verdigris
        "text": "",
        "muted": "grey62",
        "dim": "grey46",
        "error": "#c0392b",
        "warn": "#b8860b",
        "success": "#6b8e23",
        "border": "grey42",
        "header": "bold #c9a227",
        "tool": "#7f9e8f",
        "tool_ok": "#6b8e23",
        "tool_err": "#c0392b",
        "path": "#8ba7a0",
        "banner": "bold #c9a227",
        "prompt": "bold #c9a227",
        "usage": "grey62",
        "thinking": "grey46 italic",
        "add": "#6b8e23",
        "del": "#c0392b",
        "hunk": "#7f9e8f",
    },
)

MINIMAL = Theme(
    name="minimal",
    description="Minimal monochrome presentation.",
    styles={
        "accent": "bold",
        "accent2": "",
        "text": "",
        "muted": "grey62",
        "dim": "grey50",
        "error": "bold",
        "warn": "bold",
        "success": "",
        "border": "grey50",
        "header": "bold",
        "tool": "",
        "tool_ok": "",
        "tool_err": "bold",
        "path": "",
        "banner": "bold",
        "prompt": "bold",
        "usage": "grey62",
        "thinking": "grey50 italic",
        "add": "",
        "del": "",
        "hunk": "bold",
    },
)

CLASSIC = Theme(
    name="classic",
    description="Conventional terminal colours (8-colour safe).",
    styles={
        "accent": "magenta",
        "accent2": "cyan",
        "text": "",
        "muted": "dark_grey",
        "dim": "dark_grey",
        "error": "red",
        "warn": "yellow",
        "success": "green",
        "border": "blue",
        "header": "bold cyan",
        "tool": "cyan",
        "tool_ok": "green",
        "tool_err": "red",
        "path": "blue",
        "banner": "bold magenta",
        "prompt": "bold cyan",
        "usage": "dark_grey",
        "thinking": "italic",
        "add": "green",
        "del": "red",
        "hunk": "cyan",
    },
)

_THEMES: dict[str, Theme] = {
    theme.name: theme for theme in (OBSIDIAN, ARCHIVE, MINIMAL, CLASSIC)
}


def themes() -> list[Theme]:
    return list(_THEMES.values())


def get_theme(name: str) -> Theme:
    return _THEMES.get(name.strip().lower(), OBSIDIAN)


def resolve_theme(name: str) -> Theme:
    """Theme by name, honouring NO_COLOR / non-TTY by degrading to Minimal."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        forced = os.environ.get("DAEMON_FORCE_COLOR")
        if not forced:
            return MINIMAL
    return get_theme(name)


def supports_unicode() -> bool:
    if os.environ.get("DAEMON_ASCII"):
        return False
    encoding = (sys.stdout.encoding or "").lower()
    return "utf" in encoding
