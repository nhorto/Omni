"""Sentence-chunked streaming speech: ElevenLabs first, then Pocket TTS or Piper offline.

The agent's reply arrives as text deltas. `Chunker` cuts it into sentences; the
`Speaker` fetches audio for the next sentence while the current one plays, and
pipes ElevenLabs' streamed MP3 straight into mpv so the first word plays while
the rest is still downloading. `stop()` is the barge-in path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from collections import deque
from pathlib import Path

from .. import config
from . import pocket

log = logging.getLogger("omni.speak")
SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+(?=[A-Z0-9\"'(]|$)|\n{1,}")
MARKUP = re.compile(r"`{1,3}[^`]*`{1,3}|\*\*|__|#+ |\[([^\]]+)\]\([^)]+\)|https?://\S+")


class Chunker:
    """Accumulates deltas; yields speakable sentences as soon as they are complete."""

    def __init__(self, min_chars: int = 24):
        self.buffer = ""
        self.min_chars = min_chars

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        out = []
        while True:
            match = None
            for candidate in SENTENCE_END.finditer(self.buffer):
                if candidate.end() >= self.min_chars:
                    match = candidate
                    break
            if not match:
                return out
            sentence, self.buffer = self.buffer[:match.end()], self.buffer[match.end():]
            if cleaned := clean(sentence):
                out.append(cleaned)

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer, ""
        return [cleaned] if (cleaned := clean(rest)) else []


def clean(text: str) -> str:
    text = MARKUP.sub(lambda m: m.group(1) or "", text)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    return " ".join(text.split())


ECHO_OVERLAP = 0.6


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def is_echo(heard: str, spoken: str) -> bool:
    """True when a barge-in transcript is mostly Omni's own recent words picked up by the mic."""
    # Compare four-letter stems: transcribing a speaker through a room drifts endings ("worries" -> "worry").
    heard_words, spoken_stems = _words(heard), {word[:4] for word in _words(spoken)}
    if not heard_words or not spoken_stems:
        return False
    return sum(word[:4] in spoken_stems for word in heard_words) / len(heard_words) >= ECHO_OVERLAP


# Moonshine hears the name as "Amni" and fuses "hey Omni" into "Hayamani"; it also writes "Omni:" and "Omni-".
WAKE_PHRASES = re.compile(r"^\s*((hey|hay|hi|ok|okay)[, ]+)?(omni|omnie|omny|omi|amni|ha[iy]?[ao]m[aio]n[iy]e?|jarvis)\b"
                          r"[-,.!?:;]*\s*", re.I)
CALLED = re.compile(r"\b(omni|omnie|omny)[,:!]\s*", re.I)  # the name called mid-sentence: "Yeah sounds good, Omni, stop."
STOP_PHRASES = re.compile(r"^(stop|wait|hold on|never ?mind|cancel|quiet|enough|that's enough|shut up|thanks|thank you|"
                          r"ok(ay)?,? (stop|thanks|thank you))\b", re.I)


def addressed(heard: str) -> str | None:
    """The request in an utterance that calls Omni by name, or None if it is not for Omni.

    The name may start the utterance or a later sentence ("Yeah, sounds good. Omni, stop."),
    so a request is not lost when it follows other talk in the same breath.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", heard.strip()):
        if WAKE_PHRASES.match(sentence):
            start = heard.index(sentence)
            return WAKE_PHRASES.sub("", heard[start:], count=1).strip()
    # The transcriber does not always mark the sentence break before the name.
    if match := CALLED.search(heard):
        return heard[match.end():].strip()
    return None


def talk_over(heard: str, spoken: str) -> tuple[str, str]:
    """Decide what speech heard while Omni is talking means: ("ignore" | "stop" | "ask", request).

    Only speech addressed to Omni counts. Room conversation, calls, videos, and Omni's own
    voice leaking into the mic are ignored, so none of them can cut an answer off or loop.
    """
    text = heard.strip()
    if not _words(text) or is_echo(text, spoken):
        return "ignore", ""
    request = addressed(text)
    rest = text if request is None else request
    if STOP_PHRASES.match(rest) and len(_words(rest)) <= 4:
        return "stop", ""
    if request is not None:
        return ("ask", rest) if _words(rest) else ("stop", "")
    return "ignore", ""


def elevenlabs_key() -> str:
    if key := os.environ.get("ELEVENLABS_API_KEY"):
        return key
    if shutil.which("secret-tool"):
        for app in ("omni", "omi"):
            result = subprocess.run(["secret-tool", "lookup", "application", app, "service", "elevenlabs"],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
    return ""


def store_elevenlabs_key(value: str) -> None:
    result = subprocess.run(["secret-tool", "store", "--label=Omni ElevenLabs", "application", "omni", "service", "elevenlabs"],
                            input=value.strip(), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Could not save the key to the keyring")


def player_argv(target: str | None = None) -> list[str] | None:
    if shutil.which("mpv"):
        device = [f"--audio-device=pipewire/{target}"] if target else []
        return ["mpv", "--no-video", "--really-quiet", "--no-terminal", "--cache=no", "--demuxer-readahead-secs=0", *device, "-"]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"]
    return None


ELEVENLABS_RETRY = 30  # seconds offline after an ElevenLabs failure, so each sentence does not wait on it
POCKET_COLD = 60       # seconds to wait for Pocket to load when it is the chosen voice


def play_argv(kind: str, target: str | None = None, rate: int = 24000) -> list[str]:
    """The player for one synthesized sentence: raw PCM and WAV go to pw-play, MP3 to mpv."""
    targeting = [f"--target={target}"] if target else []
    if kind == "pcm":
        return ["pw-play", "--raw", "--format=s16", f"--rate={rate}", "--channels=1", *targeting, "-"]
    if kind == "wav" and shutil.which("pw-play"):
        return ["pw-play", *targeting, "-"]
    if argv := player_argv(target):
        return argv
    raise RuntimeError("Install mpv to play speech")


class Speaker:
    def __init__(self, settings, on_state=lambda speaking: None):
        self.settings = settings
        self.on_state = on_state
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._generation = 0
        self._player: subprocess.Popen | None = None
        self._worker: asyncio.Task | None = None
        self._key: str | None = None
        self._elevenlabs_retry = 0.0  # monotonic time before which ElevenLabs is skipped after a failure
        self._pocket: pocket.Pocket | None = None
        self.speaking = False
        self._recent: deque[tuple[float, str]] = deque(maxlen=8)  # (when, sentence) for echo checks

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    def say(self, sentence: str) -> None:
        if sentence and self.settings.speech:
            self.queue.put_nowait(sentence)

    def stop(self) -> None:
        """Barge-in: drop queued sentences and cut the current audio immediately."""
        self._generation += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        if self._player and self._player.poll() is None:
            self._player.kill()
        self._set_speaking(False)

    def recent_text(self, seconds: float = 30) -> str:
        """What Omni has said lately, including the sentence playing now."""
        cutoff = time.monotonic() - seconds
        return " ".join(sentence for when, sentence in self._recent if when >= cutoff)

    @property
    def busy(self) -> bool:
        return self.speaking or not self.queue.empty()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        prefetched: tuple[int, str, asyncio.Future] | None = None
        while True:
            if prefetched:
                generation, sentence, audio = prefetched
                prefetched = None
            else:
                sentence = await self.queue.get()
                generation = self._generation
                audio = loop.run_in_executor(None, self._synthesize, sentence)
            if generation != self._generation:
                if self.queue.empty():
                    self._set_speaking(False)  # a stale prefetch must not leave Omni "speaking" forever
                continue
            self._set_speaking(True)
            self._recent.append((time.monotonic(), sentence))
            try:
                chunks = await audio
                if not self.queue.empty():
                    following = self.queue.get_nowait()
                    prefetched = (self._generation, following, loop.run_in_executor(None, self._synthesize, following))
                if generation == self._generation and chunks is not None:
                    await loop.run_in_executor(None, self._play, chunks, generation)
            except Exception as exc:
                log.warning("speech failed: %s", exc)
            if self.queue.empty() and not prefetched:
                self._set_speaking(False)

    def _set_speaking(self, value: bool) -> None:
        if value != self.speaking:
            self.speaking = value
            self.on_state(value)

    # Synthesis returns (format, iterator of audio bytes[, sample rate]); playback consumes it.
    # Order: ElevenLabs, then Pocket when it is the offline voice (or forced), then Piper.
    def _synthesize(self, sentence: str):
        provider = self.settings.speech_provider
        if provider == "elevenlabs" and self.settings.elevenlabs_voice_id and time.monotonic() >= self._elevenlabs_retry:
            if self._key is None:
                self._key = elevenlabs_key()
            if self._key:
                try:
                    return ("mp3", self._elevenlabs(sentence))
                except OSError as exc:
                    self._elevenlabs_retry = time.monotonic() + ELEVENLABS_RETRY
                    log.warning("ElevenLabs failed, using the offline voice for %d s: %s", ELEVENLABS_RETRY, exc)
        if provider == "pocket" or self.settings.offline_voice == "pocket":
            if self._pocket is None:
                self._pocket = pocket.Pocket(voice=self.settings.extra.get("pocket_voice", ""))
            if self._pocket.available():
                # Forced Pocket waits out a cold start; as a fallback, Piper covers it while Pocket warms up.
                try:
                    return ("pcm", self._pocket.stream(sentence, wait=POCKET_COLD if provider == "pocket" else 0),
                            self._pocket.rate)
                except (OSError, RuntimeError) as exc:
                    log.info("Pocket unavailable, using Piper: %s", exc)
        return ("wav", self._piper(sentence))

    def _elevenlabs(self, sentence: str):
        voice = self.settings.elevenlabs_voice_id
        payload = json.dumps({"text": sentence, "model_id": self.settings.elevenlabs_model}).encode()
        request = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=mp3_44100_128&optimize_streaming_latency=3",
            data=payload, method="POST",
            headers={"xi-api-key": self._key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        response = urllib.request.urlopen(request, timeout=15)
        first = response.read(4096)  # fail here (not mid-playback) on HTTP errors

        def chunks():
            try:
                yield first
                while chunk := response.read(4096):
                    yield chunk
            finally:
                response.close()
        return chunks()

    def _piper(self, sentence: str):
        piper = shutil.which("piper") or str(Path.home() / ".local/share/omi/tts/piper/piper")
        voice = self.settings.piper_voice or str(Path.home() / ".local/share/omi/tts/voices/en_US-lessac-medium.onnx")
        if not Path(piper).is_file() and not shutil.which("piper") or not Path(voice).is_file():
            raise RuntimeError("No speech available: set an ElevenLabs key and voice, or install Piper")
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=config.private_dir(config.RUNTIME), delete=False) as handle:
            target = handle.name
        subprocess.run([piper, "--model", voice, "--output_file", target, "--quiet"], input=sentence + "\n", text=True,
                       check=True, timeout=60, stdout=subprocess.DEVNULL)
        data = Path(target).read_bytes()
        os.unlink(target)
        return iter([data])

    def _play(self, synthesized, generation: int) -> None:
        kind, chunks = synthesized[:2]
        try:
            self._pipe(play_argv(kind, self.settings.extra.get("speaker_target"), *synthesized[2:]), chunks, generation)
        finally:
            if close := getattr(chunks, "close", None):
                close()  # a streaming source stops generating (Pocket cancels the request)

    def _pipe(self, argv: list[str], chunks, generation: int) -> None:
        self._player = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert self._player.stdin
        try:
            for chunk in chunks:
                if generation != self._generation:
                    break
                self._player.stdin.write(chunk)
            self._player.stdin.close()
        except BrokenPipeError:
            pass
        if generation != self._generation:
            self._player.kill()
        self._player.wait()


class SpeechGate:
    """Decides whether a turn's final answer is spoken, streaming it when it is safe to.

    Speak answers to questions. After a turn that acted on the desktop, stay quiet
    unless the reply asks something: buffer it and decide at the end.
    """

    def __init__(self, speaker: Speaker | None, enabled: bool):
        self.speaker = speaker
        self.enabled = enabled and speaker is not None
        self.chunker = Chunker()
        self.acted = False
        self.held: list[str] = []
        self.spoken: list[str] = []

    def delta(self, text: str) -> None:
        if not self.enabled:
            return
        for sentence in self.chunker.feed(text):
            self._emit(sentence)

    def _emit(self, sentence: str) -> None:
        if self.acted:
            self.held.append(sentence)
        else:
            self.spoken.append(sentence)
            self.speaker.say(sentence)

    def finish(self) -> None:
        if not self.enabled:
            return
        for sentence in self.chunker.flush():
            self._emit(sentence)
        if self.acted and any("?" in s for s in self.held):
            for sentence in self.held:
                self.spoken.append(sentence)
                self.speaker.say(sentence)
        self.held.clear()
