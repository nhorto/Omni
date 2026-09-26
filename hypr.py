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
    left = int(monitor["x"] + 12)
    top = int(monitor["y"] + 24)
    full_width = int(monitor["width"] - 24)
    full_height = int(monitor["height"] - 48)
    half_width = int((full_width - 12) // 2)
    half_height = int((full_height - 12) // 2)
    if side in {"left", "right"}:
        width, height = half_width, full_height
    elif side in {"top", "bottom"}:
        width, height = full_width, half_height
    else:
        width, height = half_width, half_height
    x = left + (half_width + 12 if side in {"right", "top-right", "bottom-right"} else (full_width - half_width) // 2 if side == "center" else 0)
    y = top + (half_height + 12 if side in {"bottom", "bottom-left", "bottom-right"} else (full_height - half_height) // 2 if side == "center" else 0)
    for expression in (
        f'hl.dsp.window.float({{ action = "enable", window = "address:{address}" }})',
        f'hl.dsp.window.resize({{ x = {width}, y = {height}, window = "address:{address}" }})',
        f'hl.dsp.window.move({{ x = {x}, y = {y}, window = "address:{address}" }})',
    ):
        result = subprocess.run(["hyprctl", "dispatch", expression], capture_output=True, text=True, timeout=10, check=True)
        if "ok" not in result.stdout.lower():
            raise RuntimeError("Hyprland did not accept the window placement")
    placed = window(address)
    if not placed["floating"] or abs(placed["at"][0] - x) > 60 or abs(placed["at"][1] - y) > 60:
        raise RuntimeError("Window placement could not be verified")
    return f"Placed {item['title'] or item['class']} on the {side} side of {monitor['name']}"
