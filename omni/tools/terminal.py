"""Visible terminals Omni opens for Nick, with a handle to read their output later.

Headless commands do not come through here: Codex runs those with its own shell
tool, and omnid's policy decides whether they need approval.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .. import config
from . import hypr, tool

JOBS = config.DATA / "terminal-jobs"


def terminal_argv(settings, cwd: str, inner: list[str] | None, title: str) -> list[str]:
    program = getattr(settings, "terminal", "foot") if settings else "foot"
    if program == "foot" or not shutil.which(program):
        argv = ["foot", f"--working-directory={cwd}", f"--title={title}"]
        return argv + (inner or [])
    if program == "alacritty":
        return ["alacritty", "--working-directory", cwd, "--title", title] + (["-e", *inner] if inner else [])
    if program == "ghostty":
        return ["ghostty", f"--working-directory={cwd}", f"--title={title}"] + (["-e", *inner] if inner else [])
    return [program] + (["-e", *inner] if inner else [])


def open_terminal(ctx, cwd: str, command: str | None, hold: bool, position: str | None, workspace: int | None,
                  interactive: bool = False) -> dict:
    directory = Path(cwd or "~").expanduser()
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")
    handle = uuid.uuid4().hex[:10]
    config.private_dir(JOBS)
    log, result = JOBS / f"{handle}.log", JOBS / f"{handle}.json"
    inner = None
    if command:
        # The runner tees output to the log and writes an exit record Omni can read.
        inner = [sys.executable, "-m", "omni.tools.term_runner", str(log), str(result),
                 "--interactive" if interactive else "--capture", "--hold" if hold else "--no-hold", command]
    if workspace:
        hypr.dispatch(f'hl.dsp.focus({{ workspace = "{hypr.workspace(workspace)}" }})')
    before = {w["address"] for w in hypr.windows()}
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    subprocess.Popen(terminal_argv(ctx.settings, str(directory), inner, f"Omni {handle}"), cwd=directory, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    created = hypr.new_window(before, 5.0)
    info = {"handle": handle, "address": created["address"] if created else None, "cwd": str(directory)}
    if created and position:
        info["placed"] = hypr.place(created["address"], position)
    return info


@tool("Open a visible terminal window, optionally running a command in it. Returns a handle for terminal_output. "
      "Use for anything Nick should see; use your own shell tool for quiet background commands.", action=True,
      cwd={"type": "string", "description": "Working directory, default home"},
      command={"type": "string", "description": "Shell command to run; omit for an interactive shell"},
      hold={"type": "boolean", "description": "Keep the window open after the command exits (default true)"},
      position={"type": "string", "enum": list(hypr.POSITIONS)},
      workspace={"type": "integer", "minimum": 1, "maximum": 10})
def terminal_open(ctx, cwd: str = "~", command: str = "", hold: bool = True, position: str | None = None,
                  workspace: int | None = None) -> dict:
    return open_terminal(ctx, cwd, command or None, hold, position, workspace)


@tool("Read a terminal's recent output and exit status by handle.", handle={"type": "string"},
      lines={"type": "integer", "minimum": 1, "maximum": 400})
def terminal_output(ctx, handle: str, lines: int = 60) -> dict:
    if not handle.isalnum():
        raise ValueError("Bad handle")
    log, result = JOBS / f"{handle}.log", JOBS / f"{handle}.json"
    if not log.exists():
        raise KeyError("No output recorded for that handle (interactive shells without a command are not captured)")
    tail = log.read_text(errors="replace").splitlines()[-lines:]
    record = json.loads(result.read_text()) if result.exists() else None
    return {"running": record is None, "exit_code": record and record["exit_code"], "output": "\n".join(tail)}
