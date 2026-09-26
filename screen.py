"""Local OCR of the current desktop without saving a screenshot."""

from __future__ import annotations

import shutil
import subprocess


def read_text() -> str:
    if not shutil.which("grim") or not shutil.which("tesseract"):
        raise RuntimeError("Screen reading needs grim and Tesseract OCR")
    try:
        image = subprocess.run(["grim", "-"], capture_output=True, timeout=15, check=True).stdout
        if not image or len(image) > 32 * 1024 * 1024:
            raise RuntimeError("Screen capture was empty or too large")
        result = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng"], input=image, capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Screen OCR failed: {type(exc).__name__}") from exc
    content = result.stdout.decode("utf-8", errors="replace").strip()
    return content[:12000] if content else "No readable text was found on the current screen"
