"""Allow by default; ask for the deny list; refuse shell edits to Omni's own state.

Codex sends every command and file change here before running it. The rules are
deliberately about consequences (deleting, installing, escalating, publishing),
not about which program is running, so ordinary work never prompts.
"""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config

ALLOW, ASK, DENY = "allow", "ask", "deny"

DEFAULT_ASK = {
    "sudo", "pkexec", "doas", "su", "run0",
    "pacman", "yay", "paru", "flatpak", "pip", "npm", "omarchy-pkg-add", "omarchy-pkg-drop",
    "omarchy-pkg-aur-add", "omarchy-update", "omarchy-reinstall",
    "shutdown", "reboot", "poweroff", "halt", "systemctl", "loginctl",
    "dd", "shred", "wipefs", "mkfs", "fdisk", "parted", "crontab",
    "msmtp", "sendmail",
}
# Subcommands of an ask-listed program that only read state.
READ_ONLY_SUBCOMMANDS = {
    "systemctl": {"status", "show", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "cat"},
    "pacman": {"-Q", "-Qi", "-Ql", "-Qs", "-Ss", "-Si", "-Qe", "-Qq", "-Qm"},
    "flatpak": {"list", "info", "search"},
    "pip": {"list", "show", "freeze"},
    "npm": {"ls", "list", "view", "run", "test", "outdated"},
    "yay": {"-Ss", "-Si", "-Qi", "-Q", "-Qs"},
    "paru": {"-Ss", "-Si", "-Qi", "-Q", "-Qs"},
    "loginctl": {"show-session", "list-sessions", "session-status"},
}
DELETERS = {"rm", "rmdir", "unlink", "trash", "trash-put", "gio", "srm"}
# himalaya reads freely; these words send mail or remove it from the server.
HIMALAYA_ASK = {"send", "reply", "forward", "write", "delete", "expunge", "purge", "smtp"}
GIT_ASK = [("push",), ("reset", "--hard"), ("clean",), ("branch", "-D"), ("filter-branch",), ("filter-repo",)]
WRAPPERS = {"command", "builtin", "exec", "nohup", "time", "nice", "ionice", "env", "stdbuf", "timeout", "xargs", "setsid", "uwsm-app", "uwsm"}
SHELLS = {"sh", "bash", "zsh", "dash", "fish"}
MUTATING = DELETERS | {"mv", "cp", "tee", "chmod", "chown", "truncate", "sed", "ln", "install", "sqlite3"}
DEFAULT_SAFE = ["~/Downloads", "/tmp"]


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""


@dataclass
class Policy:
    ask_commands: set[str] = field(default_factory=lambda: set(DEFAULT_ASK))
    safe_roots: list[Path] = field(default_factory=lambda: [Path(p).expanduser() for p in DEFAULT_SAFE])
    protected: list[Path] = field(default_factory=list)
    home: Path = field(default_factory=Path.home)

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        path = path or config.CONFIG / "policy.toml"
        policy = cls(protected=default_protected())
        if path.is_file():
            raw = tomllib.loads(path.read_text())
            policy.ask_commands |= set(raw.get("ask", []))
            policy.ask_commands -= set(raw.get("allow", []))
            policy.safe_roots += [Path(p).expanduser() for p in raw.get("safe_roots", [])]
        return policy

    # ---- commands -------------------------------------------------------

    def command(self, command: str, cwd: str | None = None) -> Decision:
        cwd_path = Path(cwd or self.home)
        try:
            simple = list(split_commands(unwrap_shell(command)))
        except ValueError:
            return self._fallback(command)
        decisions = [self._simple(argv, redirects, cwd_path) for argv, redirects in simple]
        for level in (DENY, ASK):
            for decision in decisions:
                if decision.action == level:
                    return decision
        return Decision(ALLOW)

    def _simple(self, argv: list[str], redirects: list[str], cwd: Path) -> Decision:
        argv = strip_wrappers(argv)
        for target in redirects:
            decision = self._write_target(target, cwd, "overwrite")
            if decision.action != ALLOW:
                return decision
        if not argv:
            return Decision(ALLOW)
        program = os.path.basename(argv[0])
        if program in SHELLS and "-c" in argv[1:-1]:
            return self.command(argv[argv.index("-c") + 1], str(cwd))
        if program in MUTATING and any(self._is_protected(arg, cwd) for arg in argv[1:] if not arg.startswith("-")):
            return Decision(DENY, "Omni's own service, memory, and credentials are changed through its tools, not the shell")
        if program in ("systemctl",) and any("omni" in arg for arg in argv[1:]) and not _read_only(program, argv):
            return Decision(DENY, "Omni does not stop or reconfigure its own service")
        if program in self.ask_commands or program.startswith("mkfs"):
            if _read_only(program, argv):
                return Decision(ALLOW)
            return Decision(ASK, f"run {program} ({' '.join(argv[1:4])})".strip())
        if program == "himalaya" and HIMALAYA_ASK & set(argv[1:]):
            return Decision(ASK, "send or delete mail with himalaya")
        if program == "git":
            args = [a for a in argv[1:] if not a.startswith("-C")]
            for pattern in GIT_ASK:
                if _contains_in_order(args, pattern):
                    return Decision(ASK, "git " + " ".join(pattern))
            return Decision(ALLOW)
        if program == "find" and ("-delete" in argv or "-exec" in argv and any(os.path.basename(a) in DELETERS for a in argv)):
            return Decision(ASK, "delete files found by find")
        if program in DELETERS:
            if program == "gio" and (len(argv) < 2 or argv[1] not in ("trash", "remove")):
                return Decision(ALLOW)
            targets = [a for a in argv[1:] if not a.startswith("-") and a not in ("trash", "remove")]
            outside = [t for t in targets if not self._is_safe(t, cwd)]
            if outside or not targets:
                return Decision(ASK, "delete " + (", ".join(outside[:3]) or "files"))
        if program == "mv" and len(argv) > 2:
            destination = _resolve(argv[-1], cwd, self.home)
            if destination.exists() and not destination.is_dir() and not self._is_safe(argv[-1], cwd):
                return Decision(ASK, f"overwrite {argv[-1]}")
        return Decision(ALLOW)

    def _fallback(self, command: str) -> Decision:
        words = set(re.findall(r"[A-Za-z0-9_.-]+", command))
        risky = words & (self.ask_commands | DELETERS | {"push"})
        if risky:
            return Decision(ASK, "run a command that uses " + ", ".join(sorted(risky)[:3]))
        return Decision(ALLOW)

    # ---- file changes ---------------------------------------------------

    def file_change(self, changes: list[dict], cwd: str | None = None) -> Decision:
        cwd_path = Path(cwd or self.home)
        for change in changes:
            path = change.get("path", "")
            kind = change.get("kind")
            kind = kind.get("type") if isinstance(kind, dict) else kind
            if self._is_protected(path, cwd_path):
                return Decision(DENY, "Omni's own state is changed through its tools")
            if kind in ("delete", "update") and not self._is_safe(path, cwd_path):
                return Decision(ASK, f"{'delete' if kind == 'delete' else 'edit'} {path}")
        return Decision(ALLOW)

    # ---- paths ----------------------------------------------------------

    def _is_safe(self, target: str, cwd: Path) -> bool:
        path = _resolve(target, cwd, self.home)
        roots = [*self.safe_roots, cwd] if cwd != self.home else self.safe_roots
        return any(_within(path, Path(os.path.normpath(root))) for root in roots)

    def _is_protected(self, target: str, cwd: Path) -> bool:
        if not target or target.startswith("-"):
            return False
        path = _resolve(target, cwd, self.home)
        return any(_within(path, root) for root in self.protected)

    def _write_target(self, target: str, cwd: Path, verb: str) -> Decision:
        if target in ("/dev/null", "/dev/stdout", "/dev/stderr") or target.startswith("&"):
            return Decision(ALLOW)
        if self._is_protected(target, cwd):
            return Decision(DENY, "Omni's own state is changed through its tools")
        path = _resolve(target, cwd, self.home)
        if path.is_file() and not self._is_safe(target, cwd):
            return Decision(ASK, f"{verb} {target}")
        return Decision(ALLOW)


def default_protected() -> list[Path]:
    home = Path.home()
    return [config.DATA, config.CONFIG / "policy.toml", home / ".ssh", home / ".gnupg",
            home / ".codex/auth.json", home / ".claude/.credentials.json",
            home / ".config/systemd/user/omnid.service", home / ".local/share/keyrings"]


def unwrap_shell(command: str) -> str:
    """Codex reports commands as `/bin/zsh -lc '<script>'`; policy judges the script."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if len(argv) >= 3 and os.path.basename(argv[0]) in SHELLS and argv[1] in ("-c", "-lc", "-lic", "-ic"):
        return argv[2]
    return command


def split_commands(script: str):
    """Yield (argv, redirect_targets) per simple command; raises ValueError if unparsable."""
    lexer = shlex.shlex(script.replace("\n", " ; "), posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv: list[str] = []
    redirects: list[str] = []
    expect_target = False
    for token in lexer:
        if expect_target:
            redirects.append(token)
            expect_target = False
        elif token in (">", ">>", ">|", "&>", "&>>", "2>", "2>>", "1>"):
            expect_target = True
            if argv and argv[-1].isdigit():
                argv.pop()
        elif token and set(token) <= set(";&|()"):
            if argv or redirects:
                yield argv, redirects
            argv, redirects = [], []
        elif token in ("<", "<<", "<<<"):
            continue
        elif token.startswith("$(") or token.startswith("`"):
            raise ValueError("command substitution")
        else:
            argv.append(token)
    if expect_target:
        raise ValueError("dangling redirect")
    if argv or redirects:
        yield argv, redirects


def strip_wrappers(argv: list[str]) -> list[str]:
    while argv:
        head = os.path.basename(argv[0])
        if "=" in argv[0] and not argv[0].startswith("="):
            argv = argv[1:]
        elif head in WRAPPERS:
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", argv[0]) or "=" in argv[0]):
                argv = argv[1:]
        else:
            break
    return argv


def _read_only(program: str, argv: list[str]) -> bool:
    allowed = READ_ONLY_SUBCOMMANDS.get(program)
    if not allowed:
        return False
    words = [a for a in argv[1:] if a not in ("--user", "--no-pager", "--quiet", "-q")]
    return bool(words) and words[0] in allowed


def _contains_in_order(args: list[str], pattern: tuple[str, ...]) -> bool:
    position = 0
    for arg in args:
        if arg == pattern[position]:
            position += 1
            if position == len(pattern):
                return True
    return False


def _resolve(target: str, cwd: Path, home: Path) -> Path:
    expanded = target.replace("$HOME", str(home))
    if expanded == "~" or expanded.startswith("~/"):
        expanded = str(home) + expanded[1:]
    path = Path(expanded)
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.normpath(path))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
