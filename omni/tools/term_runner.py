"""Runs inside a terminal Omni opened: shows the command's output live, tees it to a log,
and writes an exit record. Usage: term_runner LOG RESULT --capture|--interactive --hold|--no-hold COMMAND"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    log, result, mode, hold, command = Path(argv[0]), Path(argv[1]), argv[2], argv[3] == "--hold", argv[4]
    shell = os.environ.get("SHELL", "/bin/bash")
    with open(log, "wb") as out:
        os.chmod(log, 0o600)
        if mode == "--interactive":
            # Interactive programs (codex, claude, editors) need the real TTY; we only log that they ran.
            out.write(f"$ {command}\n(interactive session; output is on screen only)\n".encode())
            out.flush()
            code = subprocess.run([shell, "-lc", command]).returncode
        else:
            process = subprocess.Popen([shell, "-lc", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert process.stdout is not None
            while chunk := os.read(process.stdout.fileno(), 4096):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                out.write(chunk)
                out.flush()
            code = process.wait()
    temporary = result.with_suffix(".tmp")
    temporary.write_text(json.dumps({"exit_code": code}))
    temporary.replace(result)
    if hold:
        print(f"\n[exited with status {code}; this terminal stays open]", flush=True)
        os.execvp(shell, [shell, "-l"])
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
