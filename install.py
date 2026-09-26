#!/usr/bin/env python3
"""Preview or install user integration; never install packages or change keybindings."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shlex
import shutil

ROOT = Path(__file__).resolve().parent


def desktop_quote(value: str) -> str:
    value = value.replace('%', '%%')
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\' + char)
    return '"' + value + '"'


def files(root: Path, home: Path) -> dict[Path, tuple[str, int]]:
    result = {}
    for name, script in (("omi", "omi_app.py"), ("omi-voice", "voice_bridge.py")):
        result[home / '.local/bin' / name] = ('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(root / script)) + ' "$@"\n', 0o755)
    entry = (root / 'integration/omi.desktop').read_text().replace('@OMI_LAUNCHER@', desktop_quote(str(home / '.local/bin/omi')))
    result[home / '.local/share/applications/omi.desktop'] = (entry, 0o644)
    result[home / '.config/systemd/user/omi-voice.service'] = ((root / 'integration/systemd-user/omi-voice.service').read_text(), 0o644)
    for source in (root / 'integration/omarchy-plugin').iterdir():
        if source.is_file():
            result[home / '.config/omarchy/plugins/local.omi' / source.name] = (source.read_text(), 0o644)
    return result


def install(root: Path, home: Path) -> list[Path]:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    changed = []
    for path, (content, mode) in files(root, home).items():
        if path.exists() and path.read_text() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = home / '.local/share/omi/integration-backups' / stamp / path.relative_to(home)
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, backup)
        path.write_text(content)
        path.chmod(mode)
        changed.append(path)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Write launchers, desktop entry, service, and plugin; back up replaced files')
    args = parser.parse_args()
    if args.apply:
        for path in install(ROOT, Path.home()):
            print('Installed:', path)
        print('Next: systemctl --user daemon-reload')
        print('Then: systemctl --user enable --now omi-voice.service')
        print('See integration/README.md for optional bar and hotkey setup.')
    else:
        for path in files(ROOT, Path.home()):
            print('Would install:', path)
        print('Preview only. Use --apply to install. No packages, hotkeys, or services are started by this script.')


if __name__ == '__main__':
    main()
