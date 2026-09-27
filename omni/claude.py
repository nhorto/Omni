"""Claude adapter: the same tools, policy, memory, and turn events as Codex, via the Claude Agent SDK.

`ClaudeRunner` mirrors `session.Runner`: `run(turn, text)`, `interrupt()`,
`close()`. Omni's tools are served in-process as the `omni` MCP server; every
Bash command and file edit goes through `policy.py` via `can_use_tool`.
Needs `pip install claude-agent-sdk` (in the Omni venv) and a signed-in `claude`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage, StreamEvent, SystemMessage, ToolResultBlock,
                              ToolUseBlock, UserMessage, create_sdk_mcp_server, tool)

from . import config, tools
from .policy import ALLOW

log = logging.getLogger("omni.claude")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
NO_NARRATION = ("\nWhen you use tools, do not narrate beforehand; the reply is spoken aloud, "
                "so only the final answer should contain text.\n")


def skills_plugin() -> dict:
    """Expose Omni's skills folder to Claude as a local plugin (plugin.json + skills/)."""
    root = config.private_dir(config.DATA / "claude-plugin")
    manifest = config.private_dir(root / ".claude-plugin") / "plugin.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({"name": "omni", "description": "Skills Omni wrote for Nick"}))
    link = root / "skills"
    if not link.exists():
        link.symlink_to(config.private_dir(config.DATA / "skills"))
    return {"type": "local", "path": str(root)}


class ClaudeRunner:
    def __init__(self, session, *, kind: str, cwd: str, instructions: str, model: str | None = None,
                 with_tools: bool = True, effort: str | None = None):
        self.session, self.kind, self.cwd = session, kind, cwd
        self.turn = None
        self.thread_id = f"claude-{int(time.time())}"
        self.started = time.time()
        self.last_used = time.time()
        self.context_tokens = 0
        self._blocks: dict[int, dict] = {}
        servers = {"omni": self._tool_server()} if with_tools else {}
        self.client = ClaudeSDKClient(ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": instructions + NO_NARRATION},
            mcp_servers=servers, cwd=cwd, model=model, effort=effort, include_partial_messages=True,
            can_use_tool=self._can_use_tool, permission_mode="default",
            setting_sources=["user"], skills="all", plugins=[skills_plugin()],
            # Omni's ~35 tools must be callable directly; deferred tool search costs a model round trip per turn.
            env={"ENABLE_TOOL_SEARCH": "false"},
        ))

    async def start(self) -> None:
        await self.client.connect()

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception as exc:
            log.debug("disconnect: %s", exc)

    async def interrupt(self) -> None:
        if self.turn and not self.turn.done.done():
            await self.client.interrupt()

    def _tool_server(self):
        definitions = []
        for spec in tools.REGISTRY.values():
            async def handler(args, name=spec.name):
                result = await self.session.call_tool(name, args, self.turn, self)
                text = "".join(item.get("text", "") for item in result["contentItems"])
                return {"content": [{"type": "text", "text": text}], "is_error": not result["success"]}
            definitions.append(tool(spec.name, spec.description, spec.schema)(handler))
        return create_sdk_mcp_server("omni", "0.2.0", definitions)

    async def _can_use_tool(self, name: str, arguments: dict, context):
        turn = self.turn
        if name.startswith("mcp__omni__"):
            spec = tools.REGISTRY.get(name.removeprefix("mcp__omni__"))
            if spec and spec.ask and not await self.session.decide_ask(f"{spec.name}: {arguments}", turn):
                return PermissionResultDeny(message="Nick declined this action.")
            return PermissionResultAllow(updated_input=arguments)
        if name == "Bash":
            decision = self.session.policy.command(arguments.get("command", ""), self.cwd)
        elif name in EDIT_TOOLS:
            path = arguments.get("file_path") or arguments.get("notebook_path") or ""
            kind = "update" if Path(path).exists() else "add"
            decision = self.session.policy.file_change([{"path": path, "kind": kind}], self.cwd)
        else:
            return PermissionResultAllow(updated_input=arguments)
        if decision.action == ALLOW:
            return PermissionResultAllow(updated_input=arguments)
        detail = arguments.get("command") or arguments.get("file_path") or name
        verdict = await self.session.decide(decision, detail, turn)
        if verdict == "accept":
            return PermissionResultAllow(updated_input=arguments)
        return PermissionResultDeny(message=decision.reason or "Nick declined this.")

    async def run(self, turn, text: str, **_params):
        self.turn = turn
        self.last_used = time.time()
        emit = self.session.emit
        try:
            await self.client.query(text)
            turn.mark("turn_started")
            async for message in self.client.receive_response():
                if isinstance(message, StreamEvent):
                    self._stream(turn, message.event)
                elif isinstance(message, AssistantMessage):
                    usage = getattr(message, "usage", None) or {}
                    if usage:
                        self.context_tokens = sum(usage.get(k, 0) for k in
                                                  ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            self._tool_started(turn, block.id, block.name, block.input)
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            for record in turn.tools:
                                if record["id"] == block.tool_use_id:
                                    record.update(finished(block))
                                    emit({"event": "tool", "episode": turn.episode, "kind": self.kind, **record})
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    self.thread_id = message.data.get("session_id", self.thread_id)
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    turn.tokens["input"] += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) \
                        + usage.get("cache_creation_input_tokens", 0)
                    turn.tokens["cached"] += usage.get("cache_read_input_tokens", 0)
                    turn.tokens["output"] += usage.get("output_tokens", 0)
                    turn.status = "failed" if message.is_error else (
                        "interrupted" if message.subtype == "error_during_execution" else "completed")
                    if message.is_error:
                        turn.error = str(message.result or message.errors or message.subtype)
                    if not turn.reply and message.result:
                        turn.reply.append(message.result)
        except Exception as exc:
            turn.status, turn.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            turn.gate.finish()
            turn.mark("completed")
            if not turn.done.done():
                turn.done.set_result(None)
            self.turn = None
            self.last_used = time.time()
        return turn

    def _stream(self, turn, event: dict) -> None:
        kind = event.get("type")
        if kind == "content_block_start":
            block = event.get("content_block", {})
            self._blocks[event.get("index", 0)] = block
            if block.get("type") == "tool_use":
                self._tool_started(turn, block["id"], block["name"], {})
        elif kind == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
            delta = event["delta"]["text"]
            turn.mark("first_token")
            turn.mark("first_answer_token")
            turn.reply.append(delta)
            turn.gate.delta(delta)
            self.session.emit({"event": "delta", "episode": turn.episode, "kind": self.kind, "text": delta})

    def _tool_started(self, turn, ident: str, name: str, arguments: dict) -> None:
        """Called once from the stream (no input yet) and again from the full message (with input)."""
        label = name.removeprefix("mcp__omni__")
        if name == "Bash":
            label = arguments.get("command", "shell")
        existing = next((r for r in turn.tools if r["id"] == ident), None)
        if existing:
            if arguments and existing["label"] != label:
                existing["label"] = label
                if acted(name, arguments):
                    turn.gate.acted = True
                self.session.emit({"event": "tool", "episode": turn.episode, "kind": self.kind, **existing})
            return
        record = {"id": ident, "type": "command" if name == "Bash" else "edit" if name in EDIT_TOOLS else "tool",
                  "label": label, "status": "running"}
        if arguments and acted(name, arguments) or name in EDIT_TOOLS:
            turn.gate.acted = True
        turn.tools.append(record)
        self.session.emit({"event": "tool", "episode": turn.episode, "kind": self.kind, **record})


READ_COMMANDS = {"ls", "cat", "head", "tail", "grep", "rg", "find", "pwd", "date", "which", "echo", "wc", "stat", "file",
                 "hyprctl", "curl", "jq", "ps", "df", "du", "uname", "whoami", "env", "printenv", "git", "omarchy"}


def acted(name: str, arguments: dict) -> bool:
    """Did this tool change the desktop or files? Decides whether Omni stays quiet afterwards."""
    if name.startswith("mcp__omni__"):
        spec = tools.REGISTRY.get(name.removeprefix("mcp__omni__"))
        return bool(spec and spec.action)
    if name == "Bash":
        words = arguments.get("command", "").split()
        return bool(words) and words[0] not in READ_COMMANDS
    return name in EDIT_TOOLS


def finished(block: ToolResultBlock) -> dict:
    content = block.content
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return {"status": "failed" if block.is_error else "completed", "output": str(content or "")[-400:]}
