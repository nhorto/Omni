"""Speech to text behind one small interface, so the mic loop does not care which engine runs.

`VoiceLoop` calls `start()` when a capture begins, `feed()` with every 32 ms chunk
(16 kHz mono s16) from the mic thread, and `finish()` on its transcription worker
once the turn has ended. `partial()` is what a streaming engine has heard so far.
"""

from __future__ import annotations

import logging
import re
import time

import numpy as np

log = logging.getLogger("omni.stt")


class WhisperBatch:
    """faster-whisper on the whole clip once the turn has ended."""

    def __init__(self, settings):
        self.name = settings.stt_model or settings.whisper_model
        self.model = None

    @property
    def ready(self) -> bool:
        return self.model is not None

    def load(self) -> None:
        from faster_whisper import WhisperModel  # noqa: PLC0415 (heavy; loaded on the loader thread)
        started = time.monotonic()
        self.model = WhisperModel(self.name, device="cpu", compute_type="int8")
        log.info("whisper %s ready in %.1fs", self.name, time.monotonic() - started)

    def start(self) -> WhisperStream:
        return WhisperStream(self)

    def transcribe(self, audio: bytes) -> str:
        while self.model is None:
            time.sleep(0.1)
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        # The prompt teaches Whisper the name; without it "Omni" comes out as "on me", "Aminee", "I'm Nie".
        segments, _ = self.model.transcribe(samples, language="en", beam_size=1, vad_filter=False,
                                            without_timestamps=True, condition_on_previous_text=False,
                                            initial_prompt="Omni")
        return " ".join(segment.text.strip() for segment in segments).strip()


class WhisperStream:
    def __init__(self, engine: WhisperBatch):
        self.engine = engine
        self.audio = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.audio += chunk

    def partial(self) -> str:
        return ""

    def finish(self) -> str:
        return self.engine.transcribe(bytes(self.audio))


# Names speech-to-text reliably mishears, as Nick says them.
TERMS = [
    (re.compile(r"\b(?:quad|quod|cloud|clawed|clod|claud|claude)[- ]?code\b", re.I), "Claude Code"),
    (re.compile(r"\bcodecs\b", re.I), "Codex"),
    (re.compile(r"\bt[- ]?(?:3|three)[- ]?code\b", re.I), "T3 Code"),
]


def fix_terms(text: str) -> str:
    for pattern, name in TERMS:
        text = pattern.sub(name, text)
    return text


def make_transcriber(settings):
    if settings.stt == "moonshine":
        from .moonshine import Moonshine  # noqa: PLC0415 (only loads the model when chosen)
        return Moonshine(settings)
    return WhisperBatch(settings)
