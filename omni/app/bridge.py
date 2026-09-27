"""Connects the GTK main loop to omnid: a subscriber thread for live events, worker threads for calls.

Every callback runs on the GTK thread (via GLib.idle_add), so widgets are only touched there.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from gi.repository import GLib

from ..client import Client, DaemonUnavailable


class Bridge:
    def __init__(self):
        self.listeners: list[Callable[[dict], None]] = []
        self.connected = False
        threading.Thread(target=self._subscribe, name="omni-events", daemon=True).start()

    def listen(self, callback: Callable[[dict], None]) -> None:
        self.listeners.append(callback)

    def _deliver(self, event: dict) -> bool:
        for callback in list(self.listeners):
            callback(event)
        return False

    def _subscribe(self) -> None:
        while True:
            try:
                with Client() as client:
                    self.connected = True
                    GLib.idle_add(self._deliver, {"event": "connection", "connected": True})
                    for message in client.stream("subscribe"):
                        if "event" in message:
                            GLib.idle_add(self._deliver, message)
            except (DaemonUnavailable, ConnectionError, OSError):
                pass
            if self.connected:
                self.connected = False
                GLib.idle_add(self._deliver, {"event": "connection", "connected": False})
            time.sleep(2)

    def call(self, op: str, done: Callable | None = None, failed: Callable | None = None, **params) -> None:
        """Run one request off the GTK thread; report the result back on it."""
        def work():
            try:
                with Client(timeout=60) as client:
                    result = client.call(op, **params)
                if done:
                    GLib.idle_add(lambda: done(result) and False)
            except Exception as exc:
                if failed:
                    GLib.idle_add(lambda: failed(str(exc)) and False)
        threading.Thread(target=work, daemon=True).start()

    def ask(self, text: str, speak: bool, on_event: Callable[[dict], None], source: str = "app") -> None:
        """Stream one request; `on_event` sees this turn's events and finally {'event': 'done', ...}."""
        def work():
            try:
                with Client() as client:
                    for message in client.stream("ask", text=text, speak=speak, source=source):
                        if "ok" in message:
                            final = {"event": "done", "ok": message["ok"], "error": message.get("error"),
                                     **(message.get("result") or {})}
                            GLib.idle_add(lambda m=final: on_event(m) and False)
                        else:
                            GLib.idle_add(lambda m=message: on_event(m) and False)
            except (DaemonUnavailable, ConnectionError, OSError) as exc:
                GLib.idle_add(lambda: on_event({"event": "done", "ok": False, "error": str(exc)}) and False)
        threading.Thread(target=work, daemon=True).start()
