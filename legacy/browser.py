"""Bounded Playwright CLI adapter for Omi's separate visible browser profile."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = Path.home() / ".local/share/omi/browser-profile"
OUTPUT = Path.home() / ".local/share/omi/browser-output"
SESSION = "-s=omi"
REF = re.compile(r"(?:f\d+)?e\d+")
INTERACTIVE = re.compile(r'^\s*- (link|button|menuitem|tab|checkbox|radio|textbox|searchbox|combobox|spinbutton)(?: "([^"]+)")?[^\n]*\[ref=((?:f\d+)?e\d+)\]', re.M)


def command(*args: str, timeout: int = 45) -> str:
    executable = shutil.which("playwright")
    if not executable or not Path("/usr/bin/chromium").is_file():
        raise RuntimeError("Playwright CLI or Chromium is unavailable")
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUTPUT.chmod(0o700)
    env = dict(os.environ, PLAYWRIGHT_MCP_EXECUTABLE_PATH="/usr/bin/chromium", PLAYWRIGHT_MCP_OUTPUT_DIR=str(OUTPUT))
    result = subprocess.run(
        [executable, "cli", SESSION, *args, "--raw"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-1200:] or "Browser command failed")
    return result.stdout.strip()


def is_open() -> bool:
    try:
        command("tab-list", timeout=10)
        return True
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def snapshot() -> str:
    if not is_open():
        raise RuntimeError("Omi browser is not open yet")
    tabs = command("tab-list", timeout=10)
    page = command("snapshot", timeout=20)
    return (tabs + "\n" + page)[:16000]


def context_for_request(request: str) -> str:
    if not re.search(r"\b(browser|webpage|website|page|tab|click|form|site|on this|on that)\b", request, re.I):
        return ""
    try:
        return snapshot()
    except (RuntimeError, subprocess.TimeoutExpired):
        return ""


def open_url(url: str) -> str:
    if is_open():
        command("goto", url)
    else:
        PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
        PROFILE.chmod(0o700)
        command("open", url, "--headed", f"--profile={PROFILE}", timeout=60)
    return command("tab-list", timeout=10)


def target_selector(target: str, *, allow_text: bool) -> str:
    if REF.fullmatch(target):
        return target
    if allow_text and target.strip() and len(target) <= 120:
        return "text=" + json.dumps(target, ensure_ascii=False)
    raise ValueError("Choose an element reference from the browser snapshot")


def resolve_target(target: str, *, fill_field: bool = False) -> tuple[str, str]:
    page = snapshot()
    matches = INTERACTIVE.findall(page)
    allowed = {"textbox", "searchbox", "combobox", "spinbutton"} if fill_field else {"link", "button", "menuitem", "tab", "checkbox", "radio"}
    candidates = [(ref, label or role) for role, label, ref in matches if role in allowed]
    raw = target.strip()
    if REF.fullmatch(raw):
        matching = [(ref, label) for ref, label in candidates if ref == raw]
    else:
        matching = [(ref, label) for ref, label in candidates if label == raw]
    if len(matching) != 1:
        raise ValueError("Browser target is not a unique visible control on the current page")
    return matching[0]


def read_page() -> str:
    return snapshot()


def click(target: str) -> str:
    if not is_open():
        raise RuntimeError("Omi browser is not open yet")
    ref, _ = resolve_target(target)
    command("click", ref)
    return command("tab-list", timeout=10) + "\n" + command("snapshot", timeout=20)[:8000]


def fill(target: str, value: str) -> str:
    if not is_open():
        raise RuntimeError("Omi browser is not open yet")
    if len(value) > 2000:
        raise ValueError("Browser input is too long")
    ref, _ = resolve_target(target, fill_field=True)
    command("fill", ref, value)
    return "Filled the selected browser field; nothing was submitted"


def back() -> str:
    if not is_open():
        raise RuntimeError("Omi browser is not open yet")
    command("go-back")
    return command("tab-list", timeout=10)
