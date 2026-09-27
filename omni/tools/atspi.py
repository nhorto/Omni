"""AT-SPI access to buttons and text fields in running apps (imported lazily; needs PyGObject)."""

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


def editable_fields(name: str) -> list[tuple[str, object]]:
    app = app_by_name(name)
    found = []
    visited = 0

    def walk(node, depth: int) -> None:
        nonlocal visited
        if visited >= 500 or depth > 24:
            return
        visited += 1
        try:
            if node.get_editable_text_iface() is not None:
                label = node.get_name()
                if label:
                    found.append((label, node))
            for index in range(min(node.get_child_count(), 50)):
                walk(node.get_child_at_index(index), depth + 1)
        except Exception:
            return

    walk(app, 0)
    return found


def resolve_field(name: str, label: str):
    matches = [node for actual, node in editable_fields(name) if actual == label]
    if len(matches) != 1:
        raise ValueError("Editable field is not unique or unavailable through accessibility")
    return matches[0]


def fill(name: str, label: str, value: str) -> str:
    node = resolve_field(name, label)
    editable = node.get_editable_text_iface()
    if not editable.set_text_contents(value):
        raise RuntimeError("Desktop field rejected the text")
    readable = node.get_text_iface()
    if readable is None or readable.get_text(0, -1) != value:
        raise RuntimeError("Desktop field text could not be verified")
    return f"Filled {label} in {name}"


def read(name: str) -> str:
    items = controls(name)
    fields = editable_fields(name)
    if not items and not fields:
        return f"{name}: no accessible controls were found"
    return "\n".join([*(f"{role}: {label}" for label, role, _ in items[:80]), *(f"editable: {label}" for label, _ in fields[:80])])


def resolve(name: str, label: str):
    items = controls(name)
    matches = [(actual, node) for actual, _, node in items if actual == label]
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
