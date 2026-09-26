#!/usr/bin/env python3
"""Run an argv command inside Foot and leave an atomic, private exit record."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run(result_path: Path, argv: list[str]) -> int:
    tail = bytearray()
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=None)
        assert process.stdout is not None
        while chunk := os.read(process.stdout.fileno(), 4096):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            tail.extend(chunk)
            if len(tail) > 4000:
                del tail[:-4000]
        process.stdout.close()
        code = process.wait()
    except OSError as exc:
        code = 127
        tail = bytearray(f"{type(exc).__name__}: {exc}".encode())
        print(tail.decode(errors="replace"), file=sys.stderr)
    result = {"exit_code": code, "output": tail.decode(errors="replace")}
    temporary = result_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.chmod(0o600)
    temporary.replace(result_path)
    print(f"\n[Omi command exited with status {code}]", flush=True)
    return code


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("Usage: terminal_job.py RESULT_PATH COMMAND [ARGS...]")
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2:]))
