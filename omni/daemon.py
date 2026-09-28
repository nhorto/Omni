"""omnid: the long-running user service. Owns the agent, tools, memory, speech, and mic.

Clients speak JSON lines over `$XDG_RUNTIME_DIR/omni/omnid.sock`:
  request  {"id": 1, "op": "ask", "text": "...", "speak": false}
  response {"id": 1, "ok": true, "result": ...}   (or "ok": false, "error": "...")
  event    {"event": "delta", "text": "...", ...}   (streamed to `subscribe`rs and to the asker)
See `OPS` for every operation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from . import config, reminders
from .memory import Entry, Memory
from .policy import Policy
from .session import Session
from .voice.speak import Speaker, elevenlabs_key

log = logging.getLogger("omni.daemon")
PROMPT_TIMEOUT = 30
YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "go", "go ahead", "do it", "approve", "approved", "confirm", "please do"}


class Daemon:
    def __init__(self, settings=None):
        self.settings = settings or config.load_settings()
        self.memory = Memory()
        self.policy = Policy.load()
        self.subscribers: set[asyncio.Queue] = set()
        self.prompts: dict[str, tuple[dict, asyncio.Future]] = {}
        self.speaker: Speaker | None = None
        self.session: Session | None = None
        self.voice = None
        self.flags = {"thinking": False, "working": 0, "listening": False, "speaking": False}
        self.last_state = ""
        self.detail = ""

    # ---- lifecycle ------------------------------------------------------------

    async def run(self) -> None:
        config.private_dir(config.RUNTIME)
        migrated = self.memory.migrate_legacy()
        if migrated:
            log.info("migrated %d legacy records", migrated)
        self._migrate_legacy_settings()
        self.speaker = Speaker(self.settings, on_state=self._speaking)
        self.speaker.start()
        self.session = Session(self.settings, self.memory, self.policy, self.emit, self.prompt, self.speaker)
        await self.session.start()
        asyncio.create_task(self.session.warm())
        self._start_voice()
        if config.SOCKET.exists():
            config.SOCKET.unlink()
        server = await asyncio.start_unix_server(self._client, path=str(config.SOCKET), limit=16 * 1024 * 1024)
        config.SOCKET.chmod(0o600)
        self._set_state()
        asyncio.create_task(self._reminder_loop())
        log.info("omnid ready on %s (model %s)", config.SOCKET, self.session.model)
        async with server:
            await server.serve_forever()

    def _start_voice(self) -> None:
        try:
            from .voice.listen import VoiceLoop
        except ImportError as exc:
            log.info("voice input unavailable (%s); text and hotkeys still work", exc)
            return
        try:
            self.voice = VoiceLoop(self, asyncio.get_running_loop())
            self.voice.start()
        except Exception as exc:  # missing mic, model download failure, etc.
            log.warning("voice input failed to start: %s", exc)
            self.voice = None

    def _migrate_legacy_settings(self) -> None:
        """Carry the ElevenLabs voice over from the old Omi settings once."""
        legacy = Path.home() / ".config/omi/settings.json"
        target = config.CONFIG / "config.toml"
        if target.exists() or not legacy.is_file():
            return
        try:
            old = json.loads(legacy.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if old.get("elevenlabs_voice_id"):
            config.save_setting("elevenlabs_voice_id", old["elevenlabs_voice_id"])
            self.settings.elevenlabs_voice_id = old["elevenlabs_voice_id"]
        if old.get("voice_model"):
            config.save_setting("piper_voice", old["voice_model"])
            self.settings.piper_voice = old["voice_model"]

    async def _reminder_loop(self) -> None:
        from .tools.reminders import database
        while True:
            try:
                await asyncio.to_thread(lambda: reminders.deliver_due(database(), self.settings.extra))
            except Exception as exc:
                log.warning("reminder delivery failed: %s", exc)
            await asyncio.sleep(20)

    # ---- events and state -----------------------------------------------------------

    def emit(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "turn.started":
            self.flags["thinking"], self.flags["working"] = True, 0
            self.detail = event.get("request", "")[:120]
        elif kind == "tool" and event.get("kind") == "foreground":
            self.flags["working"] += 1 if event.get("status") == "running" else -1
            self.flags["working"] = max(0, self.flags["working"])
            self.detail = event.get("label", "")[:120]
        elif kind == "turn.completed":
            self.flags["thinking"], self.flags["working"] = False, 0
            self.detail = ""
        event.setdefault("at", time.time())
        for queue in list(self.subscribers):
            queue.put_nowait(event)
        self._set_state()

    def _speaking(self, speaking: bool) -> None:
        self.flags["speaking"] = speaking
        if speaking and self.session:
            self.session.mark_first_audio()
        if self.voice:
            self.voice.set_speaking(speaking)
        self.emit({"event": "speaking", "value": speaking})

    def set_listening(self, listening: bool, transcript: str = "") -> None:
        self.flags["listening"] = listening
        self.emit({"event": "listening", "value": listening, "transcript": transcript})

    def state(self) -> str:
        if self.prompts:
            return "awaiting_approval"
        if self.flags["listening"]:
            return "listening"
        if self.flags["speaking"]:
            return "speaking"
        if self.flags["working"]:
            return "working"
        if self.flags["thinking"]:
            return "thinking"
        return "idle"

    def _set_state(self) -> None:
        state = self.state()
        if state == self.last_state and state not in ("working", "listening"):
            return
        self.last_state = state
        payload = {"state": state, "detail": self.detail, "agent": self.settings.agent, "updated_at": time.time(),
                   "continuous": bool(self.voice and self.voice.continuous), "wake": bool(self.voice and self.voice.wake_enabled)}
        if state == "idle":
            payload["tokens_today"] = self.memory.tokens_today()["billable"]
        temporary = config.STATUS.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload) + "\n")
        temporary.chmod(0o600)
        temporary.replace(config.STATUS)
        for queue in list(self.subscribers):
            queue.put_nowait({"event": "state", **payload})

    # ---- approvals and questions -------------------------------------------------------

    async def prompt(self, kind: str, text: str) -> str:
        """Ask Nick through every surface at once; first answer wins. Approvals return 'accept' or 'decline'."""
        ident = uuid.uuid4().hex[:8]
        future = asyncio.get_running_loop().create_future()
        info = {"id": ident, "kind": kind, "text": text}
        self.prompts[ident] = (info, future)
        self.emit({"event": "prompt", **info})
        tasks = [asyncio.create_task(self._notify_prompt(ident, kind, text))]
        if self.voice and self.settings.speech:
            tasks.append(asyncio.create_task(self._voice_prompt(ident, kind, text)))
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            answer = "decline" if kind == "approval" else ""
        finally:
            self.prompts.pop(ident, None)
            for task in tasks:
                task.cancel()
            self.emit({"event": "prompt.resolved", "id": ident})
        return answer

    def answer(self, ident: str, value: str) -> bool:
        entry = self.prompts.get(ident)
        if not entry or entry[1].done():
            return False
        info, future = entry
        if info["kind"] == "approval":
            value = "accept" if value in ("accept", "approve", "yes") else "decline"
        future.set_result(value)
        return True

    async def _notify_prompt(self, ident: str, kind: str, text: str) -> None:
        # Approvals are answered from the notification's Approve/Cancel actions (or by voice);
        # raising the popover for them covers whatever Nick is doing. Questions need a typed
        # or spoken answer, so those still raise it. A no-op if the app is not running.
        if kind != "approval" and shutil.which("gapplication"):
            subprocess.Popen(["gapplication", "action", "dev.omni.Omni", "quick-ask"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not shutil.which("notify-send"):
            return
        argv = ["notify-send", "--app-name=Omni", "--urgency=critical", f"--expire-time={PROMPT_TIMEOUT * 1000}", "--wait"]
        if kind == "approval":
            argv += ["--action=accept=Approve", "--action=decline=Cancel", "--", "Omni wants to", text]
        else:
            argv += ["--", "Omni asks", text + "\n(answer by voice or in the Omni app)"]
        process = await asyncio.create_subprocess_exec(*argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            output, _ = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            raise
        choice = output.decode().strip()
        if choice in ("accept", "decline"):
            self.answer(ident, choice)

    async def _voice_prompt(self, ident: str, kind: str, text: str) -> None:
        self.speaker.say(spoken_prompt(kind, text))
        try:
            while self.speaker.busy:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.speaker.stop()  # answered by click while still asking: stop talking at once
            raise
        heard = await self.voice.listen_once(timeout=PROMPT_TIMEOUT - 5)
        if not heard:
            return
        if kind == "approval":
            words = heard.lower().strip(" .!?,")
            self.answer(ident, "accept" if words in YES or words.startswith(("yes", "yeah", "go ahead", "do it")) else "decline")
        else:
            self.answer(ident, heard)

    # ---- clients ---------------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lock = asyncio.Lock()

        async def send(message: dict) -> None:
            async with lock:
                writer.write((json.dumps(message, default=str) + "\n").encode())
                await writer.drain()

        tasks: set[asyncio.Task] = set()
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    await send({"ok": False, "error": "invalid JSON"})
                    continue
                task = asyncio.create_task(self._handle(request, send))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            writer.close()

    async def _handle(self, request: dict, send) -> None:
        ident, op = request.get("id"), request.get("op", "")
        handler = OPS.get(op)
        try:
            if handler is None:
                raise ValueError(f"unknown op {op!r}")
            result = await handler(self, request, send)
            await send({"id": ident, "ok": True, "result": result})
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception as exc:
            if not isinstance(exc, (ValueError, KeyError)):
                log.exception("op %s failed", op)
            await send({"id": ident, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def status(self) -> dict:
        return {"state": self.state(), "agent": self.settings.agent,
                "model": (self.settings.extra.get("claude_model") or "default") if self.settings.agent == "claude"
                else self.session.model if self.session else None,
                "thread": self.session.runner.thread_id if self.session and self.session.runner else None,
                "context_tokens": self.session.runner.context_tokens if self.session and self.session.runner else 0,
                "tokens_today": self.memory.tokens_today(), "budget": self.settings.daily_token_budget,
                "prompts": [info for info, _ in self.prompts.values()],
                "delegations": [d.public() for d in self.session.delegations.values()] if self.session else [],
                "voice": self.voice.describe() if self.voice else {"available": False}}


# ---- operations -------------------------------------------------------------------------

def spoken_prompt(kind: str, text: str) -> str:
    """The line Omni says aloud for a prompt: only the first line, so an email body stays on screen."""
    headline = (text.strip().splitlines() or [""])[0]
    return f"Should I {headline.rstrip('.:')}?" if kind == "approval" else headline


async def op_ask(daemon: Daemon, request: dict, send) -> dict:
    ref = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    daemon.subscribers.add(queue)
    episode = None
    task = asyncio.create_task(daemon.session.ask(request["text"], source=request.get("source", "text"),
                                                   speak=request.get("speak"), ref=ref,
                                                   t0=request.get("t0")))
    try:
        while not task.done() or not queue.empty():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                continue
            event = getter.result()
            if event.get("event") == "turn.started" and event.get("ref") == ref:
                episode = event["episode"]
            if episode is not None and event.get("episode") == episode or event.get("event") in ("prompt", "prompt.resolved"):
                await send(event)
        return await task
    finally:
        daemon.subscribers.discard(queue)


async def op_subscribe(daemon: Daemon, request: dict, send) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    daemon.subscribers.add(queue)
    await send({"event": "state", "state": daemon.state(), "detail": daemon.detail})
    try:
        while True:
            await send(await queue.get())
    finally:
        daemon.subscribers.discard(queue)


async def op_status(daemon, request, send):
    return daemon.status()


async def op_interrupt(daemon, request, send):
    await daemon.session.interrupt()
    return True


async def op_answer(daemon, request, send):
    return daemon.answer(request["prompt"], request.get("value", ""))


async def op_new_thread(daemon, request, send):
    await daemon.session.retire()
    return True


async def op_listen(daemon, request, send):
    if not daemon.voice:
        raise RuntimeError("Voice input is not available; see `omni doctor`")
    daemon.voice.push_to_talk()
    return True


async def op_voice(daemon, request, send):
    if not daemon.voice:
        raise RuntimeError("Voice input is not available; see `omni doctor`")
    setting = request.get("setting")
    value = request.get("value")
    if setting == "continuous":
        daemon.voice.continuous = (not daemon.voice.continuous) if value is None else bool(value)
    elif setting == "wake":
        daemon.voice.wake_enabled = (not daemon.voice.wake_enabled) if value is None else bool(value)
    daemon.last_state = ""
    daemon._set_state()
    return daemon.voice.describe()


async def op_stop_speaking(daemon, request, send):
    daemon.speaker.stop()
    return True


async def op_say(daemon, request, send):
    daemon.speaker.say(request["text"])
    return True


async def op_memory_list(daemon, request, send):
    return {store: [asdict(e) for e in daemon.memory.entries(store)] for store in ("user", "memory")}


async def op_memory_save(daemon, request, send):
    entry = daemon.memory.save(request["store"], request["text"])
    daemon.emit({"event": "memory.changed"})
    return asdict(entry)


async def op_memory_update(daemon, request, send):
    entry = daemon.memory.update(request["entry"], request["text"])
    daemon.emit({"event": "memory.changed"})
    return asdict(entry)


async def op_memory_forget(daemon, request, send):
    entry = daemon.memory.forget(request["entry"])
    daemon.emit({"event": "memory.changed"})
    return asdict(entry)


async def op_memory_restore(daemon, request, send):
    daemon.memory.restore(Entry(**request["entry"]))
    daemon.emit({"event": "memory.changed"})
    return True


async def op_memory_search(daemon, request, send):
    return daemon.memory.search(request["query"])


async def op_skills_list(daemon, request, send):
    return daemon.memory.skills()


async def op_skills_read(daemon, request, send):
    return daemon.memory.read_skill(request["name"])


async def op_skills_write(daemon, request, send):
    path = daemon.memory.skills_dir / request["name"] / "SKILL.md"
    if not path.is_file():
        raise KeyError("No such skill")
    path.write_text(request["text"])
    daemon.emit({"event": "memory.changed"})
    return True


async def op_skills_delete(daemon, request, send):
    text = daemon.memory.delete_skill(request["name"])
    daemon.emit({"event": "memory.changed"})
    return text


async def op_skills_restore(daemon, request, send):
    folder = config.private_dir(daemon.memory.skills_dir / request["name"])
    (folder / "SKILL.md").write_text(request["text"])
    daemon.emit({"event": "memory.changed"})
    return True


async def op_episodes(daemon, request, send):
    return daemon.memory.recent(request.get("limit", 50), request.get("before"))


async def op_episode_delete(daemon, request, send):
    daemon.memory.delete_episode(request["episode"])
    return True


async def op_delegation_stop(daemon, request, send):
    await daemon.session.stop_delegation(request["delegation"])
    return True


async def op_settings_get(daemon, request, send):
    values = asdict(daemon.settings)
    values["elevenlabs_key_set"] = bool(await asyncio.to_thread(elevenlabs_key))
    return values


async def op_settings_set(daemon, request, send):
    key, value = request["key"], request["value"]
    if key not in daemon.settings.__dataclass_fields__ or key == "extra":
        raise KeyError(f"Unknown setting {key}")
    if key == "agent" and value not in ("codex", "claude"):
        raise ValueError("agent must be codex or claude")
    setattr(daemon.settings, key, value)
    config.save_setting(key, value)
    if key == "model":
        daemon.session.model = value or daemon.session.model
    if key == "agent":
        await daemon.session.retire()  # the next request starts on the new adapter; memory carries over
    return True


async def op_policy_get(daemon, request, send):
    path = config.CONFIG / "policy.toml"
    return {"path": str(path), "text": path.read_text() if path.exists() else "", "ask": sorted(daemon.policy.ask_commands)}


async def op_policy_set(daemon, request, send):
    import tomllib
    tomllib.loads(request["text"])  # refuse to save a file that will not load
    path = config.CONFIG / "policy.toml"
    config.private_dir(path.parent)
    path.write_text(request["text"])
    daemon.policy = daemon.session.policy = Policy.load(path)
    return True


async def op_doctor(daemon, request, send):
    from .doctor import checks
    return await asyncio.to_thread(checks)


OPS = {
    "ask": op_ask, "subscribe": op_subscribe, "status": op_status, "interrupt": op_interrupt, "answer": op_answer,
    "new_thread": op_new_thread, "listen": op_listen, "voice": op_voice, "stop_speaking": op_stop_speaking, "say": op_say,
    "memory.list": op_memory_list, "memory.save": op_memory_save, "memory.update": op_memory_update,
    "memory.forget": op_memory_forget, "memory.restore": op_memory_restore, "memory.search": op_memory_search,
    "skills.list": op_skills_list, "skills.read": op_skills_read, "skills.write": op_skills_write,
    "skills.delete": op_skills_delete, "skills.restore": op_skills_restore,
    "episodes": op_episodes, "episode.delete": op_episode_delete, "delegation.stop": op_delegation_stop,
    "settings.get": op_settings_get, "settings.set": op_settings_set, "policy.get": op_policy_get,
    "policy.set": op_policy_set, "doctor": op_doctor,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run omnid, Omni's background service")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.umask(0o077)
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
