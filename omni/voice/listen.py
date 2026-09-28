"""Always-on microphone loop: wake word, push-to-talk, continuous mode, and barge-in.

One `pw-record` stream feeds 32 ms chunks to Silero VAD and 80 ms frames to
openWakeWord. An utterance ends after `end_silence` seconds of trailing silence,
is transcribed locally (`stt.py`), and goes straight to the session. `turn.py`
decides when the speaker has finished.
While Omni is speaking, sustained speech on the mic is a barge-in: playback
stops, the turn is interrupted, and the new utterance is captured (with the
pre-roll, so the first word is not lost). Barge-in relies on echo cancellation
(PipeWire's echo-cancel module or the speakerphone's own) so Omni does not
interrupt itself; see docs/voice.md.

Needs: numpy, openwakeword, pysilero-vad, faster-whisper (pip), pw-record. The
`mic_command` setting replaces pw-record (the bench feeds WAV files through it).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# pysilero-vad bundles libgomp, whose workers busy-wait between our 32 ms calls: ~45% of a core
# while idle on Linux. Passive waiting brings the always-on loop to a few percent; libgomp reads
# this when it loads, so it must be set before the imports below.
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

import numpy as np
from openwakeword.model import Model as WakeModel
from pysilero_vad import SileroVoiceActivityDetector

from .speak import STOP_PHRASES, WAKE_PHRASES, addressed, is_echo, talk_over
from .stt import make_transcriber
from .turn import make_turn_detector

log = logging.getLogger("omni.listen")
RATE = 16000
CHUNK = 512            # Silero's window: 32 ms
WAKE_FRAME = 1280      # openWakeWord's frame: 80 ms
PREROLL = int(1.0 * RATE / CHUNK)  # long enough to keep "Omni," when a pause follows it
SPEECH_PROB = 0.5
BARGE_CHUNKS = 8       # ~256 ms of sustained speech while Omni talks
BARGE_FLOOR = -45.0    # dBFS over those chunks; a speakerphone's own AEC leaks Omni's voice at about -55
START_CHUNKS = 6       # ~190 ms to start a continuous-mode utterance
MAX_SECONDS = 30
NO_SPEECH_SECONDS = 5
VOXTYPE_STATE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voxtype/state"
BUSY_LIMIT = 120.0     # longest a conversation stays open while Omni is still busy with a turn
SEND_GRACE = 2.0       # a hotkey press this soon after capture ended was meant as "send", not "listen again"
LEVEL_EVERY = 2        # one voice.level event per 2 chunks: ~15 a second
LEVEL_FLOOR, LEVEL_CEILING = -60.0, -10.0  # dBFS mapped onto 0..1


class Feedback:
    """Live events for the on-screen indicator while a capture runs.

    Only speech addressed to Omni is ever emitted: a capture started by the hotkey, an
    approval answer, or inside a conversation shows from the start; one started by
    room speech shows only once its streaming transcript calls Omni by name.
    Until then nothing about it (not even its loudness) leaves this object.
    """

    def __init__(self, voice: VoiceLoop, trigger: str, open_mic: bool, spoken: str = ""):
        self.voice, self.trigger, self.spoken = voice, trigger, spoken
        self.open_mic = open_mic
        self.visible = False
        self.chunks, self.peak, self.text = 0, 0.0, ""
        if open_mic and trigger != "barge-in":  # talking over Omni shows once it is more than Omni's own echo
            self._show()

    def _emit(self, event: dict) -> None:
        self.voice.loop.call_soon_threadsafe(self.voice.daemon.emit, event)

    def _show(self) -> None:
        self.visible = True
        self._emit({"event": "voice.capture", "state": "start", "trigger": self.trigger})

    def _shown_text(self, partial: str) -> str | None:
        """What the indicator may show of a partial transcript, or None for nothing."""
        if self.trigger == "barge-in" and is_echo(partial, self.spoken):
            return None  # Omni's own voice in the mic
        if self.open_mic and self.trigger not in ("name", "barge-in"):
            return WAKE_PHRASES.sub("", partial)  # hotkey, answer: all of it goes to Omni
        if (request := addressed(partial)) is not None:
            return request  # from the name on; words before it were not for Omni
        return partial if self.open_mic else None

    def chunk(self, chunk: bytes, partial: str) -> None:
        """Called from the mic thread for every chunk of the capture."""
        if not self.visible:
            now = time.monotonic()
            voice = self.voice
            if voice._conversing and now < voice._follow_up_until or now < voice._named_until:
                self.open_mic = True  # the conversation opened (or "Omni" was said) during this capture
            if (self.trigger == "barge-in" or not self.open_mic) and (not partial or self._shown_text(partial) is None):
                return
            self._show()
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        self.peak = max(self.peak, float(np.mean(samples * samples)))
        self.chunks += 1
        if self.chunks % LEVEL_EVERY == 0:
            self._emit({"event": "voice.level", "level": level(self.peak)})
            self.peak = 0.0
        if partial:
            text = self._shown_text(partial)
            if text is not None and text != self.text:
                self.text = text
                self._emit({"event": "voice.partial", "text": text})

    def ended(self) -> None:
        if self.visible:
            self._emit({"event": "voice.capture", "state": "end", "trigger": self.trigger})

    def finished(self, sent: bool, text: str = "") -> None:
        """The transcript went to the agent or an approval (sent), or nothing came of it."""
        if sent:
            self._emit({"event": "voice.capture", "state": "sent", "trigger": self.trigger, "text": text})
        elif self.visible:
            self._emit({"event": "voice.capture", "state": "dropped", "trigger": self.trigger})


def level(power: float) -> float:
    """Mean square of s16 samples as 0..1 loudness for the indicator."""
    db = 10 * np.log10(power / 32768 ** 2 + 1e-12)
    return round(min(1.0, max(0.0, (db - LEVEL_FLOOR) / (LEVEL_CEILING - LEVEL_FLOOR))), 2)


class VoiceLoop:
    def __init__(self, daemon, loop: asyncio.AbstractEventLoop):
        settings = daemon.settings
        if not settings.extra.get("mic_command") and not shutil.which("pw-record"):
            raise RuntimeError("pw-record is missing")
        self.daemon, self.loop = daemon, loop
        self.settings = settings
        self.wake_enabled = settings.wake_word
        self.continuous = False
        self.barge_in = settings.extra.get("barge_in", True)
        self.barge_floor = float(settings.extra.get("barge_floor", BARGE_FLOOR))
        self.speaking = False
        self._ptt = threading.Event()
        self._stop_capture = threading.Event()
        self._once: asyncio.Future | None = None
        self._capturing = False
        self._capture_ended = 0.0
        self._named_until = 0.0  # after a bare "Omni", the next utterance is for Omni without the name
        # A conversation: after Omni answers a spoken request, a reply within `follow_up` seconds needs no name.
        self._conversing = False
        self._follow_up_until = 0.0
        self._follow_up_timer: asyncio.TimerHandle | None = None
        self._purpose = "request"
        self._transcriber = ThreadPoolExecutor(1, thread_name_prefix="whisper")
        self.vad = SileroVoiceActivityDetector()
        # Wake by name: every utterance is transcribed locally and only ones addressed to "Omni" are sent.
        # No model to train, and the request can follow the name in one breath ("Omni, open Files").
        self.wake_by_name = settings.wake_model.lower() == "omni"
        wake_model = str(Path(settings.wake_model).expanduser()) if "/" in settings.wake_model else settings.wake_model
        self.wake = (WakeModel(wakeword_models=[wake_model], inference_framework="onnx")
                     if settings.wake_word and not self.wake_by_name else None)
        self.stt = make_transcriber(settings)
        self.turn = make_turn_detector(settings)
        self.mic: subprocess.Popen | None = None
        self.last_error = ""

    # ---- control from the daemon (event-loop thread) --------------------------------

    def start(self) -> None:
        threading.Thread(target=self.stt.load, name="stt-load", daemon=True).start()
        threading.Thread(target=self._run_forever, name="mic", daemon=True).start()

    def push_to_talk(self) -> None:
        """Hotkey: start listening now, or finish the utterance if already listening."""
        if self._capturing:
            self._stop_capture.set()
        elif time.monotonic() - self._capture_ended > SEND_GRACE:
            self._ptt.set()

    def set_speaking(self, speaking: bool) -> None:
        self.speaking = speaking
        if speaking:
            self._close_follow_up(conversing=self._conversing)
        else:
            self.follow_up()

    def follow_up(self) -> None:
        """Omni has answered a spoken request: listen a few seconds for a reply that does not use the name."""
        seconds = float(self.settings.follow_up)
        if not self._conversing or self._once is not None:
            return  # no conversation, or an approval or question is listening for its own answer
        if self.daemon.speaker.recent_text(10).rstrip().endswith("?"):
            seconds *= 2  # Omni asked something: give time to think
        self._follow_up_until = time.monotonic() + seconds
        if self._follow_up_timer:
            self._follow_up_timer.cancel()
        self._follow_up_timer = self.loop.call_later(seconds, self._close_follow_up)
        self._window_shown = True
        self.daemon.set_listening(True)

    def end_conversation(self) -> None:
        """A typed request, "thanks", or silence: the next spoken request needs the name again."""
        self._close_follow_up()

    def _close_follow_up(self, conversing: bool = False) -> None:
        self._conversing = conversing and float(self.settings.follow_up) > 0
        # In a conversation the window stays open while Omni thinks and speaks; it closes `follow_up` s after.
        self._follow_up_until = float("inf") if self._conversing else 0.0
        if self._follow_up_timer:
            self._follow_up_timer.cancel()
        # A turn that fails or never speaks must not leave the conversation open for good.
        self._follow_up_timer = self.loop.call_later(BUSY_LIMIT, self._close_follow_up) if self._conversing else None
        if getattr(self, "_window_shown", False):
            self._window_shown = False
            self.daemon.set_listening(False)

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
                "stt": self.settings.stt, "whisper": self.settings.whisper_model, "whisper_ready": self.stt.ready,
                "turn": self.settings.turn_detector, "stt_error": getattr(self.stt, "error", ""), "error": self.last_error}

    # ---- mic thread ---------------------------------------------------------------

    def _run_forever(self) -> None:
        while True:
            try:
                self._run()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("mic loop failed; restarting in 3 s")
            time.sleep(3)

    def _open_mic(self) -> subprocess.Popen:
        if command := self.settings.extra.get("mic_command"):
            argv = shlex.split(command)  # must write 16 kHz mono s16 to stdout in real time
        else:
            argv = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--raw", "-"]
            if target := self.settings.extra.get("mic_target"):
                argv[1:1] = ["--target", str(target)]
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _chunks(self):
        self.mic = self._open_mic()
        try:
            while data := self._read_chunk():
                yield data
            raise RuntimeError("microphone stream ended")
        finally:
            self.mic.kill()

    def _read_chunk(self) -> bytes:
        """One whole chunk, or b"" at end of stream. A pipe read can return part of a chunk."""
        assert self.mic and self.mic.stdout
        size = CHUNK * 2
        data = self.mic.stdout.read(size)
        while data and len(data) < size:
            more = self.mic.stdout.read(size - len(data))
            if not more:
                return b""
            data += more
        return data or b""

    def _run(self) -> None:
        preroll: collections.deque[bytes] = collections.deque(maxlen=PREROLL)
        power: collections.deque[float] = collections.deque(maxlen=BARGE_CHUNKS)  # mean square per chunk
        wake_buffer = bytearray()
        voiced_run = 0
        dictating, next_check = False, 0.0
        for chunk in self._chunks():
            preroll.append(chunk)
            probability = self.vad.process_chunk(chunk)
            voiced_run = voiced_run + 1 if probability >= SPEECH_PROB else 0
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            power.append(float(np.mean(samples * samples)))
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
                if self.barge_in and voiced_run >= BARGE_CHUNKS and self._loud(power):
                    # Keep talking while this is transcribed; only speech addressed to Omni interrupts.
                    trigger = "barge-in"
            elif (self.continuous or not self.wake_by_name and now < self._follow_up_until) and voiced_run >= START_CHUNKS:
                trigger = "continuous"
            elif self.wake_by_name and self.wake_enabled and voiced_run >= START_CHUNKS:
                trigger = "name"
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
                if trigger != "name":
                    log.info("listening (%s)", trigger)
                keep = list(preroll) if trigger in ("barge-in", "continuous", "name") else []
                self._purpose = "answer" if self._once is not None else "request"
                self._capture(keep, heard_speech=trigger in ("barge-in", "continuous", "name"), trigger=trigger)
                preroll.clear()
                wake_buffer.clear()
                voiced_run = 0
                self.vad.reset()

    def _capture(self, initial: list[bytes], heard_speech: bool, trigger: str = "") -> None:
        self._capturing = True
        self._stop_capture.clear()
        shown = trigger not in ("barge-in", "name")  # not shown as listening unless it turns out to be for Omni
        if shown:
            self.loop.call_soon_threadsafe(self.daemon.set_listening, True)
        audio = bytearray(b"".join(initial))
        stream = self.stt.start()
        for chunk in initial:
            stream.feed(chunk)
        self.turn.reset(self._purpose)  # replies to a question end sooner when the model is unsure
        # Talking over Omni: stop it as soon as a streaming partial calls it by name, not after the turn ends.
        spoken = self.daemon.speaker.recent_text() if trigger == "barge-in" else ""
        cut = trigger != "barge-in"
        conversation = self._conversing and time.monotonic() < self._follow_up_until
        feedback = Feedback(self, trigger, shown or conversation or time.monotonic() < self._named_until, spoken)
        started = time.monotonic()
        speech_end = started if heard_speech else 0.0  # when the last voiced chunk arrived
        silence = 0.0
        chunk_seconds = CHUNK / RATE
        for chunk in self._chunks_continuing():
            audio += chunk
            stream.feed(chunk)
            probability = self.vad.process_chunk(chunk)
            now = time.monotonic()
            if probability >= SPEECH_PROB:
                heard_speech, silence, speech_end = True, 0.0, now
            else:
                silence += chunk_seconds
            elapsed = now - started
            partial = stream.partial()
            feedback.chunk(chunk, partial)
            if self._stop_capture.is_set() or elapsed > MAX_SECONDS:
                break
            if heard_speech and self.turn.update(audio, probability, silence):
                break
            if not cut and partial and addressed(partial) is not None and not is_echo(partial, spoken):
                cut = True
                log.info("talk-over: %r stops speech", partial)
                self._call(self.daemon.session.interrupt())
            if not heard_speech and elapsed > NO_SPEECH_SECONDS:
                audio.clear()
                break
        ended = time.monotonic()
        self._capturing = False
        self._capture_ended = ended
        feedback.ended()
        if shown:
            self.loop.call_soon_threadsafe(self.daemon.set_listening, False)
        if audio:
            # Decided now, not when the transcript is ready: a long utterance may finish after the window closes.
            conversation = conversation or self._conversing and ended < self._follow_up_until
            self._transcriber.submit(self._finish, stream, speech_end or ended, ended, self._purpose, trigger, conversation,
                                     feedback)
        else:
            feedback.finished(False)
            if self._once and not self._once.done():
                self.loop.call_soon_threadsafe(self._resolve_once, "")

    def _loud(self, power) -> bool:
        """Room speech, not the faint leak of Omni's own voice that Silero still calls speech."""
        return 10 * np.log10(sum(power) / len(power) / 32768 ** 2 + 1e-12) >= self.barge_floor

    def _chunks_continuing(self):
        while data := self._read_chunk():
            yield data

    def _finish(self, stream, speech_end: float, ended: float, purpose: str, trigger: str = "",
                conversation: bool = False, feedback: Feedback | None = None) -> None:
        done = feedback.finished if feedback else lambda sent, text="": None
        text = stream.finish()
        heard = time.monotonic()
        # Measured from when the speaker stopped, so the end-of-turn wait counts against latency.
        timings = {"endpoint": int((ended - speech_end) * 1000), "transcribe": int((heard - ended) * 1000)}
        self.on_transcript(text, timings, trigger)
        if trigger == "name":
            request = addressed(text)
            if request is None and time.monotonic() < self._named_until:
                request = text  # "Omni, … <pause> … open Files": the name ended the last capture
            elif request is None and conversation and not is_echo(text, self.daemon.speaker.recent_text()):
                if STOP_PHRASES.match(text.strip()) and len(text.split()) <= 4:
                    self.loop.call_soon_threadsafe(self.end_conversation)  # "thanks", "never mind"
                    done(False)
                    return
                request = text  # a reply in the conversation: no name needed
            if request is None:
                # Not for Omni. Room speech is neither logged nor kept; the count shows a request was missed.
                log.info("ignored %d words not addressed to Omni %s (conversation %s)", len(text.split()), timings,
                         "on" if self._conversing else "off")
                done(False)
                return
            log.info("heard %r %s (addressed by name)", text, timings)
            if not request.strip(" .,!?"):
                self._named_until = time.monotonic() + 4.0
                if not self._capturing:
                    self._ptt.set()  # just "Omni": listen for the request that follows
                return
            self._named_until = 0.0
            text = request
        else:
            log.info("heard %r %s", text, timings)
        if trigger == "barge-in":
            action, request = talk_over(text, self.daemon.speaker.recent_text())
            if action == "ignore" and conversation and len(text.split()) >= 3 and not is_echo(text, self.daemon.speaker.recent_text()):
                action, request = "ask", text  # talking over Omni mid-conversation: a reply, no name needed
            log.info("talk-over: %s", action)
            if action == "ignore":
                done(False)
                return
            if action == "stop":
                self._call(self.daemon.session.interrupt())
                done(False)
                return
            text = request  # ask() below stops speech and interrupts the turn
        if purpose == "answer":
            done(bool(self._once and not self._once.done()), text)
            self.loop.call_soon_threadsafe(self._resolve_once, text)  # dropped if already answered
            return
        text = WAKE_PHRASES.sub("", text).strip()
        if len(text) < 2:
            done(False)
            return
        done(True, text)
        self.loop.call_soon_threadsafe(self._close_follow_up, True)  # reopens once this answer ends
        self.loop.call_soon_threadsafe(self.daemon.set_listening, False, text)
        self._call(self.daemon.session.ask(text, source="voice", speak=True, t0=speech_end, timings=timings))

    def _resolve_once(self, text: str) -> None:
        if self._once and not self._once.done():
            self._once.set_result(text)

    def on_transcript(self, text: str, timings: dict, trigger: str) -> None:
        """Every finished transcription, before it is filtered; the bench overrides this."""

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
