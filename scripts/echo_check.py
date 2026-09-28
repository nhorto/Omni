#!/usr/bin/env python3
"""Measure how much of Omni's own voice the mic loop hears, with and without echo cancellation.

Plays ~20 s of Piper speech on a sink while recording sources, then reports what Silero VAD
makes of each recording, the way omni/voice/listen.py does: speech fraction (p >= 0.5), the
longest run of voiced 32 ms chunks, barge-in triggers (a run reaching 8), and how many of those
were louder than --floor (quiet leaks through the Yealink's own AEC fall below -45 dBFS). Run it with the
Omni venv (numpy, pysilero-vad). Recordings stay in the work directory, never the repo.

  VENV=~/.local/share/omni/venv/bin/python
  $VENV scripts/echo_check.py --play <sink> --record <source> [--record <source> ...]
      --record monitor:<sink>     record what a sink plays (to confirm the playback path)
      --echo-cancel MIC SPEAKER   load integration/pipewire/omni-echo-cancel.conf for this run
                                  in a separate pipewire client (no restart), with these devices
      --talker <sink>             a second voice in three bursts on another sink (double-talk):
                                  bursts caught means real speech still starts a barge-in
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from pysilero_vad import SileroVoiceActivityDetector

ROOT = Path(__file__).resolve().parents[1]
RATE, CHUNK, SPEECH_PROB, BARGE_CHUNKS = 16000, 512, 0.5, 8  # as in omni/voice/listen.py
PIPER = Path.home() / ".local/share/omi/tts/piper/piper"
VOICE = Path.home() / ".local/share/omi/tts/voices/en_US-lessac-medium.onnx"
SPEECH = ("Here is the forecast for the rest of the week. Tomorrow will be mostly sunny with a high of "
          "seventy two degrees and a light breeze from the west. On Wednesday, clouds move in during the "
          "afternoon, and there is a good chance of rain by the evening commute. Thursday and Friday look "
          "cooler, with highs in the low sixties. The weekend should clear up nicely, so Saturday is a good "
          "day for the hike you mentioned.")
TALKER = ["Hey, can you stop for a second and open my calendar?",
          "Actually, wait, what time is the meeting tomorrow morning?",
          "Turn the music down and read me the last message, please."]
BASE_CONF = """context.properties = { log.level = 2 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt args = { } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def synth(text: str, work: Path, scale: float = 1.0) -> Path:
    path = work / f"tts-{hashlib.sha1(f'{text}{scale}'.encode()).hexdigest()[:10]}.wav"
    if not path.exists():
        subprocess.run([str(PIPER), "--model", str(VOICE), "--output_file", str(path), "--quiet",
                        "--length_scale", str(scale)], input=text + "\n", text=True, check=True,
                       stdout=subprocess.DEVNULL)
    return path


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16), handle.getframerate()


def write_wav(path: Path, samples: np.ndarray, rate: int) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype(np.int16).tobytes())
    return path


def omni_track(work: Path, seconds: float) -> Path:
    samples, rate = read_wav(synth(SPEECH, work))
    return write_wav(work / "omni.wav", samples[:int(seconds * rate)], rate)


def talker_track(work: Path, seconds: float) -> tuple[Path, list[tuple[float, float]]]:
    """Three bursts spread over the run, with silence around each; returns their windows."""
    clips = [read_wav(synth(text, work, 0.9)) for text in TALKER]
    rate = clips[0][1]
    track = np.zeros(int(seconds * rate), dtype=np.int16)
    windows = []
    for index, (clip, _) in enumerate(clips):
        start = int((1.5 + index * seconds / len(clips)) * rate)
        clip = clip[:len(track) - start]
        track[start:start + len(clip)] = clip
        windows.append((start / rate, (start + len(clip)) / rate))
    return write_wav(work / "talker.wav", track, rate), windows


def node_exists(name: str) -> bool:
    return f'node.name = "{name}"' in subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True).stdout


def load_echo_cancel(work: Path, mic: str, speaker: str) -> subprocess.Popen:
    body = (ROOT / "integration/pipewire/omni-echo-cancel.conf").read_text()
    body = body.replace("@MIC@", mic).replace("@SPEAKER@", speaker)
    conf = work / "echo-cancel-client.conf"
    conf.write_text(BASE_CONF + body.split("context.modules = [", 1)[1])
    process = subprocess.Popen(["pipewire", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if node_exists("omni-echo-cancel-source"):
            time.sleep(0.5)  # let WirePlumber link the capture and playback streams
            return process
        time.sleep(0.1)
    process.terminate()
    raise SystemExit("echo-cancel nodes did not appear")


def analyze(path: Path, offset: float, length: float, windows: list[tuple[float, float]], floor: float) -> dict:
    audio = np.frombuffer(path.read_bytes(), dtype=np.int16)
    vad = SileroVoiceActivityDetector()
    probs = [vad.process_chunk(audio[i:i + CHUNK].tobytes()) for i in range(0, len(audio) - CHUNK + 1, CHUNK)]
    step = CHUNK / RATE
    first, last = int(offset / step), int((offset + length + 0.5) / step)
    voiced = [p >= SPEECH_PROB for p in probs[first:last]]
    power = [np.mean(audio[i:i + CHUNK].astype(np.float64) ** 2) for i in range(0, len(audio) - CHUNK + 1, CHUNK)]
    run = longest = loud = 0
    triggers = []
    for index, flag in enumerate(voiced):
        run = run + 1 if flag else 0
        longest = max(longest, run)
        if run == BARGE_CHUNKS:
            triggers.append((index + first) * step)
            level = np.mean(power[index + first - BARGE_CHUNKS + 1:index + first + 1])
            loud += 10 * np.log10(level / 32768 ** 2 + 1e-12) >= floor
    window = audio[int(offset * RATE):int((offset + length) * RATE)].astype(np.float64)
    rms = np.sqrt(np.mean(window ** 2)) if len(window) else 0.0
    # A burst counts as caught when a barge-in fires between its start and 0.8 s after its end
    # (Bluetooth and the AEC add a few hundred ms of delay).
    inside = [any(start <= t <= end + 0.8 for start, end in windows) for t in triggers]
    return {"speech": sum(voiced) / max(len(voiced), 1), "longest": longest, "triggers": len(triggers), "loud": loud,
            "false": inside.count(False), "caught": sum(any(start <= t <= end + 0.8 for t in triggers)
                                                        for start, end in windows),
            "dbfs": 20 * np.log10(rms / 32768) if rms else -120.0, "at": [round(t - offset, 1) for t in triggers]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--play", help="sink for Omni's voice; omit to record the quiet room")
    parser.add_argument("--record", action="append", required=True, help="source node to record (repeatable)")
    parser.add_argument("--talker", help="sink for a second voice in three bursts")
    parser.add_argument("--volume", type=float, default=1.0, help="stream volume for Omni's voice (talker stays at 1)")
    parser.add_argument("--echo-cancel", nargs=2, metavar=("MIC", "SPEAKER"))
    parser.add_argument("--floor", type=float, default=-45.0,
                        help="also count barge-ins whose 8 chunks average at least this many dBFS")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--label", default="run")
    parser.add_argument("--work", type=Path, default=Path(os.environ.get("OMNI_RUNTIME", "/tmp")) / "echo-check")
    args = parser.parse_args()
    for tool in ("pw-record", "pw-play", "pw-cli"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found")
    args.work.mkdir(parents=True, exist_ok=True, mode=0o700)
    omni = omni_track(args.work, args.seconds)
    talker, windows = talker_track(args.work, args.seconds) if args.talker else (None, [])
    module = load_echo_cancel(args.work, *args.echo_cancel) if args.echo_cancel else None
    state = "loaded for this run" if module else "present" if node_exists("omni-echo-cancel-source") else "absent"
    try:
        recordings, recorders = [], []
        for index, source in enumerate(args.record):
            path = args.work / f"{args.label}-{index}.raw"
            node = source.removeprefix("monitor:")  # monitor:<sink> records what the sink plays
            extra = ["-P", "{ stream.capture.sink = true }"] if node != source else []
            recorders.append(subprocess.Popen(["pw-record", "--target", node, *extra, "--rate", str(RATE), "--channels",
                                               "1", "--format", "s16", "--raw", "-"], stdout=path.open("wb"),
                                              stderr=subprocess.DEVNULL))
            recordings.append(path)
        started = time.monotonic()
        time.sleep(1.0)
        offset = time.monotonic() - started
        players = [subprocess.Popen(["pw-play", f"--target={sink}", f"--volume={volume}", str(track)],
                                    stderr=subprocess.DEVNULL)
                   for sink, track, volume in ((args.play, omni, args.volume), (args.talker, talker, 1.0)) if sink and track]
        if players:
            for player in players:
                player.wait()
        else:
            time.sleep(args.seconds)
        time.sleep(1.0)
        for recorder in recorders:
            recorder.terminate()
            recorder.wait()
    finally:
        if module:
            module.terminate()
            module.wait()
    windows = [(start + offset, end + offset) for start, end in windows]
    print(f"{args.label}: play={args.play or '-'} talker={args.talker or '-'} echo-cancel nodes {state}")
    print(f"  {'source':<40} {'speech':>7} {'longest':>8} {'barge-ins':>10} {f'>={args.floor:g}dB':>9} {'dBFS':>7}" +
          (f" {'bursts caught':>14} {'false':>6}" if windows else ""))
    for source, path in zip(args.record, recordings):
        r = analyze(path, offset, args.seconds, windows, args.floor)
        print(f"  {source:<40} {r['speech']:>6.0%} {r['longest']:>8} {r['triggers']:>10} {r['loud']:>9} {r['dbfs']:>7.1f}" +
              (f" {r['caught']:>12}/{len(windows)} {r['false']:>6}" if windows else "") +
              (f"  barge-ins at {r['at']} s after playback start" if r["at"] else ""))


if __name__ == "__main__":
    sys.exit(main())
