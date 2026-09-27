"""Sentence-chunked streaming speech: ElevenLabs first, Piper as the offline fallback.

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
import urllib.request
from pathlib import Path

from .. import config

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


def player_argv() -> list[str] | None:
    if shutil.which("mpv"):
        return ["mpv", "--no-video", "--really-quiet", "--no-terminal", "--cache=no", "--demuxer-readahead-secs=0", "-"]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"]
    return None


class Speaker:
    def __init__(self, settings, on_state=lambda speaking: None):
        self.settings = settings
        self.on_state = on_state
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._generation = 0
        self._player: subprocess.Popen | None = None
        self._worker: asyncio.Task | None = None
        self._key: str | None = None
        self.speaking = False

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
                continue
            self._set_speaking(True)
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

    # Synthesis returns an iterator of audio bytes plus its format; playback consumes it.
    def _synthesize(self, sentence: str):
        provider = self.settings.speech_provider
        if provider == "elevenlabs" and self.settings.elevenlabs_voice_id:
            if self._key is None:
                self._key = elevenlabs_key()
            if self._key:
                try:
                    return ("mp3", self._elevenlabs(sentence))
                except OSError as exc:
                    log.warning("ElevenLabs failed, using Piper: %s", exc)
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
        kind, chunks = synthesized
        argv = player_argv() if kind == "mp3" else (["pw-play", "-"] if shutil.which("pw-play") else player_argv())
        if not argv:
            raise RuntimeError("Install mpv to play speech")
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
