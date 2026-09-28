"""Warm agent threads: turns, tool calls, approvals, token accounting, retirement, delegation.

One `Runner` drives one Codex thread (the foreground conversation, a background
delegation, or a retiring thread's reflection pass). `Session` owns the
foreground runner, decides when to retire it, and records every turn as an
episode.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from . import config, prompt, tools
from .codex import CodexAppServer, CodexError
from .memory import Memory
from .policy import ALLOW, DENY, Policy
from .voice.speak import SpeechGate, Speaker

log = logging.getLogger("omni.session")

READ_ACTIONS = {"read", "listFiles", "search"}
Emit = Callable[[dict], None]
Prompt = Callable[..., Awaitable[str]]


@dataclass
class Turn:
    request: str
    source: str
    episode: int
    gate: SpeechGate
    t0: float
    turn_id: str | None = None
    items: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    reply: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=lambda: {"input": 0, "cached": 0, "output": 0})
    status: str = "running"
    error: str = ""
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def mark(self, name: str) -> None:
        self.timings.setdefault(name, int((time.monotonic() - self.t0) * 1000))

    @property
    def text(self) -> str:
        return "".join(self.reply).strip()


class Runner:
    """Drives turns on one app-server thread and answers its server requests."""

    def __init__(self, session: "Session", thread_id: str, *, kind: str, cwd: str | None = None):
        self.session, self.thread_id, self.kind, self.cwd = session, thread_id, kind, cwd
        self.turn: Turn | None = None
        self.started = time.time()
        self.last_used = time.time()
        self.context_tokens = 0
        codex = session.codex
        codex.thread_notifications[thread_id] = self.on_notification
        codex.thread_requests[thread_id] = self.on_request

    async def run(self, turn: Turn, text: str, **params) -> Turn:
        self.turn = turn
        self.last_used = time.time()
        try:
            turn.turn_id = await self.session.codex.start_turn(self.thread_id, text, **params)
            turn.mark("turn_started")
            await turn.done
        except (CodexError, ConnectionError, asyncio.TimeoutError) as exc:
            turn.status, turn.error = "failed", str(exc)
        finally:
            turn.gate.finish()
            turn.mark("completed")
            self.turn = None
            self.last_used = time.time()
        return turn

    async def close(self) -> None:
        await self.session.codex.archive(self.thread_id)

    async def interrupt(self) -> None:
        turn = self.turn
        if turn and turn.turn_id and not turn.done.done():
            try:
                await self.session.codex.interrupt(self.thread_id, turn.turn_id)
            except (CodexError, ConnectionError, asyncio.TimeoutError) as exc:
                log.warning("interrupt failed: %s", exc)
                if not turn.done.done():
                    turn.status = "interrupted"
                    turn.done.set_result(None)

    # ---- notifications ------------------------------------------------------

    def on_notification(self, method: str, params: dict) -> None:
        turn = self.turn
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            last = usage.get("last", {})
            self.context_tokens = last.get("totalTokens", self.context_tokens)
            if turn:
                turn.tokens["input"] += last.get("inputTokens", 0)
                turn.tokens["cached"] += last.get("cachedInputTokens", 0)
                turn.tokens["output"] += last.get("outputTokens", 0)
            return
        if turn is None:
            return
        emit = self.session.emit
        if method == "item/started":
            item = params["item"]
            turn.items[item["id"]] = item
            record = describe(item)
            if record:
                if acted(item):
                    turn.gate.acted = True
                turn.tools.append(record)
                emit({"event": "tool", "episode": turn.episode, "kind": self.kind, **record})
        elif method == "item/agentMessage/delta":
            item = turn.items.get(params.get("itemId"), {})
            delta = params.get("delta", "")
            turn.mark("first_token")
            if item.get("phase") == "commentary":
                emit({"event": "commentary", "episode": turn.episode, "kind": self.kind, "text": delta})
            else:
                turn.mark("first_answer_token")
                turn.reply.append(delta)
                turn.gate.delta(delta)
                emit({"event": "delta", "episode": turn.episode, "kind": self.kind, "text": delta})
        elif method == "item/completed":
            item = params["item"]
            turn.items[item["id"]] = item
            if item.get("type") == "agentMessage" and item.get("phase") != "commentary" and not turn.reply:
                turn.reply.append(item.get("text", ""))  # no deltas arrived (e.g. very short reply)
            for record in turn.tools:
                if record["id"] == item["id"]:
                    record.update(finished(item))
                    emit({"event": "tool", "episode": turn.episode, "kind": self.kind, **record})
        elif method == "turn/completed":
            info = params.get("turn", {})
            turn.status = info.get("status", "completed")
            if info.get("error"):
                turn.error = str(info["error"].get("message", info["error"]))
            if not turn.done.done():
                turn.done.set_result(None)
        elif method == "error":
            turn.error = str((params.get("error") or {}).get("message", params))

    # ---- server requests ----------------------------------------------------

    async def on_request(self, method: str, params: dict):
        turn = self.turn
        session = self.session
        if method == "item/tool/call":
            return await session.call_tool(params["tool"], params.get("arguments") or {}, turn, self)
        if method == "item/commandExecution/requestApproval":
            decision = session.policy.command(params.get("command", ""), params.get("cwd"))
            command = display_command(params)
            return {"decision": await session.decide(decision, command, turn)}
        if method == "item/fileChange/requestApproval":
            item = turn.items.get(params.get("itemId"), {}) if turn else {}
            decision = session.policy.file_change(item.get("changes", []), self.cwd)
            paths = ", ".join(c.get("path", "?") for c in item.get("changes", [])[:3])
            return {"decision": await session.decide(decision, f"change {paths}", turn)}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            answers = {}
            for question in params.get("questions", []):
                options = ", ".join(o["label"] for o in question.get("options") or [])
                answer = await session.prompt("question", question["question"] + (f" ({options})" if options else ""))
                answers[question["id"]] = {"answers": [answer] if answer else []}
            return {"answers": answers}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        raise LookupError(f"unsupported request {method}")


def describe(item: dict) -> dict | None:
    kind = item.get("type")
    if kind == "commandExecution":
        return {"id": item["id"], "type": "command", "label": display_command(item), "status": "running"}
    if kind == "dynamicToolCall":
        return {"id": item["id"], "type": "tool", "label": item.get("tool", "?"), "args": item.get("arguments"), "status": "running"}
    if kind == "webSearch":
        return {"id": item["id"], "type": "search", "label": item.get("query") or "web search", "status": "running"}
    if kind == "fileChange":
        paths = [c.get("path", "?") for c in item.get("changes", [])]
        return {"id": item["id"], "type": "edit", "label": ", ".join(paths[:3]), "status": "running"}
    if kind == "mcpToolCall":
        return {"id": item["id"], "type": "tool", "label": f"{item.get('server')}.{item.get('tool')}", "status": "running"}
    return None


def finished(item: dict) -> dict:
    status = item.get("status") or "completed"
    result = {"status": status, "duration_ms": item.get("durationMs")}
    if item.get("type") == "commandExecution":
        result["exit_code"] = item.get("exitCode")
        result["output"] = (item.get("aggregatedOutput") or "")[-400:]
    if item.get("type") == "dynamicToolCall":
        result["success"] = item.get("success")
    return result


def acted(item: dict) -> bool:
    kind = item.get("type")
    if kind == "commandExecution":
        actions = item.get("commandActions") or [{"type": "unknown"}]
        return any(a.get("type") not in READ_ACTIONS for a in actions)
    if kind == "dynamicToolCall":
        spec = tools.REGISTRY.get(item.get("tool", ""))
        return bool(spec and spec.action)
    return kind == "fileChange"


def display_command(item: dict) -> str:
    actions = item.get("commandActions") or []
    if actions and actions[0].get("command"):
        return "; ".join(a["command"] for a in actions if a.get("command"))
    from .policy import unwrap_shell
    return unwrap_shell(item.get("command", ""))


@dataclass
class Delegation:
    id: str
    task: str
    mode: str
    cwd: str
    agent: str
    status: str = "running"
    started: float = field(default_factory=time.time)
    summary: str = ""
    report: str = ""
    handle: str = ""
    runner: Runner | None = None

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "runner"}


class Session:
    def __init__(self, settings, memory: Memory, policy: Policy, emit: Emit, prompt_user: Prompt, speaker: Speaker | None):
        self.settings, self.memory, self.policy = settings, memory, policy
        self.emit, self.prompt, self.speaker = emit, prompt_user, speaker
        self.codex = CodexAppServer()
        self.codex.on_exit = self._codex_exited
        self.runner: Runner | None = None
        self.model: str | None = settings.model or None
        self.lock = asyncio.Lock()
        self.delegations: dict[str, Delegation] = {}
        self.budget_warned: str = ""
        self.tools = tools.load_all()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.current: Turn | None = None
        self.foreground_lock = asyncio.Lock()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            await self.codex.start()
        except (OSError, CodexError, asyncio.TimeoutError) as exc:
            if self.settings.agent != "claude":
                raise
            log.warning("codex app-server unavailable (%s); running on Claude only", exc)
            return
        if not self.model:
            self.model = pick_fast_model(await self.codex.models())
        try:
            await self.codex.request("skills/extraRoots/set", {"extraRoots": [str(self.memory.skills_dir)]})
        except CodexError as exc:
            log.warning("could not add Omni skills to Codex: %s", exc)

    def _codex_exited(self) -> None:
        log.warning("codex app-server exited")
        for runner in [self.runner, *(d.runner for d in self.delegations.values())]:
            if runner and runner.turn and not runner.turn.done.done():
                runner.turn.status, runner.turn.error = "failed", "codex app-server exited"
                runner.turn.done.set_result(None)
        self.runner = None

    async def _ensure_codex(self) -> None:
        if not self.codex.alive:
            self.codex = CodexAppServer()
            self.codex.on_exit = self._codex_exited
            await self.start()

    async def new_thread(self, *, instructions: str, cwd: str, model: str | None, ephemeral: bool = True,
                         dynamic_tools: bool = True) -> str:
        params = {
            "cwd": cwd, "approvalPolicy": "untrusted", "sandbox": "danger-full-access", "ephemeral": ephemeral,
            "developerInstructions": instructions, "serviceName": "omni",
            "config": {"web_search": "live"},
        }
        if model:
            params["model"] = model
        if dynamic_tools:
            params["dynamicTools"] = [t.spec() for t in self.tools.values()]
        return await self.codex.start_thread(**params)

    async def warm(self) -> None:
        """Open the foreground conversation before the first request so it pays no startup cost."""
        try:
            await self._foreground()
        except Exception as exc:
            log.warning("could not pre-warm the %s session: %s", self.settings.agent, exc)

    async def _foreground(self) -> Runner:
        async with self.foreground_lock:
            return await self._foreground_locked()

    async def _foreground_locked(self) -> Runner:
        runner = self.runner
        if runner and self._should_retire(runner):
            self.runner = None
            asyncio.create_task(self._retire(runner))
            runner = None
        if runner is None:
            instructions = prompt.developer_instructions(self.memory.prompt_block(), self.memory.skills())
            home = str(Path.home())
            if self.settings.agent == "claude":
                runner = await self.claude_runner(kind="foreground", cwd=home, instructions=instructions,
                                                  model=self.settings.extra.get("claude_model"), effort=self.settings.effort)
            else:
                await self._ensure_codex()
                thread_id = await self.new_thread(instructions=instructions, cwd=home, model=self.model)
                runner = Runner(self, thread_id, kind="foreground", cwd=home)
            self.runner = runner
            self.emit({"event": "thread", "thread_id": runner.thread_id, "agent": self.settings.agent})
        return runner

    async def claude_runner(self, **options):
        from .claude import ClaudeRunner  # optional dependency: claude-agent-sdk
        runner = ClaudeRunner(self, **options)
        await runner.start()
        return runner

    def _should_retire(self, runner: Runner) -> bool:
        idle = time.time() - runner.last_used > self.settings.idle_minutes * 60
        full = runner.context_tokens > self.settings.context_ceiling
        new_day = datetime.fromtimestamp(runner.started).date() != datetime.now().date()
        return idle or full or new_day

    async def retire(self) -> None:
        """Start a fresh conversation now (the app's "New conversation")."""
        if self.runner:
            runner, self.runner = self.runner, None
            asyncio.create_task(self._retire(runner))
        asyncio.create_task(self.warm())

    async def _retire(self, runner: Runner) -> None:
        episode = self.memory.start_episode("(reflection before closing the conversation)", source="system", kind="reflection",
                                            thread_id=runner.thread_id)
        turn = Turn(prompt.REFLECT, "system", episode, SpeechGate(None, False), time.monotonic())
        try:
            await runner.run(turn, prompt.REFLECT, effort="low")
        finally:
            self.memory.finish_episode(episode, reply=turn.text, tools=turn.tools, timings=turn.timings,
                                       status=turn.status, tokens=turn.tokens)
            await runner.close()

    async def ask(self, text: str, *, source: str = "text", speak: bool | None = None, t0: float | None = None,
                  ref: str | None = None, timings: dict | None = None) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("Empty request")
        if self.runner and self.runner.turn:
            await self.interrupt()
        async with self.lock:
            speak = self.settings.speech if speak is None else speak
            if self.speaker:
                self.speaker.stop()
            episode = self.memory.start_episode(text, source=source, agent=self.settings.agent)
            turn = Turn(text, source, episode, SpeechGate(self.speaker, speak), t0 or time.monotonic(),
                        timings=dict(timings or {}))  # voice: endpoint and transcribe, measured before the turn
            self.current = turn
            self.emit({"event": "turn.started", "episode": episode, "request": text, "source": source, "speak": speak, "ref": ref})
            try:
                runner = await self._foreground()
                turn.mark("thread_ready")
                await runner.run(turn, text, effort=self.settings.effort)
            except (CodexError, ConnectionError, OSError) as exc:
                turn.status, turn.error = "failed", str(exc)
            self.memory.finish_episode(episode, reply=turn.text, tools=turn.tools, timings=turn.timings, status=turn.status,
                                       tokens=turn.tokens, thread_id=self.runner.thread_id if self.runner else None)
            result = {"event": "turn.completed", "episode": episode, "status": turn.status, "reply": turn.text,
                      "error": turn.error, "timings": turn.timings, "tokens": turn.tokens,
                      "spoken": bool(turn.gate.spoken)}
            self.emit(result)
            self._check_budget()
            return result

    async def interrupt(self) -> None:
        if self.speaker:
            self.speaker.stop()
        if self.runner:
            await self.runner.interrupt()

    def mark_first_audio(self) -> None:
        turn = self.current
        if turn and turn.status == "running":
            turn.mark("first_audio")

    def _check_budget(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        used = self.memory.tokens_today()["billable"]
        if used > self.settings.daily_token_budget and self.budget_warned != today:
            self.budget_warned = today
            message = f"Heads up: Omni has used about {used // 1000}k tokens today, over your daily budget."
            self.emit({"event": "budget", "used": used, "budget": self.settings.daily_token_budget, "message": message})
            if self.speaker:
                self.speaker.say(message)

    # ---- tools and approvals ----------------------------------------------------

    async def call_tool(self, name: str, arguments: dict, turn: Turn | None, runner: Runner) -> dict:
        spec = self.tools.get(name)
        if spec and spec.ask:
            approved = await self.decide_ask(tools.approval_text(spec, arguments), turn)
            if not approved:
                return {"success": False, "contentItems": [{"type": "inputText", "text": "Nick declined this action."}]}
        loop = asyncio.get_running_loop()
        ctx = tools.Context(
            memory=self.memory, settings=self.settings, episode=turn.episode if turn else None, cwd=runner.cwd,
            say=lambda text: loop.call_soon_threadsafe(self.speaker.say, text) if self.speaker else None,
            ask=lambda question: asyncio.run_coroutine_threadsafe(self.prompt("question", question), loop).result(45),
            delegate=lambda **kw: asyncio.run_coroutine_threadsafe(self.delegate(parent=turn, **kw), loop).result(30),
            extras={"delegations": lambda: [d.public() for d in self.delegations.values()]},
        )
        ok, text = await asyncio.to_thread(tools.run, name, arguments, ctx)
        if name.startswith(("memory_", "skill_")) and ok:
            self.emit({"event": "memory.changed"})
        return {"success": ok, "contentItems": [{"type": "inputText", "text": text[:60000]}]}

    async def decide(self, decision, detail: str, turn: Turn | None) -> str:
        if decision.action == ALLOW:
            return "accept"
        if decision.action == DENY:
            self.emit({"event": "denied", "episode": turn.episode if turn else None, "detail": detail, "reason": decision.reason})
            return "decline"
        # The reason is the headline (the spoken line); the exact command follows on its own line for the screen.
        return "accept" if await self.decide_ask(f"{decision.reason}\n{detail}" if decision.reason else detail, turn) else "decline"

    async def decide_ask(self, summary: str, turn: Turn | None) -> bool:
        if turn:
            turn.mark("approval_asked")
        answer = await self.prompt("approval", summary)
        return answer == "accept"

    # ---- delegation -----------------------------------------------------------

    async def delegate(self, *, task: str, mode: str, cwd: str, agent: str, parent: Turn | None) -> dict:
        from .tools import terminal
        if mode == "default":
            mode = self.settings.delegate_default
        directory = str(Path(cwd or "~").expanduser())
        if not Path(directory).is_dir():
            return {"error": f"{directory} is not a directory"}
        ident = f"d{int(time.time()) % 100000}{len(self.delegations)}"
        item = Delegation(ident, task, mode, directory, agent)
        self.delegations[ident] = item
        if mode == "visible":
            command = f"{agent} {shlex.quote(task)}"
            info = await asyncio.to_thread(terminal.open_terminal, tools.Context(settings=self.settings), directory,
                                           command, True, None, None, True)
            item.handle = info["handle"]
            asyncio.create_task(self._watch_visible(item))
        elif agent == "claude":
            item.runner = await self.claude_runner(kind="delegation", cwd=directory, instructions=prompt.SUBAGENT,
                                                   with_tools=False, effort="medium")
            asyncio.create_task(self._run_background(item, parent))
        else:
            await self._ensure_codex()
            thread_id = await self.new_thread(instructions=prompt.SUBAGENT, cwd=directory, model=None, dynamic_tools=False)
            item.runner = Runner(self, thread_id, kind="delegation", cwd=directory)
            asyncio.create_task(self._run_background(item, parent))
        self.emit({"event": "delegation", **item.public()})
        return {"id": ident, "mode": mode, "status": "started",
                "note": "Omni will announce when it finishes." if mode == "background" else "Opened a terminal Nick can watch."}

    async def _run_background(self, item: Delegation, parent: Turn | None) -> None:
        episode = self.memory.start_episode(item.task, source="delegation", kind="delegation",
                                            parent_id=parent.episode if parent else None, thread_id=item.runner.thread_id)
        turn = Turn(item.task, "delegation", episode, SpeechGate(None, False), time.monotonic())
        await item.runner.run(turn, item.task, effort="medium")
        text = turn.text
        match = re.search(r"^SUMMARY:\s*(.+)$", text, re.M)
        item.summary = (match[1] if match else text.split("\n", 1)[0])[:300]
        item.status = "done" if turn.status == "completed" else turn.status
        if len(text) > 700:
            reports = config.REPORTS
            reports.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", item.task.lower())[:48].strip("-")
            path = reports / f"{datetime.now():%Y-%m-%d}-{slug}.md"
            path.write_text(f"# {item.task}\n\n{text}\n")
            item.report = str(path)
        self.memory.finish_episode(episode, reply=text, tools=turn.tools, timings=turn.timings, status=turn.status, tokens=turn.tokens)
        await item.runner.close()
        self.emit({"event": "delegation", **item.public()})
        if self.speaker and self.settings.speech:
            self.speaker.say(f"Background task finished. {item.summary}" if item.status == "done"
                             else f"The background task {item.status}.")

    async def _watch_visible(self, item: Delegation) -> None:
        from .tools.terminal import JOBS
        result = JOBS / f"{item.handle}.json"
        while not result.exists():
            await asyncio.sleep(3)
        item.status = "done"
        self.emit({"event": "delegation", **item.public()})

    async def stop_delegation(self, ident: str) -> None:
        item = self.delegations.get(ident)
        if item and item.runner:
            await item.runner.interrupt()
            item.status = "stopped"
            self.emit({"event": "delegation", **item.public()})


def pick_fast_model(models: list[dict]) -> str | None:
    """Voice turns want the fastest current model; fall back to the CLI default."""
    for model in models:
        if model.get("id", "").endswith("-luna") and not model.get("hidden"):
            return model["id"]
    return next((m["id"] for m in models if m.get("isDefault")), None)

