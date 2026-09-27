"""Minimal asyncio client for `codex app-server` (JSON-RPC 2.0 over stdio, one JSON object per line).

Generate the full protocol for the installed CLI with
`codex app-server generate-json-schema --experimental --out DIR`. Omni uses the
experimental API for `dynamicTools` (client-side tools called back via
`item/tool/call`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Awaitable, Callable

log = logging.getLogger("omni.codex")

Notification = Callable[[str, dict], None]
ServerRequest = Callable[[str, dict], Awaitable[Any]]


class CodexError(RuntimeError):
    def __init__(self, method: str, error: dict):
        super().__init__(f"{method}: {error.get('message', error)}")
        self.error = error


class CodexAppServer:
    def __init__(self, binary: str = "codex", args: list[str] | None = None):
        self.binary = shutil.which(binary) or binary
        self.args = args or []
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}
        self._reader: asyncio.Task | None = None
        # threadId -> handlers; notifications without a threadId go to `on_global`.
        self.thread_notifications: dict[str, Notification] = {}
        self.thread_requests: dict[str, ServerRequest] = {}
        self.on_global: Notification = lambda method, params: None
        self.on_exit: Callable[[], None] = lambda: None
        self.user_agent = ""

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> dict:
        env = dict(os.environ, RUST_LOG=os.environ.get("RUST_LOG", "warn"))
        self.process = await asyncio.create_subprocess_exec(
            self.binary, "app-server", *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read())
        result = await self.request("initialize", {
            "clientInfo": {"name": "omni", "title": "Omni", "version": "0.2"},
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized")
        self.user_agent = result.get("userAgent", "")
        return result

    async def stop(self) -> None:
        if self.alive:
            assert self.process
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()

    def _send(self, message: dict) -> None:
        if not self.alive:
            raise ConnectionError("codex app-server is not running")
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(message) + "\n").encode())

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    async def request(self, method: str, params: dict | None = None, timeout: float = 60) -> Any:
        self._next_id += 1
        ident = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = (method, future)
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(ident, None)

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("unparsable app-server line: %s", line[:200])
                    continue
                self._dispatch(message)
        finally:
            for method, future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(f"codex app-server exited during {method}"))
            self.on_exit()

    def _dispatch(self, message: dict) -> None:
        if "method" not in message:
            pending = self._pending.get(message.get("id"))
            if pending:
                method, future = pending
                if future.done():
                    return
                if "error" in message:
                    future.set_exception(CodexError(method, message["error"]))
                else:
                    future.set_result(message.get("result"))
            return
        method, params = message["method"], message.get("params") or {}
        thread = params.get("threadId") or (params.get("thread") or {}).get("id")
        if "id" in message:
            asyncio.create_task(self._answer(message["id"], method, params, thread))
            return
        handler = self.thread_notifications.get(thread) if thread else None
        try:
            (handler or self.on_global)(method, params)
        except Exception:  # a buggy handler must not kill the reader
            log.exception("notification handler failed for %s", method)

    async def _answer(self, ident, method: str, params: dict, thread: str | None) -> None:
        handler = self.thread_requests.get(thread) if thread else None
        try:
            if handler is None:
                raise LookupError(f"no handler for {method}")
            result = await handler(method, params)
            self._send({"jsonrpc": "2.0", "id": ident, "result": result})
        except Exception as exc:
            log.warning("server request %s failed: %s", method, exc)
            if self.alive:
                self._send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": str(exc)}})

    # ---- convenience ------------------------------------------------------

    async def models(self) -> list[dict]:
        return (await self.request("model/list", {})).get("data", [])

    async def start_thread(self, **params) -> str:
        result = await self.request("thread/start", params)
        return result["thread"]["id"]

    async def start_turn(self, thread_id: str, text: str, **params) -> str:
        result = await self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}], **params})
        return result["turn"]["id"]

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)

    async def archive(self, thread_id: str) -> None:
        try:
            await self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=10)
        except (CodexError, asyncio.TimeoutError):
            pass
        self.thread_notifications.pop(thread_id, None)
        self.thread_requests.pop(thread_id, None)
