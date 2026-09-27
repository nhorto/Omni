"""Blocking client for omnid's socket, used by the CLI, the app, and hotkey scripts."""

from __future__ import annotations

import json
import socket
from typing import Iterator

from . import config


class DaemonUnavailable(ConnectionError):
    pass


class Client:
    def __init__(self, path=None, timeout: float | None = None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path or config.SOCKET))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise DaemonUnavailable("omnid is not running (systemctl --user start omnid)") from exc
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self._next = 0

    def close(self) -> None:
        self.reader.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def send(self, op: str, **params) -> int:
        self._next += 1
        self.sock.sendall((json.dumps({"id": self._next, "op": op, **params}) + "\n").encode())
        return self._next

    def messages(self) -> Iterator[dict]:
        for line in self.reader:
            yield json.loads(line)

    def stream(self, op: str, **params) -> Iterator[dict]:
        """Yield events for one request, then its response (a dict with 'ok')."""
        ident = self.send(op, **params)
        for message in self.messages():
            yield message
            if message.get("id") == ident and "ok" in message:
                return

    def call(self, op: str, **params):
        for message in self.stream(op, **params):
            if "ok" in message:
                if not message["ok"]:
                    raise RuntimeError(message.get("error", "request failed"))
                return message.get("result")
        raise DaemonUnavailable("omnid closed the connection")


def call(op: str, **params):
    with Client(timeout=30) as client:
        return client.call(op, **params)
