"""Tools Omni uses to grow: facts about Nick, its own notes, episode recall, and skills."""

from __future__ import annotations

from . import tool

STORE = {"type": "string", "enum": ["user", "memory"],
         "description": "user = facts about Nick (people, preferences, businesses); memory = your own notes on how to do things for him"}


@tool("Save a durable fact or lesson. Do this without asking whenever Nick states a preference, corrects you, "
      "or you learn something that will matter next week. One fact per call, written as a full sentence.",
      store=STORE, text={"type": "string"})
def memory_save(ctx, store: str, text: str) -> str:
    entry = ctx.memory.save(store, text, ctx.episode)
    return f"Saved {entry.id}"


@tool("Rewrite a saved entry by id (ids are shown in brackets in your notes).", id={"type": "string"}, text={"type": "string"})
def memory_update(ctx, id: str, text: str) -> str:
    return f"Updated {ctx.memory.update(id, text).id}"


@tool("Delete a saved entry by id, e.g. when Nick says 'forget that' or a fact is wrong.", id={"type": "string"})
def memory_forget(ctx, id: str) -> str:
    removed = ctx.memory.forget(id)
    return f"Forgot {removed.id}: {removed.text}"


@tool("Search saved facts and past conversations (what Nick asked, what you did, when).", query={"type": "string"})
def memory_search(ctx, query: str) -> dict:
    found = ctx.memory.search(query)
    for item in found["episodes"]:
        item.pop("timings", None)
        item["reply"] = item["reply"][:600]
    return found


@tool("Save a reusable procedure as a skill after a multi-step task worked and is likely to recur. "
      "The body is markdown: when to use it, the steps, and pitfalls. Saving the same name replaces it.",
      name={"type": "string", "description": "lowercase-hyphenated"},
      description={"type": "string", "description": "One sentence: what it does and when to use it"},
      body={"type": "string"})
def skill_save(ctx, name: str, description: str, body: str) -> str:
    return f"Saved skill at {ctx.memory.save_skill(name, description, body)}"


@tool("List saved skills with their descriptions.")
def skill_list(ctx) -> list[dict]:
    return [{"name": s["name"], "description": s["description"]} for s in ctx.memory.skills()]


@tool("Read a saved skill's full instructions.", name={"type": "string"})
def skill_read(ctx, name: str) -> str:
    return ctx.memory.read_skill(name)
