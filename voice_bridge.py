#!/usr/bin/env python3
"""Feed Voxtype transcripts into Omi. Text injection uses the identical path."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import assistant as omi

INBOX = omi.RUNTIME / "inbox"
PENDING = omi.RUNTIME / "pending"
CONTINUOUS = omi.RUNTIME / "continuous"
VOICE_STATE = Path(os.environ.get("XDG_RUNTIME_DIR", str(Path.home() / ".cache"))) / "voxtype/state"
RUNNING = True


def omi_recording() -> Path:
    return omi.RUNTIME / "omi-recording"


def prepare() -> None:
    for directory in (omi.RUNTIME, INBOX, PENDING):
        omi.private_dir(directory)


def new_transcript_path() -> Path:
    prepare()
    return INBOX / f"utterance-{time.time_ns()}-{uuid.uuid4().hex[:8]}.txt"


def inject(text: str) -> Path:
    path = new_transcript_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text.strip() + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def voxtype_state() -> str:
    try:
        return VOICE_STATE.read_text().strip()
    except OSError:
        return "unavailable"


def run_voxtype(*args: str) -> None:
    result = subprocess.run(["voxtype", "record", *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Voxtype command failed")


def start_utterance() -> Path:
    path = new_transcript_path()
    omi_recording().write_text("omi\n")
    omi_recording().chmod(0o600)
    try:
        run_voxtype("start", f"--file={path}", "--no-osd")
    except Exception:
        omi_recording().unlink(missing_ok=True)
        raise
    return path


def toggle_utterance() -> None:
    if CONTINUOUS.exists():
        stop_listening()
        return
    if voxtype_state() == "recording":
        if not omi_recording().exists():
            raise RuntimeError("Dictation is recording. Finish dictation before talking to Omi.")
        run_voxtype("stop")
        print("Transcribing")
    elif voxtype_state() in {"idle", "unavailable"}:
        path = start_utterance()
        print(f"Recording for Omi: {path}")
    else:
        print(f"Voxtype is {voxtype_state()}; wait for it to finish")


def stop_listening() -> None:
    CONTINUOUS.unlink(missing_ok=True)
    if omi_recording().exists() and voxtype_state() == "recording":
        run_voxtype("stop")
    elif omi_recording().exists() and voxtype_state() == "transcribing":
        pass
    omi_recording().unlink(missing_ok=True)
    omi.set_state("idle", omi.settings()["agent"])
    print("Omi listening stopped")


def notify(title: str, body: str) -> None:
    if not body.strip():
        return
    subprocess.Popen(["notify-send", "-a", "Omi", title, body.strip()[:500]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def review_in_app(request: str, agent: str, plan: dict) -> None:
    prepare()
    path = PENDING / f"review-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({"request": request, "agent": agent, "plan": plan}))
    path.chmod(0o600)
    omi.set_state("awaiting approval", agent, request[:120])
    command = [sys.executable, str(omi.ROOT / "omi_app.py"), "--review", str(path)]
    process = subprocess.Popen(command)
    completion = path.with_suffix(".done")
    while not completion.exists() and process.poll() is None:
        time.sleep(0.1)
    completion.unlink(missing_ok=True)
    if path.exists():
        path.unlink()
        with omi.database() as db:
            db.execute("INSERT INTO task_runs(source,request,agent,model,mode,plan,status,error,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("voice", request, agent, omi.selected_model(agent), plan.get("mode", "pending"), json.dumps(plan, ensure_ascii=False), "cancelled", "Review window closed before the request ran", omi.now(), omi.now()))
            db.commit()
        omi.set_state("error", agent, "Approval window closed before review")


def process_request(text: str) -> None:
    agent = omi.settings()["agent"]
    omi.set_state("thinking", agent, text[:120])
    with omi.database() as db:
        memories = omi.relevant_memories(db, text)
        try:
            plan = omi.plan_request(agent, text, memories, omi.recent_conversation(db), db)
            if not isinstance(plan, dict) or not isinstance(plan.get("actions"), list):
                raise ValueError("Agent returned an invalid plan")
        except Exception as exc:
            omi.record_planning_failure(db, text, agent, "voice", exc)
            raise
        if any(omi.needs_approval(action) for action in plan.get("actions", []) if isinstance(action, dict) and action.get("type") in omi.KINDS):
            review_in_app(text, agent, plan)
            return
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            omi.run_request(text, agent, db, plan=plan, source="voice")
        response = buffer.getvalue().strip()
        if response:
            notify("Omi", response)
            print(response, flush=True)


class UtteranceGate:
    def __init__(self, started: float, threshold_dbfs: float = -38.0, silence_seconds: float = 1.4, idle_seconds: float = 60.0):
        self.started = started
        self.last_speech = started
        self.threshold_dbfs = threshold_dbfs
        self.silence_seconds = silence_seconds
        self.idle_seconds = idle_seconds
        self.heard_speech = False

    def observe(self, data: bytes, current: float) -> str | None:
        samples = memoryview(data[:len(data) // 2 * 2]).cast("h")
        if samples:
            rms = math.sqrt(sum(int(sample) ** 2 for sample in samples) / len(samples)) / 32768
            dbfs = 20 * math.log10(max(rms, 1e-8))
            if dbfs > self.threshold_dbfs:
                self.heard_speech = True
                self.last_speech = current
        if self.heard_speech and current - self.last_speech >= self.silence_seconds:
            return "stop"
        if not self.heard_speech and current - self.started >= self.idle_seconds:
            return "cancel"
        return None


def monitor_silence(stop_event: threading.Event, threshold_dbfs: float = -38.0, silence_seconds: float = 1.4) -> None:
    """Stop an externally triggered recording after speech followed by quiet."""
    command = ["pw-record", "--raw", "--rate", "16000", "--channels", "1", "--format", "s16", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    gate = UtteranceGate(time.monotonic(), threshold_dbfs, silence_seconds)
    try:
        while not stop_event.is_set() and process.poll() is None:
            ready, _, _ = select.select([process.stdout], [], [], 0.25)
            data = os.read(process.stdout.fileno(), 3200) if ready else b""
            if ready and not data:
                break
            decision = gate.observe(data, time.monotonic())
            if decision:
                if voxtype_state() == "recording" and CONTINUOUS.exists():
                    try:
                        run_voxtype(decision)
                    except RuntimeError as exc:
                        print(f"Could not {decision} recording: {exc}", file=sys.stderr, flush=True)
                break
        if process.poll() is not None and not stop_event.is_set() and CONTINUOUS.exists():
            CONTINUOUS.unlink(missing_ok=True)
            if voxtype_state() == "recording":
                try:
                    run_voxtype("cancel")
                except RuntimeError:
                    pass
            omi.set_state("error", detail="Microphone monitor stopped; continuous listening disabled")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def handle_transcript(path: Path) -> None:
    # Claim the file before processing. Preserve failed requests for inspection.
    claimed = path.with_suffix(".processing")
    path.replace(claimed)
    text = claimed.read_text(errors="replace").strip()
    if not CONTINUOUS.exists():
        omi_recording().unlink(missing_ok=True)
    if not text:
        claimed.unlink(missing_ok=True)
        return
    print(f"Transcript: {text}", flush=True)
    try:
        process_request(text)
        claimed.unlink(missing_ok=True)
    except Exception as exc:
        failed = claimed.with_suffix(".failed")
        claimed.replace(failed)
        omi.set_state("error", detail=str(exc)[:120])
        notify("Omi error", str(exc))
        print(f"Omi error: {exc}; transcript saved to {failed}", file=sys.stderr, flush=True)


def reminder_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            with omi.database() as db:
                omi.reminders.deliver_due(db, omi.settings())
        except Exception as exc:
            print(f"Reminder worker: {type(exc).__name__}", file=sys.stderr, flush=True)
        stop_event.wait(1)


def serve() -> None:
    global RUNNING
    prepare()
    omi_recording().unlink(missing_ok=True)
    for stale in INBOX.glob("*.processing"):
        stale.replace(stale.with_suffix(".interrupted.failed"))
    omi.set_state("idle", omi.settings()["agent"])
    current: Path | None = None
    current_started = 0.0
    vad_stop: threading.Event | None = None

    def shutdown(*_: object) -> None:
        global RUNNING
        RUNNING = False

    reminder_stop = threading.Event()
    threading.Thread(target=reminder_worker, args=(reminder_stop,), daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while RUNNING:
        if current and voxtype_state() != "recording" and vad_stop:
            vad_stop.set()
            vad_stop = None
        ready = [p for p in sorted(INBOX.glob("*.txt")) if p.stat().st_size > 0 and time.time() - p.stat().st_mtime > 0.4]
        if ready:
            for path in ready:
                if not RUNNING:
                    break
                handle_transcript(path)
                if current == path:
                    current = None
            continue
        if current and voxtype_state() == "idle" and not current.exists() and time.monotonic() - current_started > 5:
            current = None
        if not CONTINUOUS.exists() and omi_recording().exists() and voxtype_state() == "idle" and time.time() - omi_recording().stat().st_mtime > 8:
            omi_recording().unlink(missing_ok=True)
        if CONTINUOUS.exists() and current is None and voxtype_state() == "idle":
            try:
                current = start_utterance()
                current_started = time.monotonic()
                vad_stop = threading.Event()
                threading.Thread(target=monitor_silence, args=(vad_stop,), daemon=True).start()
            except Exception as exc:
                omi.set_state("error", detail=str(exc)[:120])
                time.sleep(2)
        time.sleep(0.2)
    reminder_stop.set()
    if vad_stop:
        vad_stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Omi voice and text input bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("toggle")
    sub.add_parser("stop")
    sub.add_parser("status")
    inject_parser = sub.add_parser("inject")
    inject_parser.add_argument("text", nargs="+")
    continuous = sub.add_parser("continuous")
    continuous.add_argument("mode", choices=["on", "off", "toggle"])
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "inject":
        print(inject(" ".join(args.text)))
    elif args.command == "toggle":
        toggle_utterance()
    elif args.command == "stop":
        stop_listening()
    elif args.command == "status":
        print(json.dumps({"voxtype": voxtype_state(), "continuous": CONTINUOUS.exists()}))
    elif args.command == "continuous":
        enable = args.mode == "on" or (args.mode == "toggle" and not CONTINUOUS.exists())
        prepare()
        if enable:
            if voxtype_state() == "recording" and not omi_recording().exists():
                raise RuntimeError("Finish dictation before enabling Omi continuous listening")
            CONTINUOUS.write_text("on\n")
            CONTINUOUS.chmod(0o600)
            print("Continuous listening enabled")
        else:
            stop_listening()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Omi voice bridge: {exc}", file=sys.stderr)
        raise SystemExit(1)
