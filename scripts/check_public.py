#!/usr/bin/env python3
"""Check tracked files for common accidental private data; never print matched values."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
blocked_names = {'settings.json', 'profile.md', 'personality.md', 'auth.json', 'credentials.json', 'secrets.json', '.env'}
blocked_suffixes = {'.sqlite3', '.sqlite', '.db', '.pem', '.key', '.bundle', '.jsonl', '.wav', '.mp3', '.onnx'}
patterns = [
    re.compile(rb'/home/' + rb'(?!USER(?:/|\b)|example(?:/|\b))[A-Za-z0-9_.-]+/'),
    re.compile(rb'gh[pousr]_' + rb'[A-Za-z0-9]{30,}'),
    re.compile(rb'sk-' + rb'[A-Za-z0-9_-]{30,}'),
    re.compile(rb'-----BEGIN ' + rb'(?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
]
failures = []
for name in filter(None, paths):
    path = root / name
    if path.name in blocked_names or path.suffix in blocked_suffixes or path.name.startswith('.env.'):
        failures.append((name, 'private data filename'))
    if path.is_file() and any(pattern.search(path.read_bytes()) for pattern in patterns):
        failures.append((name, 'possible private path or credential'))
for name, reason in failures:
    print(f'{name}: {reason}', file=sys.stderr)
print(f'Public-file check: {len(failures)} finding(s) across {sum(bool(p) for p in paths)} tracked files')
sys.exit(bool(failures))
