"""Moonshine v2 streaming speech to text (Useful Sensors, MIT, CPU; the `moonshine-voice` package).

The encoder runs on the audio while the user is still talking, so when the turn
ends only the last fraction of a second and one decoder pass are left. All native
calls happen on one worker thread per engine; `feed()` on the mic thread only
appends bytes. The worker sleeps on its queue between captures.

ONNX Runtime runs single-threaded here: all cores cut a long sentence's final pass
from ~300 ms to ~100 ms under load, but spin at 400-700% CPU while anyone in the
room is talking. Set MOONSHINE_ORT_SINGLE_THREAD=0 in omnid's environment to trade.

The model is downloaded on first use to ~/.local/share/omni/models/moonshine/
(sizes and CRC32C checksums pinned in the package's native catalog).
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import weakref
from pathlib import Path

import numpy as np

os.environ.setdefault("MOONSHINE_ORT_SINGLE_THREAD", "1")  # read when the model loads

log = logging.getLogger("omni.stt")
RATE = 16000
STEP = int(0.25 * RATE)  # new samples between streaming passes, at least
MODEL_DIR = Path.home() / ".local/share/omni/models/moonshine"
ARCHS = {"tiny": "TINY_STREAMING", "small": "SMALL_STREAMING", "medium": "MEDIUM_STREAMING"}
NEVER = 1e9  # the package's own update cadence; we decide when passes run
OPTIONS = {"transcription_interval": "0.1"}  # its native gate (default 0.5 s) would skip every other pass


class Moonshine:
    def __init__(self, settings):
        self.name = settings.stt_model or "small"
        if self.name not in ARCHS:
            raise ValueError(f"unknown Moonshine model {self.name!r}; use one of {', '.join(ARCHS)}")
        self.settings = settings
        self.model = None
        self.fallback = None
        self.error = ""
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()

    @property
    def ready(self) -> bool:
        return self.model is not None or (self.fallback is not None and self.fallback.ready)

    def load(self) -> None:
        started = time.monotonic()
        try:
            self.model = self._open()
            log.info("moonshine %s ready in %.1fs", self.name, time.monotonic() - started)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("moonshine %s failed to load; using Whisper instead", self.name)
            from .stt import WhisperBatch  # noqa: PLC0415 (circular at import time)
            self.fallback = WhisperBatch(self.settings)
        # Started either way, so a capture that began while loading still gets its (empty) answer.
        threading.Thread(target=self._work, name="moonshine", daemon=True).start()
        if self.fallback is not None:
            self.fallback.load()

    def _open(self):
        from moonshine_voice import ModelArch  # noqa: PLC0415 (heavy; loaded on the loader thread)
        from moonshine_voice.download import download_model_from_info, find_model_info  # noqa: PLC0415
        from moonshine_voice.transcriber import Transcriber  # noqa: PLC0415
        arch = ModelArch[ARCHS[self.name]]
        path, _ = download_model_from_info(find_model_info("en", arch), cache_root=MODEL_DIR,
                                           on_progress=lambda *_: None)
        return Transcriber(path, arch, update_interval=NEVER, options=OPTIONS)

    def start(self):
        return self.fallback.start() if self.fallback is not None else MoonshineStream(self)

    def transcribe(self, audio: bytes) -> str:
        stream = self.start()
        stream.feed(audio)
        return stream.finish()

    # ---- worker thread ------------------------------------------------------------

    def _work(self) -> None:
        while True:
            kind, state = self._jobs.get()
            try:
                if kind == "close":
                    self._close(state)
                else:
                    self._pass(state, final=kind == "finish")
            except Exception:
                log.exception("moonshine %s failed", kind)
                if kind == "finish":
                    self._close(state)
            if kind == "finish":
                state.done.set()

    def _pass(self, state: _State, final: bool) -> None:
        with state.lock:
            chunks, state.pending, state.samples, state.queued = state.pending, [], 0, False
        if state.closed or self.model is None:
            return
        started = time.monotonic()
        if state.native is None:
            state.native = self.model.create_stream(update_interval=NEVER)
            state.native.start()
        if chunks:
            samples = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
            state.native.add_audio(samples.tolist(), RATE)
        transcript = state.native.stop() if final else state.native.update_transcription()
        if transcript is not None:
            state.text = clean(" ".join(line.text for line in transcript.lines))
        # A pass must cover at least as much audio as it took, or a slow machine falls further behind each time.
        state.step = max(STEP, int((time.monotonic() - started) * RATE))
        if final:
            self._close(state)

    @staticmethod
    def _close(state: _State) -> None:
        state.closed = True
        if state.native is not None:
            state.native.close()
            state.native = None


class _State:
    """What the worker owns for one capture; outlives the stream object until closed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending: list[bytes] = []
        self.samples = 0
        self.step = STEP
        self.queued = False
        self.native = None
        self.text = ""
        self.closed = False
        self.done = threading.Event()


class MoonshineStream:
    def __init__(self, engine: Moonshine):
        self._jobs = engine._jobs
        self._state = _State()
        # An abandoned capture is simply dropped by the mic loop; stop its passes and free it then.
        weakref.finalize(self, _abandon, self._jobs, self._state)

    def feed(self, chunk: bytes) -> None:
        state = self._state
        with state.lock:
            state.pending.append(chunk)
            state.samples += len(chunk) // 2
            if state.queued or state.samples < state.step:
                return
            state.queued = True
        self._jobs.put(("update", state))

    def partial(self) -> str:
        return self._state.text

    def finish(self) -> str:
        self._jobs.put(("finish", self._state))
        self._state.done.wait()
        return self._state.text


def _abandon(jobs: queue.SimpleQueue, state: _State) -> None:
    state.closed = True  # passes already queued return at once
    jobs.put(("close", state))


def clean(text: str) -> str:
    """Undo Moonshine's quirks: "to-morrow", doubled commas, spacing between lines, and CJK hallucinations."""
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text):
        return ""  # the English model sometimes turns noise into Chinese
    text = re.sub(r"\bto-(morrow|day|night)\b", r"to\1", text, flags=re.I)
    text = re.sub(r"([,.!?]),+", r"\1", text)
    return " ".join(text.split())
