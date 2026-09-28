"""Windows, workspaces, apps, keys, typing, and screen reading on Hyprland."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import hypr, tool

ADDRESS = {"type": "string", "description": "Hyprland window address from windows_list, e.g. 0x55d0c1a2b3c0"}


def _spawn(argv: list[str], cwd: str | None = None) -> None:
    subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def app_dirs() -> list[Path]:
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return [home / "applications", *(Path(d) / "applications" for d in data_dirs)]


def desktop_entries() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for folder in app_dirs():
        if not folder.is_dir():
            continue
        for path in folder.glob("*.desktop"):
            ident = path.stem
            if ident in entries:
                continue  # earlier directories (user) win
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                section = parser["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if section.get("NoDisplay", "false") == "true" or section.get("Hidden", "false") == "true":
                continue
            entries[ident] = {"id": ident, "name": section.get("Name", ident), "comment": section.get("Comment", ""),
                              "wm_class": section.get("StartupWMClass", "")}
    return entries


def find_app(query: str) -> dict:
    entries = desktop_entries()
    q = query.lower().removesuffix(".desktop").strip()
    exact = [e for e in entries.values() if q in (e["id"].lower(), e["name"].lower())]
    if exact:
        return exact[0]
    partial = [e for e in entries.values() if q in e["name"].lower() or q in e["id"].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise ValueError("Several apps match: " + ", ".join(e["name"] for e in partial[:8]) + ". Use the exact name or id.")
    raise ValueError(f"No installed app matches {query!r}; call apps_list")


@tool("List open windows with address, class, title, workspace, and which is focused.")
def windows_list(ctx) -> list[dict]:
    return hypr.windows()


@tool("Focus a window (switches to its workspace).", action=True, address=ADDRESS)
def window_focus(ctx, address: str) -> str:
    item = hypr.window(address)
    hypr.dispatch(f'hl.dsp.focus({{ window = "address:{address}" }})')
    if not hypr.wait_for(lambda: hypr.query("activewindow").get("address") == address):
        raise RuntimeError("Focus could not be verified")
    return f"Focused {item['title'] or item['class']}"


@tool("Move a window to another workspace.", action=True, address=ADDRESS,
      workspace={"type": "integer", "minimum": 1, "maximum": 10})
def window_move(ctx, address: str, workspace: int) -> str:
    item = hypr.window(address)
    number = hypr.workspace(workspace)
    hypr.dispatch(f'hl.dsp.window.move({{ workspace = "{number}", window = "address:{address}" }})')
    return f"Moved {item['title'] or item['class']} to workspace {number}"


@tool("Float a window and tile it to a side, corner, or center of its monitor.", action=True, address=ADDRESS,
      position={"type": "string", "enum": list(hypr.POSITIONS)})
def window_place(ctx, address: str, position: str) -> str:
    return hypr.place(address, position)


@tool("Close a window politely (like Super+W).", action=True, address=ADDRESS)
def window_close(ctx, address: str) -> str:
    item = hypr.window(address)
    hypr.dispatch(f'hl.dsp.window.close({{ window = "address:{address}" }})')
    return f"Closed {item['title'] or item['class']}"


@tool("Switch to a workspace.", action=True, workspace={"type": "integer", "minimum": 1, "maximum": 10})
def workspace_switch(ctx, workspace: int) -> str:
    number = hypr.workspace(workspace)
    hypr.dispatch(f'hl.dsp.focus({{ workspace = "{number}" }})')
    return f"On workspace {number}"


@tool("List installed desktop apps (id and name). Pass a filter to narrow it.",
      filter={"type": "string", "description": "Optional case-insensitive substring"})
def apps_list(ctx, filter: str = "") -> list[dict]:
    q = filter.lower()
    return sorted(({"id": e["id"], "name": e["name"]} for e in desktop_entries().values()
                   if not q or q in e["name"].lower() or q in e["id"].lower()), key=lambda e: e["name"].lower())


@tool("Launch an installed app by name or desktop id, optionally on a workspace and placed on a side. "
      "Returns the new window's address when it appears.", action=True,
      app={"type": "string"}, workspace={"type": "integer", "minimum": 1, "maximum": 10},
      position={"type": "string", "enum": list(hypr.POSITIONS)})
def app_launch(ctx, app: str, workspace: int | None = None, position: str | None = None) -> dict:
    entry = find_app(app)
    if workspace:
        workspace_switch(ctx, workspace)
    current = hypr.windows()
    before = {w["address"] for w in current}
    running = [w for w in current if _is_app(w, entry)]
    launcher = ["uwsm-app", "--", entry["id"] + ".desktop"] if shutil.which("uwsm-app") else ["gtk-launch", entry["id"]]
    _spawn(launcher)
    # A single-instance app just raises its open window, so do not wait long when one is already open.
    created = hypr.new_window(before, 2.0 if running else 6.0)
    result = {"launched": entry["name"], "address": created["address"] if created else None}
    if created:
        if position:
            result["placed"] = hypr.place(created["address"], position)
        return result
    if running := [w for w in hypr.windows() if _is_app(w, entry)]:
        window = running[0]
        result["address"] = window["address"]
        result["note"] = (f"{entry['name']} runs one instance: it reused its window on workspace {window['workspace']}. "
                          "Use window_move to bring that window here; launching again does nothing.")
        return result
    # Slow starters (web apps behind a local service) appear later: finish placing them in the background.
    threading.Thread(target=_finish_launch, args=(before, entry, workspace, position), daemon=True).start()
    result["note"] = (f"{entry['name']} is still starting. Its window will be put on "
                      f"{f'workspace {workspace}' if workspace else 'the current workspace'} when it appears "
                      f"(up to {LATE_WINDOW:.0f} s). Do not launch it again.")
    return result


LATE_WINDOW = 30.0


def _is_app(window: dict, entry: dict) -> bool:
    """Whether a window belongs to a desktop entry. Classes rarely match exactly (T3 Code is com.t3tools.T3Code,
    web apps are chrome-<host>-Default), so compare letters and digits only; titles must match the app's name."""
    def key(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())
    names = {key(name) for name in (entry.get("wm_class"), entry["id"], entry["name"]) if len(key(name)) >= 3}
    title = key(window.get("title", ""))
    return any(name in key(window["class"]) or title.startswith(name) for name in names)


def _finish_launch(before: set[str], entry: dict, workspace: int | None, position: str | None) -> None:
    window = hypr.new_window(before, LATE_WINDOW)
    if not window:
        subprocess.run(["notify-send", "--app-name=Omni", f"{entry['name']} did not open",
                        f"No window appeared within {LATE_WINDOW:.0f} s."], check=False)
        return
    try:
        if workspace and window["workspace"] != workspace:
            window_move(None, window["address"], workspace)
        if position:
            hypr.place(window["address"], position)
    except (RuntimeError, ValueError, subprocess.SubprocessError):
        pass  # the window closed or moved away meanwhile; nothing to report to a finished turn


@tool("Open a file, folder, or URL with its default app (xdg-open).", action=True, target={"type": "string"})
def open_target(ctx, target: str) -> str:
    if not re.match(r"^(https?://|mailto:|file://)", target):
        path = Path(target).expanduser()
        if not path.exists():
            raise ValueError(f"{target} does not exist")
        target = str(path)
    _spawn(["xdg-open", target])
    return f"Opened {target}"


@tool("Send a key chord to a window (default: the focused one), e.g. CTRL+L, SUPER+RETURN, ALT+F4, RETURN.",
      action=True, chord={"type": "string"}, address=ADDRESS)
def keys_send(ctx, chord: str, address: str = "") -> str:
    parts = [p for p in chord.upper().replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("Empty chord")
    mods, key = " ".join(parts[:-1]), parts[-1]
    target = address or hypr.query("activewindow").get("address", "")
    hypr.window(target)
    try:
        hypr.dispatch(f'hl.dsp.send_shortcut({{ mods = "{mods}", key = "{key}", window = "address:{target}" }})')
    except subprocess.CalledProcessError as exc:
        # After type_text, wtype's temporary keymap leaves Hyprland unable to resolve key names
        # ("send_shortcut: key not found"); press the chord through wtype in the focused target instead.
        if "key not found" not in (exc.stdout or "") or not shutil.which("wtype"):
            raise
        hypr.dispatch(f'hl.dsp.focus({{ window = "address:{target}" }})')
        subprocess.run(["wtype", *_wtype_chord(parts)], check=True, timeout=5)
    return f"Sent {chord}"


WTYPE_MODS = {"CTRL": "ctrl", "CONTROL": "ctrl", "SHIFT": "shift", "ALT": "alt", "SUPER": "logo", "META": "logo"}
WTYPE_KEYS = {"RETURN": "Return", "ENTER": "Return", "ESC": "Escape", "ESCAPE": "Escape", "TAB": "Tab", "SPACE": "space",
              "BACKSPACE": "BackSpace", "DELETE": "Delete", "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
              "HOME": "Home", "END": "End", "PAGEUP": "Prior", "PAGEDOWN": "Next"}


def _wtype_chord(parts: list[str]) -> list[str]:
    mods, key = [WTYPE_MODS.get(p, p.lower()) for p in parts[:-1]], parts[-1]
    key = WTYPE_KEYS.get(key, key if len(key) > 1 and key[0] == "F" and key[1:].isdigit() else key.lower())
    return [arg for m in mods for arg in ("-M", m)] + ["-k", key] + [arg for m in reversed(mods) for arg in ("-m", m)]


@tool("Type text into the focused window as if from the keyboard. Focus the right window first.",
      action=True, text={"type": "string"}, submit={"type": "boolean", "description": "Press Enter afterwards"})
def type_text(ctx, text: str, submit: bool = False) -> str:
    if len(text) > 5000:
        raise ValueError("Type at most 5000 characters at once")
    if not shutil.which("wtype"):
        raise RuntimeError("wtype is not installed; use desktop_fill for accessible fields")
    subprocess.run(["wtype", "--", text], check=True, timeout=30)
    if submit:
        subprocess.run(["wtype", "-k", "Return"], check=True, timeout=5)
    return "Typed" + (" and submitted" if submit else "")


@tool("Read the text currently visible on screen (OCR of all monitors).")
def screen_read(ctx) -> str:
    if not shutil.which("grim") or not shutil.which("tesseract"):
        raise RuntimeError("Screen reading needs grim and tesseract")
    image = subprocess.run(["grim", "-"], capture_output=True, timeout=15, check=True).stdout
    result = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng"], input=image, capture_output=True, timeout=30, check=True)
    return result.stdout.decode("utf-8", "replace").strip()[:12000] or "No readable text on screen"


@tool("List buttons and text fields an app exposes through accessibility (AT-SPI).", app={"type": "string"})
def desktop_controls(ctx, app: str) -> str:
    from . import atspi
    return atspi.read(app)


@tool("Click an accessible button or menu item by its exact label.", action=True,
      app={"type": "string"}, label={"type": "string"})
def desktop_click(ctx, app: str, label: str) -> str:
    from . import atspi
    return atspi.click(app, label)


@tool("Set the text of an accessible field by its exact label.", action=True,
      app={"type": "string"}, label={"type": "string"}, text={"type": "string"})
def desktop_fill(ctx, app: str, label: str, text: str) -> str:
    from . import atspi
    return atspi.fill(app, label, text)


@tool("Show a desktop notification.", title={"type": "string"}, body={"type": "string"})
def notify(ctx, title: str, body: str = "") -> str:
    subprocess.run(["notify-send", "--app-name=Omni", "--", title, body], check=True, timeout=5)
    return "Notified"
