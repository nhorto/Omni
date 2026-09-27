"""Deliberate speech mid-task and short spoken questions."""

from __future__ import annotations

from . import tool


@tool("Say one short sentence out loud now, for progress Nick should hear during a long task. "
      "Do not use it for the final answer; that is spoken automatically when appropriate.", text={"type": "string"})
def say(ctx, text: str) -> str:
    ctx.say(text)
    return "Said"


@tool("Ask Nick a short question and wait up to 30 s for his answer (spoken or typed). "
      "Use only when you cannot proceed sensibly without it.", question={"type": "string"})
def ask(ctx, question: str) -> str:
    answer = ctx.ask(question)
    return answer or "(no answer; proceed with the safest reasonable choice or stop)"
