#!/usr/bin/env python3
"""Keep general text dictation separate from Omi voice requests."""

from __future__ import annotations

import subprocess
import sys

import voice_bridge


def main() -> int:
    if voice_bridge.is_omi_recording() or voice_bridge.continuous_enabled():
        subprocess.Popen(["notify-send", "-a", "Omi", "Finish talking to Omi first", "Then start dictation with Super+Ctrl+X."], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 1
    result = subprocess.run(["voxtype", "record", "toggle"], capture_output=True, text=True, timeout=10)
    if result.returncode:
        print((result.stderr or result.stdout).strip(), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
