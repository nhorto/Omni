"""The voice indicator: a small pill at the bottom of the focused monitor while Omni hears you.

Level bars move with your voice and the live transcript follows along, driven by
omnid's `voice.capture`, `voice.level`, and `voice.partial` events (see listen.py,
which only sends them for speech addressed to Omni). During a conversation's
follow-up window it shows a quiet "Listening…". A gtk4-layer-shell overlay that
never takes keyboard focus and lets clicks through. Nothing runs while hidden.
"""

from __future__ import annotations

import logging
import math
import time

import cairo
import gi

gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell as LayerShell, Pango  # noqa: E402

log = logging.getLogger("omni.hud")
BARS = 7
BAR_WEIGHTS = [0.5, 0.75, 0.92, 1.0, 0.92, 0.75, 0.5]
FRAME_MS = 33
THINKING_SECONDS = 1.5
THINKING_MIN = 0.8     # the turn starts within milliseconds; keep the dots long enough to see
MAX_CHARS = 90
CSS = """
window.omni-hud { background: none; }
.omni-hud-pill { background: alpha(@window_bg_color, 0.94); color: @window_fg_color; border-radius: 22px;
                 border: 1px solid alpha(@accent_color, 0.45); padding: 9px 18px 9px 14px; margin: 10px;
                 box-shadow: 0 3px 10px alpha(black, 0.35); }
.omni-hud-bars { color: @accent_color; }
.omni-hud-text { font-size: 1.02em; }
.omni-hud-text.placeholder { opacity: 0.6; }
.omni-hud-text.thinking { opacity: 0.75; }
"""


def tail(text: str, limit: int = MAX_CHARS) -> str:
    """The last `limit` characters, cut at a word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return "…" + (cut.split(" ", 1)[1] if " " in cut else cut)


class VoiceHud:
    @classmethod
    def create(cls, app, bridge) -> VoiceHud | None:
        if not LayerShell.is_supported():
            log.info("voice indicator off: the compositor or process has no layer-shell support")
            return None
        return cls(app, bridge)

    def __init__(self, app, bridge):
        self.mode = "hidden"  # hidden | listening | hearing | end | thinking
        self.listening = False  # omnid's follow-up window (or a hotkey capture) is open
        self.text = ""
        self.target = self.level = 0.0
        self.heights = [0.0] * BARS
        self.changed_at = self.heard_at = self.last_frame = self.thinking_until = 0.0
        self._timer = 0

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        window = self.window = Gtk.Window(application=app, decorated=False, resizable=False, css_classes=["omni-hud"])
        window.set_title("Omni voice")
        LayerShell.init_for_window(window)
        LayerShell.set_namespace(window, "omni-voice")
        LayerShell.set_layer(window, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(window, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_margin(window, LayerShell.Edge.BOTTOM, 36)
        LayerShell.set_keyboard_mode(window, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(window, -1)  # float over panels instead of pushing them
        window.set_can_focus(False)

        pill = Gtk.Box(spacing=12, css_classes=["omni-hud-pill"], halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        self.bars = Gtk.DrawingArea(content_width=BARS * 4 + (BARS - 1) * 3, content_height=26,
                                    css_classes=["omni-hud-bars"], valign=Gtk.Align.CENTER)
        self.bars.set_draw_func(self._draw)
        self.label = Gtk.Label(xalign=0, wrap=True, lines=2, max_width_chars=46,
                               ellipsize=Pango.EllipsizeMode.END,
                               css_classes=["omni-hud-text"], valign=Gtk.Align.CENTER)
        pill.append(self.bars)
        pill.append(self.label)
        window.set_child(pill)
        window.connect("realize", self._realized)
        bridge.listen(self.on_event)

    def _realized(self, window) -> None:
        # An empty input region: clicks go to whatever is underneath. Re-applied after every layout.
        surface = window.get_surface()
        surface.set_input_region(cairo.Region())
        surface.connect("layout", lambda s, *_: s.set_input_region(cairo.Region()))

    # ---- events (GTK thread) ------------------------------------------------------

    def on_event(self, event: dict) -> None:
        kind = event.get("event")
        now = time.monotonic()
        if kind == "voice.capture":
            state = event.get("state")
            if state == "start":
                self._set_mode("hearing", "")
                self.heard_at = now
            elif state == "end" and self.mode == "hearing":
                self.target = 0.0
                self._set_mode("end")
            elif state == "sent":
                self._set_mode("thinking", event.get("text") or self.text)
            elif state == "dropped":
                self._done()
        elif kind == "voice.level" and self.mode == "hearing":
            self.target, self.heard_at = float(event.get("level", 0)), now
        elif kind == "voice.partial" and self.mode == "hearing":
            self._set_text(event.get("text", ""))
        elif kind == "listening":
            self.listening = bool(event.get("value"))
            if self.listening and self.mode == "hidden":
                self._set_mode("listening", "")
            elif not self.listening and self.mode == "listening":
                self._set_mode("hidden")
        elif kind == "turn.started" and self.mode == "thinking":
            self.thinking_until = min(self.thinking_until, max(now, self.changed_at + THINKING_MIN))
        elif kind == "connection" and not event.get("connected"):
            self.listening = False
            self._set_mode("hidden")

    def _done(self) -> None:
        """A capture is over: back to the quiet listening state if the window is still open, else away."""
        self._set_mode("listening" if self.listening else "hidden", "")

    def _set_mode(self, mode: str, text: str | None = None) -> None:
        self.mode, self.changed_at = mode, time.monotonic()
        if text is not None:
            self.text = text
        for name in ("placeholder", "thinking"):
            self.label.remove_css_class(name)
        if mode == "thinking":
            self.label.add_css_class("thinking")
            self.thinking_until = self.changed_at + THINKING_SECONDS
        if mode == "hidden":
            self._stop()
            self.window.set_visible(False)
            return
        self._set_text(self.text)
        if mode in ("listening", "end"):
            self.target = 0.0
        if not self.window.get_visible():
            self.heights = [0.0] * BARS
            self.level = 0.0
            self.window.set_visible(True)
        self._start()

    def _set_text(self, text: str) -> None:
        self.text = tail(text)
        if self.text:
            self.label.set_label(self.text)
            self.label.remove_css_class("placeholder")
        elif self.mode != "hidden":
            self.label.set_label("Thinking…" if self.mode == "thinking" else "Listening…")
            self.label.add_css_class("placeholder")

    # ---- animation ------------------------------------------------------------------

    def _start(self) -> None:
        if not self._timer:
            self.last_frame = time.monotonic()
            self._timer = GLib.timeout_add(FRAME_MS, self._tick)

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self) -> bool:
        now = time.monotonic()
        dt, self.last_frame = min(0.1, now - self.last_frame), now
        since = now - self.changed_at
        # Never stay up on a lost event: captures send levels continuously, and the rest end on their own.
        if (self.mode == "hearing" and now - self.heard_at > 3 or self.mode == "end" and since > 10
                or self.mode == "thinking" and now >= self.thinking_until):
            self._timer = 0
            self._done()
            return False  # _done started a fresh timer if it still shows
        self.level += (self.target - self.level) * min(1.0, dt * 14)
        for i, weight in enumerate(BAR_WEIGHTS):
            if self.mode == "hearing":
                wobble = 0.72 + 0.28 * math.sin(now * (7.0 + i * 1.3) + i * 1.9)
                goal = max(0.14, min(1.0, self.level * 1.25 * weight * wobble))
            else:  # a slow, low wave while waiting
                goal = 0.16 + 0.2 * (0.5 + 0.5 * math.sin(now * 3.2 - i * 0.7))
            rate = 22 if goal > self.heights[i] else 9
            self.heights[i] += (goal - self.heights[i]) * min(1.0, dt * rate)
        self.bars.queue_draw()
        return True

    def _draw(self, area, cr, width: int, height: int) -> None:
        color = area.get_color()
        bar, gap = 4, 3
        x0 = (width - (BARS * bar + (BARS - 1) * gap)) / 2
        if self.mode == "thinking":
            now = time.monotonic()
            for i in range(3):
                pulse = 0.5 + 0.5 * math.sin(now * 6 - i * 0.9)
                cr.set_source_rgba(color.red, color.green, color.blue, 0.35 + 0.65 * pulse)
                cr.arc(width / 2 + (i - 1) * 11, height / 2, 2.6 + 0.8 * pulse, 0, 2 * math.pi)
                cr.fill()
            return
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        for i, h in enumerate(self.heights):
            length = max(bar, h * height)
            x, y = x0 + i * (bar + gap), (height - length) / 2
            radius = bar / 2
            cr.new_sub_path()
            cr.arc(x + radius, y + radius, radius, math.pi, 0)
            cr.arc(x + radius, y + length - radius, radius, 0, math.pi)
            cr.close_path()
            cr.fill()
