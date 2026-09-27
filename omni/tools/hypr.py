"""Hyprland queries and dispatches (Hyprland 0.52+ Lua dispatcher syntax, as Omarchy ships)."""

from __future__ import annotations

import json
import re
import subprocess
import time

POSITIONS = ("left", "right", "top", "bottom", "center", "top-left", "top-right", "bottom-left", "bottom-right")


def query(*args: str):
    result = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(result.stdout)


def dispatch(expression: str) -> None:
    result = subprocess.run(["hyprctl", "dispatch", expression], capture_output=True, text=True, timeout=5, check=True)
    if "ok" not in result.stdout.lower():
        raise RuntimeError(f"Hyprland rejected {expression}: {result.stdout.strip()[:200]}")


def windows() -> list[dict]:
    active = query("activewindow").get("address")
    return [{"address": w["address"], "class": w.get("class", ""), "title": w.get("title", "")[:100],
             "workspace": w.get("workspace", {}).get("id"), "monitor": w.get("monitor"), "at": w.get("at"),
             "size": w.get("size"), "floating": w.get("floating"), "focused": w["address"] == active}
            for w in query("clients") if w.get("mapped")]


def window(address: str) -> dict:
    if not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise ValueError("Window target must be a Hyprland address like 0x55d0c1a2b3c0; call windows_list first")
    for item in windows():
        if item["address"] == address:
            return item
    raise ValueError("That window is gone; call windows_list again")


def workspace(value: int | str) -> int:
    number = int(str(value).strip())
    if not 1 <= number <= 10:
        raise ValueError("Workspace must be 1–10")
    return number


def wait_for(predicate, timeout: float = 2.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(step)


def new_window(before: set[str], timeout: float = 5.0, expected_class: str | None = None) -> dict | None:
    def found():
        fresh = [w for w in windows() if w["address"] not in before
                 and (expected_class is None or expected_class.lower() in w["class"].lower())]
        return fresh[0] if len(fresh) == 1 else None
    return wait_for(found, timeout, 0.1)


def placement_geometry(monitor: dict, side: str) -> tuple[int, int, int, int]:
    """Logical coordinates for a half/quarter of a monitor, respecting scale, rotation, and bars."""
    width, height = monitor["width"], monitor["height"]
    if int(monitor.get("transform", 0)) % 2:
        width, height = height, width
    scale = float(monitor.get("scale", 1)) or 1.0
    reserve_left, reserve_top, reserve_right, reserve_bottom = monitor.get("reserved", [0, 0, 0, 0])
    gap = 12
    left = int(monitor["x"] + reserve_left + gap)
    top = int(monitor["y"] + reserve_top + gap)
    full_width = int(width / scale - reserve_left - reserve_right - gap * 2)
    full_height = int(height / scale - reserve_top - reserve_bottom - gap * 2)
    half_width, half_height = (full_width - gap) // 2, (full_height - gap) // 2
    if min(half_width, half_height) <= 0:
        raise ValueError("Monitor has no usable placement area")
    w = full_width if side in {"top", "bottom"} else half_width
    h = full_height if side in {"left", "right"} else half_height
    x = left + (half_width + gap if side in {"right", "top-right", "bottom-right"}
                else (full_width - half_width) // 2 if side == "center" else 0)
    y = top + (half_height + gap if side in {"bottom", "bottom-left", "bottom-right"}
               else (full_height - half_height) // 2 if side == "center" else 0)
    return x, y, w, h


def place(address: str, side: str) -> str:
    item = window(address)
    if side not in POSITIONS:
        raise ValueError("Position must be one of " + ", ".join(POSITIONS))
    monitor = next((m for m in query("monitors") if m.get("id") == item["monitor"]), None)
    if monitor is None:
        raise RuntimeError("Could not identify the window's monitor")
    x, y, w, h = placement_geometry(monitor, side)
    target = f'window = "address:{address}"'
    dispatch(f'hl.dsp.window.float({{ action = "enable", {target} }})')
    dispatch(f"hl.dsp.window.resize({{ x = {w}, y = {h}, {target} }})")
    dispatch(f"hl.dsp.window.move({{ x = {x}, y = {y}, {target} }})")
    return f"Placed {item['title'] or item['class']} on the {side} of {monitor['name']}"
