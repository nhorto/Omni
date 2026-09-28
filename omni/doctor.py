"""Read-only setup checks shown by `omni doctor` and the app's Check setup page."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess

from . import config


def _exe(name: str, purpose: str, required: bool = False) -> tuple[str, str, str]:
    found = shutil.which(name)
    return (purpose, "ok" if found else "fail" if required else "warn", found or f"install {name}")


def _echo_cancel(settings: config.Settings) -> list[tuple[str, str, str]]:
    """When mic_target or speaker_target names an omni-echo-cancel node, check PipeWire has it."""
    wanted = [name for name in (settings.extra.get("mic_target"), settings.extra.get("speaker_target"))
              if str(name or "").startswith("omni-echo-cancel-")]
    if not wanted:
        return []
    if not shutil.which("pw-cli"):
        return [("Echo cancellation", "fail", "install pipewire (pw-cli)")]
    nodes = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=10).stdout
    missing = [name for name in wanted if f'node.name = "{name}"' not in nodes]
    if missing:
        return [("Echo cancellation", "fail", f"no {', '.join(missing)}: set echo_cancel_mic and echo_cancel_speaker, "
                 "run install.py --apply, then systemctl --user restart pipewire pipewire-pulse wireplumber")]
    if len(wanted) == 1:
        return [("Echo cancellation", "warn", "set both mic_target and speaker_target to the omni-echo-cancel nodes; "
                 "it only removes speech played through its sink")]
    return [("Echo cancellation", "ok", "omni-echo-cancel-source and -sink present")]


def checks() -> list[tuple[str, str, str]]:
    settings = config.load_settings()
    result = [_exe("codex", "Codex CLI", True)]
    if shutil.which("codex"):
        status = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
        ok = status.returncode == 0 and "logged in" in (status.stdout + status.stderr).lower()
        result.append(("Codex login", "ok" if ok else "fail", "signed in" if ok else "run `codex login`"))
    result += [
        _exe("hyprctl", "Hyprland control", True),
        _exe("foot", "Terminal (foot)"),
        _exe("uwsm-app", "App launcher (uwsm-app)"),
        _exe("wtype", "Typing into apps (wtype)"),
        _exe("notify-send", "Notifications and approval buttons"),
        _exe("mpv", "Speech playback (mpv)"),
        _exe("pw-record", "Microphone capture (PipeWire)"),
        _exe("grim", "Screen capture"),
        _exe("tesseract", "Screen OCR"),
        _exe("secret-tool", "Keyring"),
    ]
    from .voice.speak import elevenlabs_key
    key = bool(elevenlabs_key())
    result.append(("ElevenLabs key", "ok" if key else "warn", "in keyring" if key else "run `omni key elevenlabs`"))
    result.append(("ElevenLabs voice", "ok" if settings.elevenlabs_voice_id else "warn",
                   settings.elevenlabs_voice_id or "set elevenlabs_voice_id in ~/.config/omni/config.toml"))
    for module, purpose in (("numpy", "Audio math (numpy)"), ("openwakeword", "Wake word (openWakeWord)"),
                            ("faster_whisper", "Local transcription (faster-whisper)"), ("pysilero_vad", "Voice activity (Silero VAD)"),
                            ("gi", "GTK app (PyGObject)"), ("claude_agent_sdk", "Claude adapter (claude-agent-sdk)")):
        present = importlib.util.find_spec(module) is not None
        result.append((purpose, "ok" if present else "warn", "installed" if present else f"pip install {module.replace('_', '-')}"))
    if settings.stt == "moonshine":
        present = importlib.util.find_spec("moonshine_voice") is not None
        result.append(("Streaming transcription (Moonshine)", "ok" if present else "warn",
                       "installed" if present else "run `python3 install.py --apply` (falls back to Whisper)"))
    if "pocket" in (settings.offline_voice, settings.speech_provider):
        from .voice import pocket
        ready = pocket.PYTHON.is_file() and (pocket.MODELS / "models--kyutai--pocket-tts-without-voice-cloning").is_dir()
        result.append(("Offline voice (Pocket TTS)", "ok" if ready else "warn",
                       "installed" if ready else "run `python3 install.py --apply` (Piper is used meanwhile)"))
    result += _echo_cancel(settings)
    result.append(("Data folder", "ok", str(config.DATA)))
    return result
