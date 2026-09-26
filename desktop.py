"""Small AT-SPI adapter for accessible desktop controls."""

from __future__ import annotations

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


def apps() -> list[str]:
    root = Atspi.get_desktop(0)
    names = []
    for index in range(root.get_child_count()):
        app = root.get_child_at_index(index)
        name = app.get_name()
        if name and not name.startswith("Unnamed") and app.get_child_count():
            names.append(name)
    return names


def app_by_name(name: str):
    root = Atspi.get_desktop(0)
    matches = []
    for index in range(root.get_child_count()):
        app = root.get_child_at_index(index)
        if app.get_name() == name and app.get_child_count():
            matches.append(app)
    if len(matches) != 1:
        raise ValueError("Desktop app is not uniquely available through accessibility")
    return matches[0]


def controls(name: str) -> list[tuple[str, str, object]]:
    app = app_by_name(name)
    found = []
    visited = 0

    def walk(node, depth: int) -> None:
        nonlocal visited
        if visited >= 500 or depth > 24:
            return
        visited += 1
        try:
            label = node.get_name()
            count = node.get_n_actions()
            if label and count:
                actions = {node.get_action_name(i) for i in range(count)}
                if "click" in actions or "activate" in actions or "press" in actions:
                    found.append((label, node.get_role_name(), node))
            for index in range(min(node.get_child_count(), 50)):
                walk(node.get_child_at_index(index), depth + 1)
        except Exception:
            return

    walk(app, 0)
    return found


def read(name: str) -> str:
    items = controls(name)
    if not items:
        return f"{name}: no accessible clickable controls were found"
    return "\n".join(f"{role}: {label}" for label, role, _ in items[:80])


def resolve(name: str, label: str):
    items = controls(name)
    matches = [(actual, node) for actual, _, node in items if actual == label]
    if not matches:
        matches = [(actual, node) for actual, _, node in items if len(actual) >= 3 and label.startswith(actual) and len(label) - len(actual) <= 24]
    if len(matches) != 1:
        raise ValueError("Desktop control is not unique or has no accessibility action")
    return matches[0]


def click(name: str, label: str) -> str:
    actual, node = resolve(name, label)
    for index in range(node.get_n_actions()):
        if node.get_action_name(index) in {"click", "activate", "press"}:
            if node.do_action(index):
                return f"Activated {actual} in {name}"
            break
    raise RuntimeError("Desktop control did not activate")
