"""Hand a bigger job to another agent: a visible terminal Nick can watch, or a background thread."""

from __future__ import annotations

from . import tool


@tool("Delegate a substantial task (coding, multi-file research, long analysis) to a separate agent. "
      "mode 'visible' opens a terminal running the agent so Nick can watch and take over; "
      "'background' runs it headless and reports back when done. Pick visible when Nick says "
      "'in a terminal', 'so I can watch', or 'let me see'; otherwise use the default mode.",
      task={"type": "string", "description": "Complete, self-contained instructions for the other agent"},
      mode={"type": "string", "enum": ["visible", "background", "default"]},
      cwd={"type": "string", "description": "Working directory, e.g. a repo path"},
      agent={"type": "string", "enum": ["codex", "claude"]})
def delegate(ctx, task: str, mode: str = "default", cwd: str = "~", agent: str = "codex") -> dict:
    return ctx.delegate(task=task, mode=mode, cwd=cwd, agent=agent)


@tool("List delegated tasks and their status (running, done, failed) with any result summary.")
def delegate_status(ctx) -> list[dict]:
    return ctx.extras["delegations"]() if "delegations" in ctx.extras else []
