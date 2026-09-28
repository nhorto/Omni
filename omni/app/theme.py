"""Map the current Omarchy theme (colors.toml + font) onto libadwaita's named colors, live.

Omarchy writes the active theme to ~/.local/state/omarchy/current/theme/colors.toml
(~/.config/omarchy/current/theme on older installs) with tokens like accent,
background, foreground, and mode = "dark" | "light".
We translate them into @define-color overrides so every stock libadwaita widget
picks them up, and re-apply when the theme changes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, Gtk


def _theme_dir() -> Path:
    if override := os.environ.get("OMNI_THEME_DIR"):
        return Path(override)
    state, legacy = Path.home() / ".local/state/omarchy/current/theme", Path.home() / ".config/omarchy/current/theme"
    return legacy if legacy.is_dir() and not state.is_dir() else state


THEME_DIR = _theme_dir()

APP_CSS = """
.omni-bubble { padding: 10px 14px; border-radius: 14px; }
.omni-user { background: alpha(@accent_bg_color, 0.18); }
.omni-reply { background: @card_bg_color; }
.omni-step { font-size: 0.9em; opacity: 0.7; }
.omni-state-dot { min-width: 10px; min-height: 10px; border-radius: 5px; background: alpha(@window_fg_color, 0.3); }
.omni-state-dot.listening { background: @error_color; }
.omni-state-dot.thinking, .omni-state-dot.working { background: @accent_color; }
.omni-state-dot.speaking { background: @success_color; }
.omni-state-dot.awaiting_approval { background: @warning_color; }
.omni-quick { border-radius: 16px; }
.omni-quick entry { min-height: 44px; font-size: 1.15em; }
.omni-quick .omni-answer { font-size: 1.05em; }
.omni-approval { border: 1px solid alpha(@warning_color, 0.6); border-radius: 12px; padding: 12px; }
"""


def mix(a: str, b: str, amount: float) -> str:
    """Blend two #rrggbb colors; amount 0 → a, 1 → b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(ca, cb))


def load_colors(theme_dir: Path = THEME_DIR) -> dict | None:
    path = theme_dir / "colors.toml"
    try:
        colors = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return colors if colors.get("background") and colors.get("foreground") else None


def css_for(colors: dict) -> str:
    bg, fg = colors["background"], colors["foreground"]
    accent = colors.get("accent") or colors.get("blue") or fg
    light = colors.get("mode") == "light"
    view = colors.get("dark_background") if not light else colors.get("lighter_background", bg)
    card = colors.get("lighter_background") or mix(bg, fg, 0.06)
    sidebar = colors.get("dark_background") or mix(bg, "#000000", 0.15)
    headerbar = bg
    strong_fg = colors.get("bright_foreground") or fg
    values = {
        "accent_color": accent, "accent_bg_color": accent, "accent_fg_color": bg,
        "window_bg_color": bg, "window_fg_color": strong_fg,
        "view_bg_color": view or bg, "view_fg_color": strong_fg,
        "headerbar_bg_color": headerbar, "headerbar_fg_color": strong_fg, "headerbar_backdrop_color": bg,
        "sidebar_bg_color": sidebar, "sidebar_fg_color": strong_fg, "sidebar_backdrop_color": sidebar,
        "card_bg_color": card, "card_fg_color": strong_fg,
        "popover_bg_color": card, "popover_fg_color": strong_fg,
        "dialog_bg_color": card, "dialog_fg_color": strong_fg,
        "destructive_bg_color": colors.get("red", "#e01b24"), "destructive_color": colors.get("red", "#e01b24"),
        "error_color": colors.get("red", "#e01b24"), "success_color": colors.get("green", "#2ec27e"),
        "warning_color": colors.get("yellow", "#e5a50a"),
    }
    lines = [f"@define-color {name} {value};" for name, value in values.items()]
    # libadwaita ≥ 1.6 also reads CSS variables; set both so older and newer versions agree.
    variables = "".join(f"--{name.replace('_', '-')}: {value};" for name, value in values.items())
    return "\n".join(lines) + f"\n:root {{ {variables} }}\n"


def current_font() -> str | None:
    if not shutil.which("omarchy-font-current"):
        return None
    try:
        return subprocess.run(["omarchy-font-current"], capture_output=True, text=True, timeout=2).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


class ThemeWatcher:
    """Applies the theme now and whenever Omarchy switches themes."""

    def __init__(self):
        display = Gdk.Display.get_default()
        self.theme = Gtk.CssProvider()
        self.app = Gtk.CssProvider()
        self.app.load_from_string(APP_CSS)
        Gtk.StyleContext.add_provider_for_display(display, self.theme, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(display, self.app, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.apply()
        # The theme folder is a symlink Omarchy swaps; watch its parent.
        self.monitor = Gio.File.new_for_path(str(THEME_DIR.parent)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", lambda *args: self.apply())

    def apply(self) -> None:
        colors = load_colors()
        style = Adw.StyleManager.get_default()
        if not colors:
            self.theme.load_from_string("")
            style.set_color_scheme(Adw.ColorScheme.DEFAULT)
            return
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if colors.get("mode") == "light" else Adw.ColorScheme.FORCE_DARK)
        css = css_for(colors)
        if font := current_font():
            css += f"\nwindow, popover {{ font-family: \"{font}\"; }}\n"
        self.theme.load_from_string(css)
