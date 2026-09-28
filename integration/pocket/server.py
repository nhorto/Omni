#!/usr/bin/env python3
"""Kyutai Pocket TTS server for Omni's offline voice. Runs in its own venv (it needs torch).

stdin:  JSON lines, {"id": 1, "text": "..."} to speak and {"cancel": 1} to drop a request.
stdout: frames of <id u32><kind u8><length u32><payload>. Kind 0 is mono s16le PCM,
        1 ends a request, 2 is an error message, 3 (id 0) is ready with {"rate": ...}.
Requests run one at a time in arrival order. The server exits when stdin closes.

  server.py --voice alba     serve
  server.py --fetch          download the model and voice once (needs the network)
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import logging
import os
import queue
import struct
import sys
import threading
import warnings

HEADER = struct.Struct("<IBI")
AUDIO, END, ERROR, READY = range(4)
DEFAULT_VOICE = "alba"
TRIM_AFTER = 2  # idle seconds before returning freed memory

out = sys.stdout.buffer
out_lock = threading.Lock()


def send(request: int, kind: int, payload: bytes = b"") -> None:
    with out_lock:
        out.write(HEADER.pack(request, kind, len(payload)) + payload)
        out.flush()


def load(voice: str, quantize: bool):
    from pocket_tts import TTSModel

    model = TTSModel.load_model(quantize=quantize)
    try:
        state = model.get_state_for_audio_prompt(voice)
    except Exception as exc:  # a cloning sample needs Kyutai's gated weights; keep speaking with a stock voice
        print(f"pocket: voice {voice!r} unavailable ({exc}); using {DEFAULT_VOICE}", file=sys.stderr)
        state = model.get_state_for_audio_prompt(DEFAULT_VOICE)
    return model, state


def trim() -> None:
    # Loading and generating leave a few hundred MB of freed heap; hand it back to the system.
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def pcm(chunk) -> bytes:
    import torch

    return (chunk.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="stock voice name, .safetensors state, or .wav sample")
    parser.add_argument("--no-quantize", action="store_true", help="float32 weights (slower, more RAM)")
    parser.add_argument("--fetch", action="store_true", help="download the model and voice, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    warnings.filterwarnings("ignore", message=".*quantize_per_tensor.*")  # torch's own int8 path warns about itself
    # Frames own stdout; anything a library prints goes to stderr instead.
    global out
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    model, state = load(args.voice, not args.no_quantize)
    for _ in model.generate_audio_stream(state, "Ready."):  # warm the kernels before the first real sentence
        pass
    if args.fetch:
        print("pocket: model and voice are cached", file=sys.stderr)
        return
    trim()
    send(0, READY, json.dumps({"rate": model.sample_rate}).encode())

    pending: queue.Queue = queue.Queue()
    stops: dict[int, threading.Event] = {}

    def work() -> None:
        dirty = False
        while True:
            try:
                item = pending.get(timeout=TRIM_AFTER if dirty else None)
            except queue.Empty:
                trim()  # only between answers, so it never delays a queued sentence
                dirty = False
                continue
            if item is None:
                return
            request, text = item
            dirty = True
            stop = stops[request]
            try:
                if not stop.is_set():
                    for chunk in model.generate_audio_stream(state, text, stop=stop):
                        if stop.is_set():
                            break
                        send(request, AUDIO, pcm(chunk))
                send(request, END)
            except Exception as exc:
                send(request, ERROR, str(exc).encode())
            finally:
                stops.pop(request, None)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "cancel" in message:
            if stop := stops.get(message["cancel"]):
                stop.set()
        elif "text" in message:
            stops[message["id"]] = threading.Event()
            pending.put((message["id"], message["text"]))
    pending.put(None)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        os._exit(0)
