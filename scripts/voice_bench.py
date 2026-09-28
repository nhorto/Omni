#!/usr/bin/env python3
"""Replay synthesized speech through the real mic loop and measure end of turn and transcription.

Runs `VoiceLoop` against a FIFO (the `mic_command` setting) fed in real time, with a
stub session that records what would have been asked. Utterances are synthesized
once with Piper (default) or ElevenLabs into `$OMNI_RUNTIME/bench/`, never the repo.

    ~/.local/share/omni/venv/bin/python scripts/voice_bench.py
    ... --set turn_detector=smart --set stt=moonshine --label smart-moonshine
    ... --voice elevenlabs --only pause

Per case it reports, measured from when the audio's speech really ended:
endpoint (turn end), transcribe, ready (text delivered), the transcript, whether the
name was heard, word error rate, and whether the utterance was cut in two.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni import config  # noqa: E402
from omni.voice import listen  # noqa: E402
from omni.voice.speak import WAKE_PHRASES, addressed, elevenlabs_key  # noqa: E402

RATE, CHUNK = listen.RATE, listen.CHUNK

# (name, kind, segments): text is spoken, a number is a pause in seconds.
# kind "request" is addressed by name; "bare" is the name alone, then the request;
# "answer" is a reply to an approval or question (captured with listen_once, no name).
CASES = [
    ("open-files", "request", ["Omni, open Files."]),
    ("time", "request", ["Omni, what time is it?"]),
    ("next-workspace", "request", ["Omni, go to the next workspace."]),
    ("mute", "request", ["Hey Omni, mute the volume."]),
    ("stop", "request", ["Omni, stop."]),
    ("scores", "request", ["Omni, what are the college football scores today, and did Tennessee win their game on Saturday?"]),
    ("jacket", "request", ["Omni, can you look up the weather for tomorrow and tell me if I need a jacket in the morning?"]),
    ("email", "request", ["Omni, summarize the unread emails from this morning and tell me which ones need a reply."]),
    ("terminals", "request", ["Omni, go to workspace three, open a terminal on the left and run pwd, "
                              "and another one on the right that runs date."]),
    ("pause-garage", "request", ["Omni, remind me to", 0.9, "um, call the garage tomorrow about the brakes."]),
    ("pause-spreadsheet", "request", ["Omni, open the", 0.7, "the budget spreadsheet from last week."]),
    ("pause-capital", "request", ["Omni, what's the", 1.2, "what's the capital of Australia?"]),
    ("pause-list", "request", ["Omni, add milk, eggs,", 0.8, "and coffee to my shopping list."]),
    ("bare-name", "bare", ["Omni.", 2.0, "What's the weather tomorrow?"]),
    ("yes", "answer", ["Yes."]),
    ("no", "answer", ["No."]),
    ("go-ahead", "answer", ["Go ahead."]),
    ("cancel", "answer", ["Cancel that."]),
    ("yes-send", "answer", ["Yes, send it."]),
]
FILLERS = {"um", "uh", "erm", "hmm"}
NUMBERS = {"3": "three"}


# ---- synthesis ---------------------------------------------------------------------

def synthesize(text: str, voice: str, cache: Path, voice_id: str) -> np.ndarray:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
    path = cache / voice / f"{slug}.pcm"
    if not path.is_file():
        config.private_dir(path.parent)
        path.write_bytes(_piper(text) if voice == "piper" else _elevenlabs(text, voice_id))
    return np.frombuffer(path.read_bytes(), dtype=np.int16)


def _piper(text: str) -> bytes:
    from scipy.signal import resample_poly  # noqa: PLC0415
    home = Path.home() / ".local/share/omi/tts"
    raw = subprocess.run([str(home / "piper/piper"), "--model", str(home / "voices/en_US-lessac-medium.onnx"),
                          "--output_raw", "--quiet"], input=text.encode(), capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return np.clip(resample_poly(samples, 320, 441), -32768, 32767).astype(np.int16).tobytes()  # 22.05 -> 16 kHz


def _elevenlabs(text: str, voice_id: str) -> bytes:
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_16000",
        data=json.dumps({"text": text, "model_id": "eleven_flash_v2_5"}).encode(), method="POST",
        headers={"xi-api-key": elevenlabs_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def speech_bounds(samples: np.ndarray) -> tuple[int, int]:
    """First and last sample of audible speech (10 ms frames above -45 dBFS)."""
    frame = RATE // 100
    frames = samples[: len(samples) // frame * frame].astype(np.float32).reshape(-1, frame)
    loud = np.flatnonzero(np.sqrt((frames ** 2).mean(axis=1)) > 32768 * 10 ** (-45 / 20))
    return (int(loud[0]) * frame, int(loud[-1] + 1) * frame) if len(loud) else (0, len(samples))


def build_clip(segments, voice: str, cache: Path, voice_id: str) -> tuple[np.ndarray, int]:
    """The case's audio, trimmed at the edges and padded to whole chunks, and the sample where its speech ends."""
    texts = [s for s in segments if isinstance(s, str)]
    if len(texts) > 1 and not texts[0].rstrip().endswith((".", "?", "!")):
        clip = spliced(segments, voice, cache, voice_id)
        return np.concatenate([clip, np.zeros(-len(clip) % CHUNK, dtype=np.int16)]), len(clip)
    parts = []
    for segment in segments:
        if isinstance(segment, (int, float)):
            parts.append(np.zeros(int(segment * RATE), dtype=np.int16))
        else:
            samples = synthesize(segment, voice, cache, voice_id)
            start, end = speech_bounds(samples)
            parts.append(samples[start:end])
    clip = np.concatenate(parts)
    return np.concatenate([clip, np.zeros(-len(clip) % CHUNK, dtype=np.int16)]), len(clip)


_aligner = None


def spliced(segments, voice: str, cache: Path, voice_id: str) -> np.ndarray:
    """A mid-thought pause: the sentence synthesized in one piece, so the words before the pause keep their
    continuing intonation, with silence inserted between the words where the pause goes (word times from Whisper)."""
    global _aligner
    from faster_whisper import WhisperModel  # noqa: PLC0415
    samples = synthesize(" ".join(s for s in segments if isinstance(s, str)), voice, cache, voice_id)
    start, end = speech_bounds(samples)
    samples = samples[start:end]
    _aligner = _aligner or WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4)
    found, _ = _aligner.transcribe(samples.astype(np.float32) / 32768, language="en", word_timestamps=True,
                                   initial_prompt="Omni")
    timed = [word for segment in found for word in segment.words]
    parts, at, count = [], 0, 0
    for segment in segments:
        if isinstance(segment, str):
            count += len(re.findall(r"[\w']+", segment))
            continue
        cut = int((timed[count - 1].end + timed[count].start) / 2 * RATE)
        logging.getLogger("bench").info("pause of %.1f s after %r, before %r", segment, timed[count - 1].word, timed[count].word)
        parts += [samples[at:cut], np.zeros(int(segment * RATE), dtype=np.int16)]
        at = cut
    return np.concatenate(parts + [samples[at:]])


# ---- the loop under test -----------------------------------------------------------

class StubSession:
    def __init__(self):
        self.asks: list[dict] = []

    async def ask(self, text, *, source="text", speak=None, t0=None, ref=None, timings=None):
        self.asks.append({"text": text, "at": time.monotonic(), "t0": t0, "timings": timings or {}})
        return {}

    async def interrupt(self):
        pass


class StubDaemon:
    def __init__(self, settings):
        self.settings = settings
        self.session = StubSession()
        self.speaker = type("Speaker", (), {"recent_text": lambda self, seconds=30: ""})()

    def set_listening(self, listening, transcript=""):
        pass


class BenchLoop(listen.VoiceLoop):
    def __init__(self, *args):
        super().__init__(*args)
        self.captures: list[dict] = []

    @staticmethod
    def _dictating() -> bool:
        return False  # the real loop goes deaf while Voxtype dictates; the bench's mic is not the room

    def on_transcript(self, text, timings, trigger):
        self.captures.append({"text": text, "at": time.monotonic(), "timings": timings, "trigger": trigger})
        logging.getLogger("bench").info("capture (%s) %r %s", trigger, text, timings)


def make_settings(overrides: list[str], fifo: Path) -> config.Settings:
    fields = {f.name for f in dataclasses.fields(config.Settings)} - {"extra"}
    values, extra = {}, {"mic_command": f"cat {fifo}", "barge_in": False}
    for item in overrides:
        key, _, raw = item.partition("=")
        try:
            value = tomllib.loads(f"v = {raw}")["v"]
        except tomllib.TOMLDecodeError:
            value = raw
        (values if key in fields else extra)[key] = value
    return config.Settings(**values, extra=extra)


def run(cases, args) -> list[dict]:
    cache = config.RUNTIME / "bench"
    voice_id = args.voice_id or config.load_settings(Path.home() / ".config/omni/config.toml").elevenlabs_voice_id
    clips = {name: build_clip(segments, args.voice, cache, voice_id) for name, _, segments in cases}

    fifo = Path(tempfile.mkdtemp(prefix="omni-bench-")) / "mic"
    os.mkfifo(fifo)
    settings = make_settings(args.set, fifo)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    daemon = StubDaemon(settings)
    voice = BenchLoop(daemon, loop)
    voice.start()
    while not voice.stt.ready or not getattr(voice.turn, "ready", True):
        time.sleep(0.1)

    rng = np.random.default_rng(0)
    timeline, answers = [], {}
    with open(fifo, "wb", buffering=0) as mic:
        base, written = time.monotonic(), 0

        def write(samples: np.ndarray) -> None:
            nonlocal written
            samples = samples.astype(np.int32) + rng.normal(0, args.noise, len(samples)).astype(np.int32)
            samples = np.clip(samples, -32768, 32767).astype(np.int16)
            for i in range(0, len(samples), CHUNK):
                piece = samples[i:i + CHUNK]
                delay = base + written / RATE - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                mic.write(piece.tobytes())
                written += len(piece)

        write(np.zeros(RATE, dtype=np.int16))
        for name, kind, _ in cases:
            clip, end = clips[name]
            start = time.monotonic()
            if kind == "answer":
                future = asyncio.run_coroutine_threadsafe(voice.listen_once(args.tail + 5), loop)
                future.add_done_callback(lambda f, name=name: answers.setdefault(name, (f.result(), time.monotonic())))
            write(np.zeros(int(args.lead * RATE) // CHUNK * CHUNK, dtype=np.int16))
            speech_start = base + written / RATE
            write(clip)
            timeline.append({"name": name, "kind": kind, "start": start, "speech_start": speech_start,
                             "speech_end": speech_start + end / RATE})
            write(np.zeros(int(args.tail * RATE) // CHUNK * CHUNK, dtype=np.int16))
    time.sleep(0.5)
    return [score(entry, cases, voice, daemon, answers, timeline) for entry in timeline]


def words(text: str) -> list[str]:
    out = []
    for word in re.findall(r"[a-z0-9']+", text.lower()):
        word = NUMBERS.get(word, word)
        if word not in FILLERS:
            out.append(word)
    return out


def wer(expected: str, heard: str) -> float:
    ref, hyp = words(expected), words(heard)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / max(len(ref), 1)


def score(entry, cases, voice, daemon, answers, timeline) -> dict:
    index = timeline.index(entry)
    until = timeline[index + 1]["start"] if index + 1 < len(timeline) else float("inf")
    within = lambda items: [item for item in items if entry["start"] <= item["at"] < until]  # noqa: E731
    captures = within(voice.captures)
    segments = next(segments for name, _, segments in cases if name == entry["name"])
    spoken = " ".join(s for s in segments if isinstance(s, str))
    result = {"name": entry["name"], "kind": entry["kind"], "captures": len(captures),
              "heard": " | ".join(c["text"] for c in captures)}
    if entry["kind"] == "answer":
        text, at = answers.get(entry["name"], ("", None))
        expected = spoken
    else:
        asks = within(daemon.session.asks)
        text, at = (asks[-1]["text"], asks[-1]["at"]) if asks else ("", None)
        expected = WAKE_PHRASES.sub("", segments[-1] if entry["kind"] == "bare" else spoken, count=1)
        result["name_heard"] = addressed(captures[0]["text"]) is not None if captures else False
    result["text"] = text
    result["wer"] = round(wer(expected, text), 2) if at else 1.0
    result["cut_off"] = len(captures) > (2 if entry["kind"] == "bare" else 1) or (bool(at) and result["wer"] > 0.3
                                                                                   and len(words(text)) < len(words(expected)))
    if at and captures:
        final = captures[-1]["timings"]
        result["ready"] = int((at - entry["speech_end"]) * 1000)
        result["transcribe"] = final["transcribe"]
        result["endpoint"] = result["ready"] - final["transcribe"]
    return result


def summarize(results: list[dict]) -> dict:
    def median(key, kinds=("request", "bare", "answer")):
        values = [r[key] for r in results if key in r and r["kind"] in kinds]
        return int(statistics.median(values)) if values else None
    named = [r for r in results if "name_heard" in r]
    return {"endpoint_median": median("endpoint"), "endpoint_median_answers": median("endpoint", ("answer",)),
            "transcribe_median": median("transcribe"), "ready_median": median("ready"),
            "cut_offs": sum(r["cut_off"] for r in results), "missed": sum("ready" not in r for r in results),
            "name_heard": f"{sum(r['name_heard'] for r in named)}/{len(named)}",
            "wer_mean": round(statistics.mean(r["wer"] for r in results), 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--voice", choices=("piper", "elevenlabs"), default="piper")
    parser.add_argument("--voice-id", help="ElevenLabs voice (default: elevenlabs_voice_id from ~/.config/omni)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting (repeatable)")
    parser.add_argument("--only", help="run cases whose name contains this")
    parser.add_argument("--lead", type=float, default=1.0, help="silence before each case (s)")
    parser.add_argument("--tail", type=float, default=4.0, help="silence after each case (s)")
    parser.add_argument("--noise", type=float, default=30.0, help="background noise level (s16 standard deviation)")
    parser.add_argument("--label", default="run")
    parser.add_argument("-v", "--verbose", action="store_true", help="log the mic loop")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(relativeCreated)7d %(name)s %(message)s")
    cases = [case for case in CASES if not args.only or args.only in case[0]]

    results = run(cases, args)
    print(f"{'case':18} {'endpoint':>8} {'transcr':>7} {'ready':>6} {'wer':>5} {'cut':>4} {'name':>4}  text")
    for r in results:
        ms = lambda key: str(r.get(key, "-"))  # noqa: E731
        name = {True: "yes", False: "NO"}.get(r.get("name_heard"), "")
        print(f"{r['name']:18} {ms('endpoint'):>8} {ms('transcribe'):>7} {ms('ready'):>6} {r['wer']:>5} "
              f"{'CUT' if r['cut_off'] else '':>4} {name:>4}  {r['text'] or '(nothing) heard: ' + r['heard']}")
    summary = summarize(results)
    print(json.dumps(summary))
    out = config.private_dir(config.RUNTIME / "bench") / f"results-{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"args": vars(args), "summary": summary, "results": results}, indent=1))
    print(f"saved {out}")
    sys.stdout.flush()
    os._exit(0)  # the mic thread is blocked reopening the FIFO


if __name__ == "__main__":
    main()
