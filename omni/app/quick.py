"""The quick-ask popover (Super+H): one input, the streaming reply, live state, inline approvals."""

from __future__ import annotations

from gi.repository import GLib, Gtk

STATE_LABELS = {"idle": "Ready", "listening": "Listening…", "thinking": "Thinking…", "working": "Working…",
                "speaking": "Speaking", "awaiting_approval": "Needs your OK", "offline": "Omni is not running"}


class StateIndicator(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=8, valign=Gtk.Align.CENTER)
        self.dot = Gtk.Box(css_classes=["omni-state-dot"], valign=Gtk.Align.CENTER)
        self.label = Gtk.Label(label="Ready", css_classes=["dim-label", "caption"])
        self.append(self.dot)
        self.append(self.label)
        self.state = "idle"

    def set_state(self, state: str, detail: str = "") -> None:
        self.dot.remove_css_class(self.state)
        self.state = state
        self.dot.add_css_class(state)
        text = STATE_LABELS.get(state, state)
        self.label.set_label(f"{text} · {detail}" if detail and state in ("working", "thinking") else text)


class ApprovalCard(Gtk.Box):
    """Shown for a pending approval or question from omnid; answers go back over the socket."""

    def __init__(self, bridge):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["omni-approval"], visible=False)
        self.bridge = bridge
        self.prompt: dict | None = None
        self.title = Gtk.Label(xalign=0, wrap=True, css_classes=["heading"])
        self.entry = Gtk.Entry(placeholder_text="Your answer", visible=False)
        self.entry.connect("activate", lambda *_: self._answer(self.entry.get_text()))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.cancel = Gtk.Button(label="Cancel")
        self.approve = Gtk.Button(label="Approve", css_classes=["suggested-action"])
        self.cancel.connect("clicked", lambda *_: self._answer("decline"))
        self.approve.connect("clicked", lambda *_: self._answer("accept" if self.prompt["kind"] == "approval" else self.entry.get_text()))
        buttons.append(self.cancel)
        buttons.append(self.approve)
        self.append(self.title)
        self.append(self.entry)
        self.append(buttons)

    def show_prompt(self, prompt: dict) -> None:
        self.prompt = prompt
        approval = prompt["kind"] == "approval"
        self.title.set_label(f"Omni wants to {prompt['text']}" if approval else prompt["text"])
        self.entry.set_visible(not approval)
        self.entry.set_text("")
        self.approve.set_label("Approve" if approval else "Send")
        self.cancel.set_label("Cancel" if approval else "Skip")
        self.set_visible(True)
        (self.entry if not approval else self.approve).grab_focus()

    def resolved(self, ident: str) -> None:
        if self.prompt and self.prompt["id"] == ident:
            self.prompt = None
            self.set_visible(False)

    def _answer(self, value: str) -> None:
        if self.prompt:
            self.bridge.call("answer", prompt=self.prompt["id"], value=value)


class QuickAsk(Gtk.ApplicationWindow):
    def __init__(self, app, bridge, open_full):
        super().__init__(application=app, title="Omni Quick Ask", decorated=False, resizable=False, default_width=680)
        self.add_css_class("omni-quick")
        self.bridge = bridge
        self.speak = True
        self.busy = False
        self.close_after_speech = False
        self._close_timer = 0

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=14, margin_bottom=14,
                      margin_start=16, margin_end=16)
        header = Gtk.Box(spacing=6)
        self.indicator = StateIndicator()
        header.append(self.indicator)
        header.append(Gtk.Box(hexpand=True))
        self.speak_button = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic", active=True, css_classes=["flat"],
                                             tooltip_text="Speak answers")
        self.speak_button.connect("toggled", self._toggle_speak)
        mic = Gtk.Button(icon_name="audio-input-microphone-symbolic", css_classes=["flat"], tooltip_text="Talk (push to talk)")
        mic.connect("clicked", lambda *_: bridge.call("listen"))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", css_classes=["flat"], tooltip_text="Stop")
        stop.connect("clicked", lambda *_: bridge.call("interrupt"))
        expand = Gtk.Button(icon_name="view-fullscreen-symbolic", css_classes=["flat"], tooltip_text="Open Omni")
        expand.connect("clicked", lambda *_: (self.set_visible(False), open_full()))
        for widget in (self.speak_button, mic, stop, expand):
            header.append(widget)

        self.entry = Gtk.Entry(placeholder_text="Ask Omni or tell it what to do…")
        self.entry.connect("activate", self._send)
        self.steps = Gtk.Label(xalign=0, css_classes=["omni-step"], ellipsize=3, visible=False)
        self.answer = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["omni-answer"])
        scroller = self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                                      max_content_height=420, visible=False)
        scroller.set_child(self.answer)
        self.approval = ApprovalCard(bridge)
        for widget in (header, self.entry, self.steps, scroller, self.approval):
            box.append(widget)
        self.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        bridge.listen(self._event)

    def present_focused(self) -> None:
        self._cancel_close()
        self.present()
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def _toggle_speak(self, button) -> None:
        self.speak = button.get_active()
        button.set_icon_name("audio-volume-high-symbolic" if self.speak else "audio-volume-muted-symbolic")

    def _key(self, controller, keyval, keycode, state) -> bool:
        if keyval == 0xff1b:  # Escape
            self.set_visible(False)
            return True
        return False

    def _send(self, *args) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        self._cancel_close()
        self.busy = True
        self.answer.set_label("")
        self.scroller.set_visible(False)
        self.steps.set_visible(False)
        self.set_default_size(680, -1)
        self.entry.set_text("")
        self.entry.set_placeholder_text(text)
        self.bridge.ask(text, self.speak, self._turn_event)

    def _turn_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "delta":
            self.answer.set_label(self.answer.get_label() + event["text"])
            self.scroller.set_visible(True)
        elif kind == "tool":
            self.steps.set_label(("✓ " if event.get("status") == "completed" else "… ") + event.get("label", "").replace("_", " "))
            self.steps.set_visible(True)
        elif kind == "done":
            self.busy = False
            self.entry.set_placeholder_text("Ask Omni or tell it what to do…")
            if not event.get("ok") or event.get("status") not in ("completed", None):
                self.answer.set_label(event.get("error") or f"Stopped ({event.get('status')})")
                self.scroller.set_visible(True)
                return
            reply = event.get("reply", "")
            if event.get("spoken"):
                self.close_after_speech = True
            elif self.steps.get_visible() and len(reply) < 80 and "?" not in reply:
                self._close_in(1500)  # a plain action: get out of the way

    def _event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "state":
            self.indicator.set_state(event["state"], event.get("detail", ""))
        elif kind == "connection" and not event["connected"]:
            self.indicator.set_state("offline")
        elif kind == "prompt":
            self._cancel_close()
            self.approval.show_prompt(event)
        elif kind == "prompt.resolved":
            self.approval.resolved(event["id"])
        elif kind == "listening" and event.get("transcript") and self.get_visible():
            self.entry.set_placeholder_text(event["transcript"])
        elif kind == "speaking" and not event["value"] and self.close_after_speech:
            self.close_after_speech = False
            self._close_in(2500)

    def _close_in(self, ms: int) -> None:
        self._cancel_close()
        self._close_timer = GLib.timeout_add(ms, self._auto_close)

    def _auto_close(self) -> bool:
        self._close_timer = 0
        if not self.approval.get_visible() and not self.busy and not self.entry.get_text():
            self.set_visible(False)
        return False

    def _cancel_close(self) -> None:
        if self._close_timer:
            GLib.source_remove(self._close_timer)
            self._close_timer = 0
