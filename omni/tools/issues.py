"""Flag a problem with Omni itself: collect evidence and open a coding agent on the repo to fix it."""

from __future__ import annotations

from .. import issues
from . import tool


@tool("Report a problem with Omni itself to a coding agent that diagnoses and fixes it in a visible terminal. "
      "Use when Nick says to flag, report or log an issue or problem, or to get Claude or Codex to fix something "
      "about Omni. Omni gathers recent turns and logs as evidence.",
      summary={"type": "string", "description": "Nick's words about the problem, plus what you observed (the failing "
                                                "request, tool, or error)"},
      agent={"type": "string", "enum": ["claude", "codex"], "description": "Default claude"})
def issue_report(ctx, summary: str, agent: str = "claude") -> dict:
    evidence, prompt = issues.prepare(summary)
    result = ctx.delegate(task=prompt, mode="visible", cwd=str(issues.REPO), agent=agent)
    if result.get("error"):
        raise RuntimeError(f"Evidence saved to {evidence}, but the agent did not start: {result['error']}")
    return {"evidence": str(evidence), "delegation": result.get("id"), "agent": agent}
