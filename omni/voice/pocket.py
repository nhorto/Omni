"""Kyutai Pocket TTS as the offline voice: a warm child process that streams 16-bit PCM.

Pocket needs torch, which stays out of omnid's venv. The server (`integration/pocket/server.py`)
runs in `~/.local/share/omni/pocket-venv`, starts the first time it is needed, and exits after
ten idle minutes to give its RAM back. The model loads offline from the cache `--fetch` filled.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("omni.pocket")
HOME = Path.home() / ".local/share/omni"
PYTHON = HOME / "pocket-venv/bin/python"
MODELS = HOME / "models/pocket-tts"
SERVER = Path(__file__).resolve().parents[2] / "integration/pocket/server.py"
HEADER = struct.Struct("<IBI")
AUDIO, END, ERROR, READY = range(4)
IDLE = 600          # seconds warm after the last sentence
RETRY = 60          # seconds before trying again after the server failed
FIRST_AUDIO = 10    # seconds to wait for a sentence's first audio (it may queue behind the one playing)


class Pocket:
    def __init__(self, voice: str = "", python: Path | str = PYTHON, argv: list[str] | None = None, idle: float = IDLE):
        self.argv = argv or [str(python), str(SERVER), *(["--voice", voice] if voice else [])]
        self.idle = idle
        self.rate = 24000
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._streams: dict[int, queue.Queue] = {}
        self._next = 0
        self._failed_at = -RETRY
        self._last_use = time.monotonic()
        self._timer: threading.Timer | None = None

    def installed(self) -> bool:
        return Path(self.argv[0]).is_file() and Path(self.argv[1]).is_file() and shutil.which("pw-play") is not None

    def available(self) -> bool:
        return self.installed() and time.monotonic() - self._failed_at >= RETRY

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Launch the server if it is not running; returns at once (see `wait`)."""
        with self._lock:
            if self._proc and self._proc.poll() is None or not self.available():
                return
            env = {**os.environ, "HF_HUB_CACHE": str(MODELS), "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
            self._ready.clear()
            try:
                self._proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
            except OSError:
                self._failed_at = time.monotonic()
                raise
            threading.Thread(target=self._read, args=(self._proc,), daemon=True, name="pocket-read").start()
        self._touch()

    def wait(self, timeout: float) -> bool:
        return self._ready.wait(timeout) and self.ready

    def stream(self, text: str, wait: float = 0):
        """Iterator of PCM bytes for one sentence; raises RuntimeError if Pocket cannot speak it now."""
        self.start()
        if not self.wait(wait):
            raise RuntimeError("Pocket is not ready" if self.available() else "Pocket is unavailable")
        with self._lock:
            self._next += 1
            request, audio = self._next, queue.Queue()
            self._streams[request] = audio
        self._send({"id": request, "text": text})
        try:
            first = self._take(audio, FIRST_AUDIO)
        except BaseException:
            self._finish(request, cancel=True)
            raise
        if first is None:
            self._finish(request, cancel=False)
            raise RuntimeError("Pocket produced no audio")
        return Stream(self, request, audio, first)

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready.clear()
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()  # the server exits on EOF; kill it if it does not
                proc.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    def _take(self, audio: queue.Queue, timeout: float) -> bytes | None:
        try:
            kind, payload = audio.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("Pocket stopped sending audio") from None
        if kind == ERROR:
            raise RuntimeError(f"Pocket failed: {payload.decode(errors='replace')}")
        return payload if kind == AUDIO else None

    def _finish(self, request: int, cancel: bool) -> None:
        with self._lock:
            self._streams.pop(request, None)
        if cancel:
            try:
                self._send({"cancel": request})
            except RuntimeError:
                pass  # the server is gone, so nothing is generating
        self._touch()

    def _send(self, message: dict) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None:
            raise RuntimeError("Pocket is not running")
        try:
            with self._write_lock:
                proc.stdin.write((json.dumps(message) + "\n").encode())
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Pocket is not running: {exc}") from None

    def _read(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        while (header := stdout.read(HEADER.size)) and len(header) == HEADER.size:
            request, kind, length = HEADER.unpack(header)
            payload = stdout.read(length) if length else b""
            if kind == READY:
                self.rate = json.loads(payload).get("rate", self.rate)
                self._ready.set()
                continue
            with self._lock:
                audio = self._streams.get(request)
            if audio:
                audio.put((kind, payload))
        # The server exited: fail whatever was waiting, and back off unless we stopped it on purpose.
        code = proc.wait()
        for pipe in (proc.stdin, stdout):
            try:
                pipe.close()
            except OSError:
                pass
        with self._lock:
            ours = self._proc is proc
            if ours:
                self._proc = None
                self._ready.set()  # wake anyone in wait(); `ready` is false without a process
            waiting = list(self._streams.values())
        if ours:
            self._failed_at = time.monotonic()
            log.warning("Pocket TTS exited (%s); using Piper for %d s", code, RETRY)
        for audio in waiting:
            audio.put((ERROR, b"the Pocket server exited"))

    def _touch(self) -> None:
        self._last_use = time.monotonic()
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.idle, self._idle_check)
            self._timer.daemon = True
            self._timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            busy = bool(self._streams)
        if busy:
            self._touch()
        elif time.monotonic() - self._last_use >= self.idle - 1:
            log.info("Pocket TTS idle for %d s; stopping it", self.idle)
            self.stop()


class Stream:
    """One sentence's PCM chunks. Closing it early, or dropping it unread, cancels the request."""

    def __init__(self, pocket: Pocket, request: int, audio: queue.Queue, first: bytes):
        self.pocket, self.request, self.audio = pocket, request, audio
        self._first: bytes | None = first
        self._done = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._first is not None:
            chunk, self._first = self._first, None
            return chunk
        if self._done:
            raise StopIteration
        try:
            chunk = self.pocket._take(self.audio, FIRST_AUDIO)
        except BaseException:
            self.close()
            raise
        if chunk is None:
            self._done = True
            self.pocket._finish(self.request, cancel=False)
            raise StopIteration
        return chunk

    def close(self) -> None:
        if not self._done:
            self._done = True
            self.pocket._finish(self.request, cancel=True)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
