"""Omni's tool surface, shared by every agent adapter.

Each tool is a small synchronous function. `@tool` records its JSON schema and two
flags: `action` (it changes the desktop, so Omni stays quiet afterwards) and
`ask` (it always needs Nick's approval, e.g. sending mail). An `ask` tool may give
`confirm`, which writes the approval text so Nick sees exactly what will happen
(the default summary truncates each argument). The daemon runs tools
on worker threads and hands them a `Context` for memory, speech, and delegation.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Any]
    action: bool = False
    ask: bool = False
    confirm: Callable[[dict], str] | None = None

    def spec(self) -> dict:
        return {"type": "function", "name": self.name, "description": self.description, "inputSchema": self.schema}


@dataclass
class Context:
    """What a running tool can reach. The daemon fills the callables in."""
    memory: Any = None
    settings: Any = None
    episode: int | None = None
    cwd: str | None = None
    say: Callable[[str], None] = lambda text: None
    ask: Callable[[str], str] = lambda question: ""
    delegate: Callable[..., dict] = lambda **kwargs: {"error": "delegation unavailable"}
    extras: dict = field(default_factory=dict)


REGISTRY: dict[str, Tool] = {}


def tool(summary: str, /, *, action: bool = False, ask: bool = False, confirm: Callable[[dict], str] | None = None,
         **properties):
    """Register a tool. Keyword arguments are JSON-schema property definitions."""
    def decorate(function):
        parameters = inspect.signature(function).parameters
        required = [name for name, p in parameters.items() if name != "ctx" and p.default is inspect.Parameter.empty]
        schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
        REGISTRY[function.__name__] = Tool(function.__name__, summary, schema, function, action, ask, confirm)
        return function
    return decorate


def load_all() -> dict[str, Tool]:
    from . import desktop, terminal, research, memory_tools, reminders, voice, delegate, mail  # noqa: F401
    return REGISTRY


def approval_text(item: Tool, arguments: dict) -> str:
    """What Nick is asked to approve before an `ask` tool runs."""
    if item.confirm:
        try:
            return item.confirm(arguments or {})
        except (ValueError, KeyError, TypeError):
            pass
    return f"{item.name}: " + ", ".join(f"{k}={str(v)[:80]}" for k, v in (arguments or {}).items())


def run(name: str, arguments: dict, ctx: Context) -> tuple[bool, str]:
    """Run a tool and return (success, text for the model)."""
    item = REGISTRY.get(name)
    if item is None:
        return False, f"Unknown tool {name}"
    try:
        result = item.handler(ctx, **(arguments or {}))
    except (ValueError, KeyError, RuntimeError, OSError, TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except TypeError as exc:
        return False, f"Bad arguments for {name}: {exc}"
    if isinstance(result, str):
        return True, result
    return True, json.dumps(result, ensure_ascii=False, default=str)
