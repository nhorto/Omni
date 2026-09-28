"""Smart Turn v3 (pipecat-ai, BSD-2): has the speaker finished, judged from the audio itself?

After a short VAD pause the model is asked once whether the last 8 s sound like a finished
turn. If it says no, the turn goes on until speech resumes and pauses again (another
question), or `turn_max_silence` passes. It is not asked again as the same pause grows: it
was trained on ~0.2 s of trailing silence, and longer silence alone pushes it towards
"finished", mid-thought or not.

The model (8 MB int8 ONNX: a Whisper-tiny encoder and a linear head) is downloaded on first
use to ~/.local/share/omni/models/smart-turn/ and checked against a pinned sha256. Its input
is Whisper's 80-bin log-mel over exactly 8 s, computed here in numpy instead of `transformers`
(the same features to 5e-6). This is faster-whisper's extractor, not imported from it: importing
faster_whisper on this loader thread while the STT loader imports it deadlocks.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

log = logging.getLogger("omni.turn")
RATE = 16000
WINDOW = 8 * RATE        # the model hears the last 8 s, left-padded with zeros
N_FFT, HOP, N_MELS = 400, 160, 80
CHECK = 0.2              # seconds of VAD silence before asking
WAIT = 0.025             # how long the mic thread waits for the answer before taking it on a later chunk
THREADS = 4              # ~15 ms per question on the 8745HS (1 thread: ~45 ms); idle otherwise
ANSWER_SILENCE = 0.7     # replies to a question ("yes", "no") rarely pause; don't wait long when the model doubts
MIN_VOICED = 5           # voiced chunks (160 ms) before asking: a click or breath alone is not a finished turn
MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
MODEL_URL = ("https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/"
             f"f766f81d3cfdf7737ac64aad813d91bbfd56bf93/{MODEL_FILE}")
MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"
MODEL_DIR = Path.home() / ".local/share/omni/models/smart-turn"


def fetch_model(directory: Path = MODEL_DIR) -> Path:
    path = directory / MODEL_FILE
    if path.is_file():
        return path
    directory.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    log.info("downloading %s", MODEL_URL)
    digest = hashlib.sha256()
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response, open(partial, "wb") as out:
        while block := response.read(1 << 16):
            digest.update(block)
            out.write(block)
    if digest.hexdigest() != MODEL_SHA256:
        partial.unlink()
        raise RuntimeError(f"the Smart Turn download has the wrong checksum ({digest.hexdigest()})")
    partial.rename(path)
    return path


def mel_filters() -> np.ndarray:
    """Whisper's (80, 201) Slaney-scale triangular filters."""
    def hz_to_mel(f):
        return np.where(f < 1000, 3 * f / 200, 15 + np.log(np.maximum(f, 1e-9) / 1000) * 27 / np.log(6.4))

    def mel_to_hz(m):
        return np.where(m < 15, 200 * m / 3, 1000 * np.exp((m - 15) * np.log(6.4) / 27))
    edges = mel_to_hz(np.linspace(0, hz_to_mel(np.float64(RATE / 2)), N_MELS + 2))
    ramps = edges[:, None] - np.fft.rfftfreq(N_FFT, 1 / RATE)[None, :]
    lower = -ramps[:-2] / np.diff(edges)[:-1, None]
    upper = ramps[2:] / np.diff(edges)[1:, None]
    return np.maximum(0, np.minimum(lower, upper)) * (2 / (edges[2:] - edges[:-2]))[:, None]


MEL = mel_filters().astype(np.float32)
HANN = np.hanning(N_FFT + 1)[:-1].astype(np.float32)


def log_mel(x: np.ndarray) -> np.ndarray:
    """Whisper's log-mel spectrogram of float32 audio: (80, len(x) // 160)."""
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(x, N_FFT // 2, mode="reflect"), N_FFT)[::HOP]
    power = np.abs(np.fft.rfft(frames * HANN, axis=-1)[:-1]) ** 2
    mel = np.log10(np.maximum(MEL @ power.T, 1e-10))
    return (np.maximum(mel, mel.max() - 8) + 4) / 4


class Model:
    """The ONNX session and its features. `predict(samples)` is p(turn finished) for 16 kHz s16 audio."""

    def __init__(self, path: Path, threads: int = THREADS):
        import onnxruntime as ort  # noqa: PLC0415
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = threads
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")  # no busy-wait between questions
        options.enable_cpu_mem_arena = False  # the arena keeps ~80 MB for a ~1 ms gain
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])

    def features(self, samples: np.ndarray) -> np.ndarray:
        """(1, 80, 800), prepared as the reference does: the last 8 s left-padded with zeros, the waveform
        normalized to zero mean and unit variance, then Whisper's log-mel."""
        x = samples[-WINDOW:].astype(np.float32) / 32768
        x = np.pad(x, (WINDOW - len(x), 0))
        x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
        return log_mel(x)[None].astype(np.float32)

    def predict(self, samples: np.ndarray) -> float:
        return float(self.session.run(None, {"input_features": self.features(samples)})[0].item())


class SmartTurn:
    """End the turn when the model hears a finished turn at a pause, or after `turn_max_silence`.

    Until the model has loaded (or if it cannot), it ends turns like `SilenceTurn`."""

    def __init__(self, settings, model=None):
        self.threshold = settings.turn_threshold
        self.max_silence = self.request_silence = settings.turn_max_silence
        self.end_silence = settings.end_silence
        self.model = model
        self.ready = model is not None
        self.last: dict = {}
        self._worker = ThreadPoolExecutor(1, thread_name_prefix="smart-turn")
        self._pending: Future | None = None
        self._asked = False
        self._voiced = 0
        if model is None:
            threading.Thread(target=self._load, name="smart-turn-load", daemon=True).start()

    def _load(self) -> None:
        try:
            self.model = Model(fetch_model())
            self.model.predict(np.zeros(RATE, dtype=np.int16))  # the first run is slow
        except Exception as exc:  # noqa: BLE001
            log.warning("Smart Turn unavailable, ending turns after %.1f s of silence: %s", self.end_silence, exc)
            self.model = None
        self.ready = True

    def reset(self, purpose: str = "request") -> None:
        self.last = {}
        self._pending = None
        self._asked = False
        self._voiced = 0
        self.max_silence = min(self.request_silence, ANSWER_SILENCE) if purpose == "answer" else self.request_silence

    def update(self, audio: bytearray, prob: float, silence: float) -> bool:
        if silence == 0:
            self._pending, self._asked = None, False  # speech resumed: the next pause gets its own question
            self._voiced += 1
            return False
        if self._voiced < MIN_VOICED:
            return silence >= self.request_silence  # only a blip so far: keep listening for the real start
        if silence >= self.max_silence:
            return True
        if self.model is None:
            return silence >= self.end_silence
        if not self._asked and silence >= CHECK:
            self._asked = True
            samples = np.frombuffer(bytes(audio[-WINDOW * 2:]), dtype=np.int16)
            self._pending = self._worker.submit(self._predict, samples, silence)
            wait([self._pending], WAIT)  # if still thinking, the answer is picked up on a later chunk
        if self._pending is not None and self._pending.done():
            pending, self._pending = self._pending, None
            try:
                return pending.result() >= self.threshold
            except Exception as exc:  # noqa: BLE001
                log.warning("Smart Turn failed, falling back to silence: %s", exc)
                self.model = None
        return False

    def _predict(self, samples: np.ndarray, silence: float) -> float:
        started = time.perf_counter()
        p = self.model.predict(samples)
        ms = (time.perf_counter() - started) * 1000
        self.last = {"p": round(p, 3), "ms": round(ms)}
        log.info("turn: p(finished) %.2f after %.2f s of silence (%.0f ms)", p, silence, ms)
        return p
