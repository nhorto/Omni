"""Bounded Hyprland workspace and window actions."""

from __future__ import annotations

import json
import re
import subprocess
import time


def _json(*args: str):
    result = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def windows() -> list[dict]:
    return [{"address": item["address"], "class": item.get("class", ""), "title": item.get("title", "")[:100], "workspace": item.get("workspace", {}).get("id"), "monitor": item.get("monitor"), "at": item.get("at"), "size": item.get("size"), "floating": item.get("floating")} for item in _json("clients") if item.get("mapped")]


def available_windows() -> list[dict]:
    try:
        return windows()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def workspace_number(value: str) -> int:
    if not re.fullmatch(r"(?:[1-9]|10)", value.strip()):
        raise ValueError("Workspace must be a number from 1 to 10")
    return int(value.strip())


def window(address: str) -> dict:
    if not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise ValueError("Window target must be an exact Hyprland address")
    matches = [item for item in windows() if item["address"] == address]
    if len(matches) != 1:
        raise ValueError("Window is no longer present")
    return matches[0]


def switch_workspace(value: str) -> str:
    number = workspace_number(value)
    result = subprocess.run(["hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{number}" }})'], capture_output=True, text=True, timeout=10, check=True)
    if "ok" not in result.stdout.lower():
        raise RuntimeError("Hyprland did not accept the workspace change")
    if _json("activeworkspace").get("id") != number:
        raise RuntimeError("Workspace change could not be verified")
    return f"Switched to workspace {number}"


def move_window(address: str, value: str) -> str:
    item = window(address)
    number = workspace_number(value)
    result = subprocess.run(["hyprctl", "dispatch", f'hl.dsp.window.move({{ workspace = "{number}", window = "address:{address}" }})'], capture_output=True, text=True, timeout=10, check=True)
    if "ok" not in result.stdout.lower():
        raise RuntimeError("Hyprland did not accept the window move")
    moved = window(address)
    if moved["workspace"] != number:
        raise RuntimeError("Window move could not be verified")
    return f"Moved {item['title'] or item['class']} to workspace {number}"


def focus_window(address: str) -> str:
    item = window(address)
    if item["workspace"] is None or item["workspace"] < 1:
        raise ValueError("Window is not on a regular workspace")
    result = subprocess.run(["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{address}" }})'], capture_output=True, text=True, timeout=10, check=True)
    if "ok" not in result.stdout.lower():
        raise RuntimeError("Hyprland did not accept the window focus")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _json("activewindow").get("address") == address:
            return f"Focused {item['title'] or item['class']}"
        time.sleep(0.05)
    raise RuntimeError("Window focus could not be verified")


def detect_new_window(before: set[str], timeout: float = 4.0, expected_class: str | None = None) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = [item["address"] for item in available_windows() if item["address"] not in before and (expected_class is None or item["class"] == expected_class)]
        if len(new) == 1:
            return new[0]
        if len(new) > 1:
            return None
        time.sleep(0.15)
    return None


def placement_geometry(monitor: dict, side: str) -> tuple[int, int, int, int]:
    """Compute logical coordinates, respecting display scale, rotation, and panels."""
    physical_width, physical_height = monitor["width"], monitor["height"]
    if int(monitor.get("transform", 0)) % 2:
        physical_width, physical_height = physical_height, physical_width
    scale = float(monitor.get("scale", 1))
    if scale <= 0:
        raise ValueError("Invalid monitor scale")
    # hyprctl JSON order: reserved top-left (x,y), then bottom-right (x,y).
    reserve_left, reserve_top, reserve_right, reserve_bottom = monitor.get("reserved", [0, 0, 0, 0])
    gap = 12
    left = int(monitor["x"] + reserve_left + gap)
    top = int(monitor["y"] + reserve_top + gap)
    full_width = int(physical_width / scale - reserve_left - reserve_right - gap * 2)
    full_height = int(physical_height / scale - reserve_top - reserve_bottom - gap * 2)
    half_width, half_height = (full_width - gap) // 2, (full_height - gap) // 2
    if min(half_width, half_height) <= 0:
        raise ValueError("Monitor has no usable placement area")
    width = full_width if side in {"top", "bottom"} else half_width
    height = full_height if side in {"left", "right"} else half_height
    x = left + (half_width + gap if side in {"right", "top-right", "bottom-right"} else (full_width - half_width) // 2 if side == "center" else 0)
    y = top + (half_height + gap if side in {"bottom", "bottom-left", "bottom-right"} else (full_height - half_height) // 2 if side == "center" else 0)
    return x, y, width, height


def place_window(address: str, side: str) -> str:
    item = window(address)
    positions = {"left", "right", "top", "bottom", "center", "top-left", "top-right", "bottom-left", "bottom-right"}
    if side not in positions:
        raise ValueError("Unsupported window position")
    monitors = _json("monitors")
    matches = [monitor for monitor in monitors if monitor.get("id") == item["monitor"]]
    if len(matches) != 1:
        raise RuntimeError("Could not identify the window's monitor")
    monitor = matches[0]
    x, y, width, height = placement_geometry(monitor, side)
    for expression in (
        f'hl.dsp.window.float({{ action = "enable", window = "address:{address}" }})',
        f'hl.dsp.window.resize({{ x = {width}, y = {height}, window = "address:{address}" }})',
        f'hl.dsp.window.move({{ x = {x}, y = {y}, window = "address:{address}" }})',
    ):
        result = subprocess.run(["hyprctl", "dispatch", expression], capture_output=True, text=True, timeout=10, check=True)
        if "ok" not in result.stdout.lower():
            raise RuntimeError("Hyprland did not accept the window placement")
    deadline = time.monotonic() + 2
    while True:
        placed = window(address)
        actual = [*placed["at"], *placed["size"]]
        if placed["floating"] and all(abs(a - b) <= 12 for a, b in zip(actual, (x, y, width, height))):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Window placement or size could not be verified; the app may enforce a minimum size")
        time.sleep(0.05)
    return f"Placed {item['title'] or item['class']} on the {side} side of {monitor['name']}"
