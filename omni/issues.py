"""Issue reports: gather evidence about something Omni got wrong, and brief a coding agent to fix it.

The evidence file and the task file live in the private data folder, never in the checkout.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import config

REPO = Path(__file__).resolve().parents[1]
SECRET = re.compile(r"key|token|secret|password", re.I)
OUTPUT_LIMIT = 1500


def collect(summary: str, turns: int = 12) -> Path:
    """Write the evidence file for `summary` and return its path."""
    folder = config.private_dir(config.DATA / "issues")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", summary.lower())[:40].strip("-") or "issue"
    path = folder / f"{stamp}-{slug}.md"
    sections = [
        f"# Omni issue {stamp}\n\n## Nick's summary\n\n{summary.strip()}",
        f"## Last {turns} foreground turns (newest first)\n\n{_turns(turns)}",
        f"## omnid journal (last 300 lines, httpx lines removed)\n\n~~~\n{_journal()}\n~~~",
        f"## Repository\n\n~~~\n{_git()}\n~~~",
        f"## Settings (secrets removed)\n\n~~~json\n{json.dumps(_scrub(asdict(config.load_settings())), indent=2, default=str)}\n~~~",
        f"## Setup checks\n\n{_doctor()}",
    ]
    path.write_text("\n\n".join(sections) + "\n")
    path.chmod(0o600)
    return path


def task_text(summary: str, evidence: Path) -> str:
    """Self-contained instructions for the coding agent."""
    test = re.match(r"\s*test\s*[:\-]", summary, re.I)
    lines = [
        "# Fix a problem Nick flagged in Omni",
        "",
        "Nick flagged a problem with Omni, his voice assistant for this desktop (the repository you are in). In his words:",
        "",
        *[f"> {line}" for line in summary.strip().splitlines()],
        "",
        f"The evidence is in {evidence}: his recent turns with Omni (requests, replies, tool calls, timings), "
        "the omnid journal, git state, settings and setup checks. Read it first, then read AGENTS.md and PLAN.md.",
        "",
    ]
    if test:
        lines += [
            "**This is only a test of the issue reporter.** Change nothing: do not edit files, run tests or restart "
            "services. Read the evidence file, report briefly what it contains (its sections and whether each looks "
            "complete), and stop.",
        ]
    else:
        lines += [
            "Your job:",
            "1. Find the root cause, and say which evidence points to it.",
            "2. Implement a fix, with a regression test in tests/ where sensible.",
            "3. Run `python3 -m unittest discover -s tests` and "
            "`~/.local/share/omni/venv/bin/python -m unittest discover -s tests`. Both must pass.",
            "4. If runtime code changed, restart the services: `systemctl --user restart omnid`, "
            "and `systemctl --user restart omni-app` if omni/app/ changed.",
            "5. Run `python3 scripts/check_public.py`.",
            "6. Summarize what you found and what you changed, and what Nick should try to confirm the fix.",
            "",
            "Rules:",
            "- Do not commit or push until Nick says so.",
            "- The evidence file holds private data. Never copy any of it into the repo (code, tests, fixtures, docs, "
            "PLAN.md); use synthetic examples.",
            "- The working tree may hold someone else's uncommitted changes; leave unrelated edits alone.",
        ]
    return "\n".join(lines) + "\n"


def prepare(summary: str, turns: int = 12) -> tuple[Path, str]:
    """Collect evidence, write the task next to it, and return (evidence, short prompt for the agent).

    The prompt only points at the task file, so no shell quoting can mangle the instructions."""
    evidence = collect(summary, turns)
    task = evidence.with_name(evidence.stem + "-task.md")
    task.write_text(task_text(summary, evidence))
    task.chmod(0o600)
    return evidence, f"Read {task} and do what it says."


def launch(prompt: str, agent: str = "claude", settings=None) -> dict:
    """Open the agent in a visible terminal in the repo without omnid (what Session.delegate does in visible mode)."""
    from .tools import Context, terminal
    return terminal.open_terminal(Context(settings=settings or config.load_settings()), str(REPO),
                                  f"{agent} {shlex.quote(prompt)}", True, None, None, True)


def _turns(limit: int) -> str:
    db = config.DATA / "episodes.sqlite"
    if not db.is_file():
        return "(no episodes.sqlite)"
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM episodes WHERE kind = 'turn' ORDER BY started_at DESC LIMIT ?",
                                  (limit,)).fetchall()
    finally:
        connection.close()
    return "\n\n".join(_turn(dict(row)) for row in rows) or "(no turns)"


def _turn(row: dict) -> str:
    when = datetime.fromtimestamp(row["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
    out = [f"### {when} · {row['source']} · {row['status']} (episode {row['id']}, {row.get('agent', '?')})",
           f"Request: {row['request']}", f"Reply: {_cut(row['reply'] or '(none)', 4000)}"]
    if row.get("error"):
        out.append(f"Error: {row['error']}")
    timings = json.loads(row.get("timings") or "{}")
    if timings:
        out.append("Timings (ms): " + ", ".join(f"{k} {v}" for k, v in timings.items()))
    for call in json.loads(row.get("tools") or "[]"):
        detail = [call.get("status", "?")]
        if call.get("duration_ms") is not None:
            detail.append(f"{call['duration_ms']} ms")
        for key in ("exit_code", "success"):
            if call.get(key) is not None:
                detail.append(f"{key} {call[key]}")
        out.append(f"- {call.get('type', 'tool')} `{call.get('label', '?')}` {', '.join(detail)}")
        if call.get("args") is not None:
            args = call["args"] if isinstance(call["args"], str) else json.dumps(call["args"], ensure_ascii=False)
            out.append(f"  args: {_cut(args, OUTPUT_LIMIT)}")
        if call.get("output"):
            body = "\n".join("  " + line for line in _cut(call["output"], OUTPUT_LIMIT).splitlines())
            out.append(f"  ~~~\n{body}\n  ~~~")
    return "\n".join(out)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def _journal() -> str:
    try:
        result = subprocess.run(["journalctl", "--user", "-u", "omnid", "-n", "800", "--no-pager", "-o", "short-iso"],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(journalctl failed: {exc})"
    lines = [line for line in result.stdout.splitlines() if "httpx" not in line]
    return "\n".join(lines[-300:]) or f"(no journal lines) {result.stderr.strip()}"


def _git() -> str:
    parts = []
    for args in (["log", "-1", "--oneline"], ["status", "--short"]):
        try:
            result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=10)
            parts.append(f"$ git {' '.join(args)}\n{(result.stdout or result.stderr).rstrip()}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            parts.append(f"$ git {' '.join(args)}\n(failed: {exc})")
    return "\n\n".join(parts)


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not SECRET.search(str(k))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _doctor() -> str:
    try:
        from .doctor import checks
        return "\n".join(f"- {status}: {name}: {hint}" for name, status, hint in checks())
    except Exception as exc:  # the report must still be written
        return f"(checks failed: {type(exc).__name__}: {exc})"
