"""Always-on microphone loop: wake word, push-to-talk, continuous mode, and barge-in.

One `pw-record` stream feeds 32 ms chunks to Silero VAD and 80 ms frames to
openWakeWord. An utterance ends after `end_silence` seconds of trailing silence,
is transcribed locally with faster-whisper, and goes straight to the session.
While Omni is speaking, sustained speech on the mic is a barge-in: playback
stops, the turn is interrupted, and the new utterance is captured (with the
pre-roll, so the first word is not lost). Barge-in relies on echo cancellation
(PipeWire's echo-cancel module or the speakerphone's own) so Omni does not
interrupt itself; see docs/voice.md.

Needs: numpy, openwakeword, pysilero-vad, faster-whisper (pip), pw-record.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeModel
from pysilero_vad import SileroVoiceActivityDetector

log = logging.getLogger("omni.listen")
RATE = 16000
CHUNK = 512            # Silero's window: 32 ms
WAKE_FRAME = 1280      # openWakeWord's frame: 80 ms
PREROLL = int(0.6 * RATE / CHUNK)
SPEECH_PROB = 0.5
BARGE_CHUNKS = 8       # ~256 ms of sustained speech while Omni talks
START_CHUNKS = 6       # ~190 ms to start a continuous-mode utterance
MAX_SECONDS = 30
NO_SPEECH_SECONDS = 5
WAKE_PHRASES = re.compile(r"^\s*(hey[, ]+)?(omni|jarvis|omi)[,.!]?\s*", re.I)
VOXTYPE_STATE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voxtype/state"


class VoiceLoop:
    def __init__(self, daemon, loop: asyncio.AbstractEventLoop):
        if not shutil.which("pw-record"):
            raise RuntimeError("pw-record is missing")
        self.daemon, self.loop = daemon, loop
        settings = daemon.settings
        self.settings = settings
        self.wake_enabled = settings.wake_word
        self.continuous = False
        self.barge_in = settings.extra.get("barge_in", True)
        self.speaking = False
        self._ptt = threading.Event()
        self._stop_capture = threading.Event()
        self._once: asyncio.Future | None = None
        self._capturing = False
        self._purpose = "request"
        self._transcriber = ThreadPoolExecutor(1, thread_name_prefix="whisper")
        self.vad = SileroVoiceActivityDetector()
        wake_model = str(Path(settings.wake_model).expanduser()) if "/" in settings.wake_model else settings.wake_model
        self.wake = WakeModel(wakeword_models=[wake_model], inference_framework="onnx") if settings.wake_word else None
        self.whisper: WhisperModel | None = None
        self.mic: subprocess.Popen | None = None
        self.last_error = ""

    # ---- control from the daemon (event-loop thread) --------------------------------

    def start(self) -> None:
        threading.Thread(target=self._load_whisper, name="whisper-load", daemon=True).start()
        threading.Thread(target=self._run_forever, name="mic", daemon=True).start()

    def push_to_talk(self) -> None:
        """Hotkey: start listening now, or finish the utterance if already listening."""
        if self._capturing:
            self._stop_capture.set()
        else:
            self._ptt.set()

    def set_speaking(self, speaking: bool) -> None:
        self.speaking = speaking

    async def listen_once(self, timeout: float) -> str:
        """Capture one short reply (approvals, clarifying questions) without sending it to the agent."""
        self._once = self.loop.create_future()
        self._ptt.set()
        try:
            return await asyncio.wait_for(self._once, timeout)
        except asyncio.TimeoutError:
            return ""
        finally:
            self._once = None
            if self._capturing and self._purpose == "answer":
                self._stop_capture.set()  # answered another way (click); drop what the mic is still hearing

    def describe(self) -> dict:
        return {"available": True, "wake": self.wake_enabled, "wake_model": self.settings.wake_model,
                "continuous": self.continuous, "barge_in": self.barge_in, "capturing": self._capturing,
                "whisper": self.settings.whisper_model, "whisper_ready": self.whisper is not None, "error": self.last_error}

    # ---- mic thread ---------------------------------------------------------------

    def _load_whisper(self) -> None:
        started = time.monotonic()
        self.whisper = WhisperModel(self.settings.whisper_model, device="cpu", compute_type="int8")
        log.info("whisper %s ready in %.1fs", self.settings.whisper_model, time.monotonic() - started)

    def _run_forever(self) -> None:
        while True:
            try:
                self._run()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("mic loop failed; restarting in 3 s")
            time.sleep(3)

    def _open_mic(self) -> subprocess.Popen:
        argv = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--raw", "-"]
        if target := self.settings.extra.get("mic_target"):
            argv[1:1] = ["--target", str(target)]
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _chunks(self):
        self.mic = self._open_mic()
        assert self.mic.stdout
        size = CHUNK * 2
        try:
            while True:
                data = self.mic.stdout.read(size)
                while data and len(data) < size:
                    more = self.mic.stdout.read(size - len(data))
                    if not more:
                        break
                    data += more
                if len(data) < size:
                    raise RuntimeError("microphone stream ended")
                yield data
        finally:
            self.mic.kill()

    def _run(self) -> None:
        preroll: collections.deque[bytes] = collections.deque(maxlen=PREROLL)
        wake_buffer = bytearray()
        voiced_run = 0
        dictating, next_check = False, 0.0
        for chunk in self._chunks():
            preroll.append(chunk)
            probability = self.vad.process_chunk(chunk)
            voiced_run = voiced_run + 1 if probability >= SPEECH_PROB else 0
            now = time.monotonic()
            if now >= next_check:
                dictating, next_check = self._dictating(), now + 0.5
            if dictating:
                continue  # Voxtype dictation owns the mic; never treat it as a request
            trigger = None
            if self._ptt.is_set():
                self._ptt.clear()
                trigger = "hotkey"
            elif self.speaking:
                if self.barge_in and voiced_run >= BARGE_CHUNKS:
                    trigger = "barge-in"
                    self._call(self.daemon.session.interrupt())
            elif self.continuous and voiced_run >= START_CHUNKS:
                trigger = "continuous"
            elif self.wake is not None and self.wake_enabled:
                wake_buffer += chunk
                if len(wake_buffer) >= WAKE_FRAME * 2:
                    frame = np.frombuffer(bytes(wake_buffer[:WAKE_FRAME * 2]), dtype=np.int16)
                    del wake_buffer[:WAKE_FRAME * 2]
                    scores = self.wake.predict(frame)
                    if max(scores.values(), default=0) >= self.settings.wake_threshold:
                        self.wake.reset()
                        trigger = "wake"
            if trigger:
                log.info("listening (%s)", trigger)
                keep = list(preroll) if trigger in ("barge-in", "continuous") else []
                self._purpose = "answer" if self._once is not None else "request"
                self._capture(keep, heard_speech=trigger in ("barge-in", "continuous"))
                preroll.clear()
                wake_buffer.clear()
                voiced_run = 0
                self.vad.reset()

    def _capture(self, initial: list[bytes], heard_speech: bool) -> None:
        self._capturing = True
        self._stop_capture.clear()
        self.loop.call_soon_threadsafe(self.daemon.set_listening, True)
        audio = bytearray(b"".join(initial))
        started = time.monotonic()
        silence = 0.0
        chunk_seconds = CHUNK / RATE
        for chunk in self._chunks_continuing():
            audio += chunk
            if self.vad.process_chunk(chunk) >= SPEECH_PROB:
                heard_speech, silence = True, 0.0
            else:
                silence += chunk_seconds
            elapsed = time.monotonic() - started
            if self._stop_capture.is_set() or elapsed > MAX_SECONDS:
                break
            if heard_speech and silence >= self.settings.end_silence:
                break
            if not heard_speech and elapsed > NO_SPEECH_SECONDS:
                audio.clear()
                break
        ended = time.monotonic()
        self._capturing = False
        self.loop.call_soon_threadsafe(self.daemon.set_listening, False)
        if audio:
            self._transcriber.submit(self._finish, bytes(audio), ended, self._purpose)
        elif self._once and not self._once.done():
            self.loop.call_soon_threadsafe(self._resolve_once, "")

    def _chunks_continuing(self):
        assert self.mic and self.mic.stdout
        size = CHUNK * 2
        while True:
            data = self.mic.stdout.read(size)
            if len(data) < size:
                return
            yield data

    def _finish(self, audio: bytes, ended: float, purpose: str) -> None:
        text = self.transcribe(audio)
        log.info("heard %r in %.2fs", text, time.monotonic() - ended)
        if purpose == "answer":
            self.loop.call_soon_threadsafe(self._resolve_once, text)  # dropped if already answered
            return
        text = WAKE_PHRASES.sub("", text).strip()
        if len(text) < 2:
            return
        self.loop.call_soon_threadsafe(self.daemon.set_listening, False, text)
        self._call(self.daemon.session.ask(text, source="voice", speak=True, t0=ended))

    def _resolve_once(self, text: str) -> None:
        if self._once and not self._once.done():
            self._once.set_result(text)

    def transcribe(self, audio: bytes) -> str:
        while self.whisper is None:
            time.sleep(0.1)
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.whisper.transcribe(samples, language="en", beam_size=1, vad_filter=False,
                                              without_timestamps=True, condition_on_previous_text=False)
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _call(self, coroutine) -> None:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        future.add_done_callback(lambda f: not f.cancelled() and f.exception()
                                 and log.warning("voice request failed: %s", f.exception()))

    @staticmethod
    def _dictating() -> bool:
        try:
            return VOXTYPE_STATE.read_text().strip() == "recording"
        except OSError:
            return False
