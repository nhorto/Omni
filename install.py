#!/usr/bin/env python3
"""Preview or install Omni for this user. Never uses sudo; prints the pacman line for anything missing.

  python3 install.py            show what would change
  python3 install.py --apply    create the venv, launchers, services, plugin, and Hyprland file
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
DATA = HOME / ".local/share/omni"
VENV = DATA / "venv"
POCKET_VENV = DATA / "pocket-venv"
ECHO_CANCEL = HOME / ".config/pipewire/pipewire.conf.d/omni-echo-cancel.conf"
SYSTEM_PACKAGES = {"python-gobject": "gi", "gtk4": None, "libadwaita": None, "mpv": "mpv", "wtype": "wtype",
                   "grim": "grim", "tesseract": "tesseract", "tesseract-data-eng": None, "foot": "foot",
                   "libnotify": "notify-send", "libsecret": "secret-tool", "pipewire": "pw-record"}


def launcher(module: str) -> str:
    # systemd user services get a minimal PATH; keep the one codex, claude, and omarchy-* were found on.
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin")
    return (f"#!/bin/sh\nexport PATH={shlex.quote(path)}\nexport PYTHONPATH={ROOT}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {VENV / 'bin/python'} -m {module} \"$@\"\n")


def echo_cancel() -> str | None:
    """The PipeWire echo-cancel drop-in for the devices set in config.toml, or None when they are not set."""
    from omni import config
    extra = config.load_settings().extra
    mic, speaker = extra.get("echo_cancel_mic"), extra.get("echo_cancel_speaker")
    if not mic or not speaker:
        return None
    return ((ROOT / "integration/pipewire/omni-echo-cancel.conf").read_text()
            .replace("@MIC@", str(mic)).replace("@SPEAKER@", str(speaker)))


def files() -> dict[Path, tuple[str, int]]:
    result = {
        HOME / ".local/bin/omni": (launcher("omni.cli"), 0o755),
        HOME / ".local/bin/omnid": (launcher("omni.daemon"), 0o755),
        HOME / ".config/hypr/omni.lua": ((ROOT / "integration/hypr/omni.lua").read_text(), 0o644),
    }
    entry = ((ROOT / "integration/omni.desktop").read_text()
             .replace("@OMNI_LAUNCHER@", str(HOME / ".local/bin/omni"))
             .replace("@OMNI_ICON@", str(ROOT / "assets/omni-icon.png")))
    result[HOME / ".local/share/applications/dev.omni.Omni.desktop"] = (entry, 0o644)
    for unit in (ROOT / "integration/systemd-user").iterdir():
        result[HOME / ".config/systemd/user" / unit.name] = (unit.read_text(), 0o644)
    for source in (ROOT / "integration/omarchy-plugin").iterdir():
        result[HOME / ".config/omarchy/plugins/local.omni" / source.name] = (source.read_text(), 0o644)
    if conf := echo_cancel():
        result[ECHO_CANCEL] = (conf, 0o644)
    return result


def missing_packages() -> list[str]:
    missing = []
    for package, probe in SYSTEM_PACKAGES.items():
        if probe == "gi":
            ok = subprocess.run(["/usr/bin/python3", "-c", "import gi; gi.require_version('Adw', '1')"],
                                capture_output=True).returncode == 0
        elif probe:
            ok = shutil.which(probe) is not None
        else:
            ok = subprocess.run(["pacman", "-Q", package], capture_output=True).returncode == 0 if shutil.which("pacman") else True
        if not ok:
            missing.append(package)
    return missing


def write(path: Path, content: str, mode: int, stamp: str) -> bool:
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = DATA / "install-backups" / stamp / path.relative_to(HOME)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    path.write_text(content)
    path.chmod(mode)
    return True


def make_venv() -> None:
    if not (VENV / "bin/python").exists():
        subprocess.run(["/usr/bin/python3", "-m", "venv", "--system-site-packages", str(VENV)], check=True)
    subprocess.run([str(VENV / "bin/pip"), "install", "--quiet", "--upgrade", "-r", str(ROOT / "requirements.txt")], check=True)
    subprocess.run([str(VENV / "bin/pip"), "install", "--quiet", "--upgrade", "--no-deps", "openwakeword>=0.6"], check=True)
    # Fetch openWakeWord's feature models and the default wake word once, so the daemon starts offline.
    subprocess.run([str(VENV / "bin/python"), "-c",
                    "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"], check=False)
    # And the streaming transcription model (stt = "moonshine").
    subprocess.run([str(VENV / "bin/python"), "-c", "import types; from omni.voice.moonshine import Moonshine; "
                    "Moonshine(types.SimpleNamespace(stt_model=''))._open()"], cwd=ROOT, check=False)


def make_pocket_venv() -> None:
    # Pocket TTS (the offline voice) needs torch; keep it in its own venv with the CPU-only build.
    if not (POCKET_VENV / "bin/python").exists():
        subprocess.run(["/usr/bin/python3", "-m", "venv", str(POCKET_VENV)], check=True)
    # Without the CPU index pip pulls in about 3 GB of CUDA packages.
    subprocess.run([str(POCKET_VENV / "bin/pip"), "install", "--quiet", "--upgrade", "pocket-tts==3.3.0",
                    "--extra-index-url", "https://download.pytorch.org/whl/cpu"], check=True)
    # Download the model and stock voice once, so Pocket loads offline when ElevenLabs is unreachable.
    subprocess.run([str(POCKET_VENV / "bin/python"), str(ROOT / "integration/pocket/server.py"), "--fetch"],
                   env={**os.environ, "HF_HUB_CACHE": str(DATA / "models/pocket-tts")}, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-venv", action="store_true", help="Do not create or update the Python venv")
    args = parser.parse_args()
    missing = missing_packages() if sys.platform.startswith("linux") else []
    if missing:
        print("Missing system packages (install them yourself):\n  sudo pacman -S --needed " + " ".join(missing))
    if not args.apply:
        print(f"Would create {VENV} and install requirements.txt into it, and {POCKET_VENV} for the offline voice")
        planned = files()
        for path in planned:
            print("Would install:", path)
        if ECHO_CANCEL not in planned:
            print("Skipping echo cancellation: set echo_cancel_mic and echo_cancel_speaker in config.toml (docs/voice.md)")
        print("Preview only. Run with --apply.")
        return
    if not args.skip_venv:
        make_venv()
        make_pocket_venv()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path, (content, mode) in files().items():
        if write(path, content, mode, stamp):
            print("Installed:", path)
            if path == ECHO_CANCEL:
                print("  Load it with: systemctl --user restart pipewire pipewire-pulse wireplumber"
                      " (drops audio for a moment and may reconnect Bluetooth)")
    print("""
Next:
  1. Add  require("hypr.omni")  to ~/.config/hypr/hyprland.lua (after the other requires), then remove old Omi bindings.
  2. systemctl --user disable --now omi-voice.service   # the old bridge, if installed
  3. systemctl --user daemon-reload && systemctl --user enable --now omnid.service omni-app.service
  4. omni key elevenlabs      # once, if the key is not already in the keyring from Omi
  5. omni doctor              # everything should be ✓ or an optional !
  6. omni ask "what time is it"
""")


if __name__ == "__main__":
    main()
