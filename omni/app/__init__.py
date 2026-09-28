"""Omni's GTK 4 / libadwaita app: the quick-ask popover and the full window.

One resident process (started at login with --background) so the popover appears
instantly: the Super+H binding runs `gapplication action dev.omni.Omni quick-ask`,
which only sends a D-Bus message to the running app.
"""

from __future__ import annotations

import ctypes
import logging
import sys

try:  # the voice indicator's layer shell must load before GTK pulls in libwayland-client
    ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)
except OSError:
    pass

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

APP_ID = "dev.omni.Omni"


class OmniApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.bridge = None
        self.window = None
        self.quick = None
        self.hud = None
        for name, callback in (("quick-ask", lambda *_: self.show_quick()), ("open", lambda *_: self.show_window())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def do_startup(self):
        Adw.Application.do_startup(self)
        from .bridge import Bridge
        from .theme import ThemeWatcher
        self.theme = ThemeWatcher()
        self.bridge = Bridge()
        self.hud = self._voice_hud()
        self.hold()  # stay resident when windows close, so the popover is instant

    def _voice_hud(self):
        from .. import config
        if not config.load_settings().extra.get("voice_hud", True):
            return None
        try:
            from .voice_hud import VoiceHud
            return VoiceHud.create(self, self.bridge)
        except (ImportError, ValueError) as exc:  # gtk4-layer-shell or pycairo missing
            logging.getLogger("omni.app").info("voice indicator unavailable: %s", exc)
            return None

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        if "--popover" in args:
            self.show_quick()
        elif "--background" not in args:
            self.show_window()
        return 0

    def show_window(self):
        if self.window is None:
            from .window import OmniWindow
            self.window = OmniWindow(self, self.bridge)
            self.window.set_hide_on_close(True)
        self.window.present()

    def show_quick(self):
        if self.quick is None:
            from .quick import QuickAsk
            self.quick = QuickAsk(self, self.bridge, self.show_window)
            self.quick.set_hide_on_close(True)
        self.quick.present_focused()


def main(argv: list[str] | None = None) -> int:
    return OmniApp().run([sys.argv[0], *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    raise SystemExit(main())
