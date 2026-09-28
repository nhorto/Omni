"""Paths and user settings. Private data never lives in the checkout."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


HOME = Path.home()
DATA = _env_path("OMNI_DATA", HOME / ".local/share/omni")
CONFIG = _env_path("OMNI_CONFIG", HOME / ".config/omni")
RUNTIME = _env_path("OMNI_RUNTIME", Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omni")
SOCKET = RUNTIME / "omnid.sock"
STATUS = RUNTIME / "status.json"
LEGACY_DB = HOME / ".local/share/omi/memory.sqlite3"
REPORTS = HOME / "Documents/Omni Reports"


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@dataclass
class Settings:
    agent: str = "codex"
    model: str | None = None
    effort: str = "low"
    # Thread retirement (see PLAN.md "Context and cost control").
    idle_minutes: int = 45
    context_ceiling: int = 150_000
    daily_token_budget: int = 3_000_000
    # Speech.
    speech: bool = True
    speech_provider: str = "elevenlabs"
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    piper_voice: str = ""
    offline_voice: str = "pocket"  # used when ElevenLabs is unavailable: "pocket" (Piper while it loads) or "piper"
    # Voice input.
    wake_word: bool = True
    wake_model: str = "omni"  # "omni": listen for the name in transcripts; otherwise an openWakeWord model
    wake_threshold: float = 0.5
    whisper_model: str = "base.en"
    stt: str = "moonshine"  # "moonshine" (streaming; Whisper if it cannot load) or "whisper" (batch after the turn)
    stt_model: str = ""  # engine-specific model name; empty means the engine's default
    end_silence: float = 1.0
    turn_detector: str = "smart"  # "smart" (Smart Turn model; end_silence if it cannot load) or "silence"
    turn_threshold: float = 0.5  # Smart Turn: end the turn when p(finished) reaches this
    turn_max_silence: float = 1.5  # Smart Turn: end the turn after this much silence regardless
    # Nested agents.
    delegate_default: str = "background"
    terminal: str = "foot"
    extra: dict = field(default_factory=dict)


def load_settings(path: Path | None = None) -> Settings:
    path = path or CONFIG / "config.toml"
    if not path.is_file():
        return Settings()
    raw = tomllib.loads(path.read_text())
    known = {name for name in Settings.__dataclass_fields__ if name != "extra"}
    values = {key: value for key, value in raw.items() if key in known}
    return Settings(**values, extra={key: value for key, value in raw.items() if key not in known})


def save_setting(key: str, value, path: Path | None = None) -> None:
    """Update one top-level key in config.toml, keeping the rest of the file."""
    path = path or CONFIG / "config.toml"
    private_dir(path.parent)
    lines = path.read_text().splitlines() if path.is_file() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() != key]
    if value is not None:  # None removes the key, restoring the default
        lines.append(f"{key} = {_toml(value)}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
