"""The full Omni window: conversation, activity, memory, skills, history, settings, setup.

Every page renders from omnid over the socket and updates from its event stream;
nothing polls on a timer.
"""

from __future__ import annotations

import time
from datetime import datetime

from gi.repository import Adw, Gtk, Pango

from .quick import ApprovalCard, StateIndicator

PAGES = [("conversation", "Conversation", "user-available-symbolic"),
         ("activity", "Activity", "utilities-system-monitor-symbolic"),
         ("memory", "Memory", "user-bookmarks-symbolic"),
         ("skills", "Skills", "applications-engineering-symbolic"),
         ("history", "History", "document-open-recent-symbolic"),
         ("settings", "Settings", "emblem-system-symbolic"),
         ("setup", "Check setup", "emblem-ok-symbolic")]


def label(text: str = "", *classes: str, **kwargs) -> Gtk.Label:
    options = {"xalign": 0, "wrap": True, "wrap_mode": Pango.WrapMode.WORD_CHAR, **kwargs}
    return Gtk.Label(label=text, css_classes=list(classes), **options)


def clock(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp)
    return moment.strftime("%-I:%M %p") if moment.date() == datetime.now().date() else moment.strftime("%b %-d, %-I:%M %p")


def page(title: str, content: Gtk.Widget, *header_widgets: Gtk.Widget, bottom: Gtk.Widget | None = None) -> Adw.ToolbarView:
    view = Adw.ToolbarView()
    header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title=title))
    for widget in header_widgets:
        header.pack_end(widget)
    view.add_top_bar(header)
    view.set_content(content)
    if bottom:
        view.add_bottom_bar(bottom)
    return view


# ---- conversation ----------------------------------------------------------------------

class TurnRow(Gtk.Box):
    def __init__(self, episode: int, request: str, source: str, started: float):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=4)
        self.episode = episode
        spoken = source == "voice"
        user = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, css_classes=["omni-bubble", "omni-user"])
        user.append(label(request, selectable=True))
        user.append(label(("🎙 " if spoken else "") + clock(started), "caption", "dim-label", xalign=1))
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.expander = Gtk.Expander(label="", child=self.steps_box, visible=False, css_classes=["omni-step"])
        self.reply = label("", selectable=True)
        self.meta = label("", "caption", "dim-label")
        reply_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.START,
                            css_classes=["omni-bubble", "omni-reply"], visible=False)
        reply_box.append(self.reply)
        reply_box.append(self.meta)
        self.reply_box = reply_box
        self.steps: dict[str, Gtk.Label] = {}
        for widget in (user, self.expander, reply_box):
            self.append(widget)

    def step(self, event: dict) -> None:
        mark = {"running": "…", "completed": "✓", "failed": "✗", "declined": "⊘"}.get(event.get("status"), "·")
        text = f"{mark} {event.get('type', '')}: {event.get('label', '')}"
        if event["id"] in self.steps:
            self.steps[event["id"]].set_label(text)
        else:
            self.steps[event["id"]] = label(text, "omni-step", ellipsize=Pango.EllipsizeMode.END, wrap=False)
            self.steps_box.append(self.steps[event["id"]])
        count = len(self.steps)
        self.expander.set_label(f"{count} step{'s' if count != 1 else ''}")
        self.expander.set_visible(True)

    def delta(self, text: str) -> None:
        self.reply.set_label(self.reply.get_label() + text)
        self.reply_box.set_visible(True)

    def finish(self, event: dict) -> None:
        if event.get("reply") and not self.reply.get_label():
            self.reply.set_label(event["reply"])
        if event.get("status") not in ("completed", None):
            self.reply.set_label(self.reply.get_label() or event.get("error") or event["status"].capitalize())
        self.reply_box.set_visible(bool(self.reply.get_label()))
        timings = event.get("timings") or {}
        bits = [f"{timings['completed'] / 1000:.1f}s"] if "completed" in timings else []
        if event.get("spoken"):
            bits.append("spoken")
        self.meta.set_label(" · ".join(bits))


class ConversationPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.rows: dict[int, TurnRow] = {}
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=18, margin_end=18, margin_bottom=12)
        clamp = Adw.Clamp(maximum_size=820, child=self.list)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
        # Follow new content while the view is at the bottom; stop following once Nick scrolls up.
        self.follow = True
        adjustment = self.scroller.get_vadjustment()
        adjustment.connect("changed", lambda adj: self.follow and adj.set_value(adj.get_upper() - adj.get_page_size()))
        adjustment.connect("value-changed", lambda adj: setattr(
            self, "follow", adj.get_value() >= adj.get_upper() - adj.get_page_size() - 48))
        self.empty = Adw.StatusPage(icon_name="user-available-symbolic", title="Ask Omni anything",
                                    description="Type below, press Super+H anywhere, or say the wake word.", vexpand=True)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.scroller, "list")
        self.approval = ApprovalCard(self.bridge)

        composer = Gtk.Box(spacing=8, margin_top=8, margin_bottom=10, margin_start=12, margin_end=12)
        self.entry = Gtk.Entry(placeholder_text="Message Omni…", hexpand=True)
        self.entry.connect("activate", self._send)
        mic = Gtk.Button(icon_name="audio-input-microphone-symbolic", tooltip_text="Talk", css_classes=["circular"])
        mic.connect("clicked", lambda *_: self.bridge.call("listen"))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop", css_classes=["circular"])
        stop.connect("clicked", lambda *_: self.bridge.call("interrupt"))
        send = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Send", css_classes=["circular", "suggested-action"])
        send.connect("clicked", self._send)
        for widget in (self.entry, mic, stop, send):
            composer.append(widget)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12)
        bottom.append(self.approval)
        bottom.append(composer)

        new = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New conversation")
        new.connect("clicked", self._new)
        self.append(page("Conversation", self.stack, new, bottom=bottom))
        self.bridge.listen(self._event)
        self.load()

    def load(self) -> None:
        self.bridge.call("episodes", done=self._loaded, limit=40)

    def _loaded(self, episodes: list) -> None:
        start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        for item in reversed(episodes):
            if item["kind"] != "turn" or item["started_at"] < start or item["id"] in self.rows:
                continue
            row = self._add(item["id"], item["request"], item["source"], item["started_at"])
            for tool in item["tools"]:
                row.step(tool)
            row.finish({"reply": item["reply"], "status": item["status"], "timings": item["timings"], "error": ""})

    def _add(self, episode: int, request: str, source: str, started: float) -> TurnRow:
        row = TurnRow(episode, request, source, started)
        self.rows[episode] = row
        self.list.append(row)
        self.stack.set_visible_child_name("list")
        return row

    def _event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "turn.started":
            self._add(event["episode"], event["request"], event.get("source", "text"), event.get("at", time.time()))
        elif kind == "prompt":
            self.approval.show_prompt(event)
        elif kind == "prompt.resolved":
            self.approval.resolved(event["id"])
        row = self.rows.get(event.get("episode"))
        if row is None:
            return
        if kind == "tool" and event.get("kind") == "foreground":
            row.step(event)
        elif kind == "delta" and event.get("kind") == "foreground":
            row.delta(event["text"])
        elif kind == "turn.completed":
            row.finish(event)

    def _send(self, *args) -> None:
        text = self.entry.get_text().strip()
        if text:
            self.entry.set_text("")
            self.bridge.ask(text, False, self._done)

    def _done(self, event: dict) -> None:
        if event.get("event") == "done" and not event.get("ok"):
            self.window.toast(event.get("error") or "Request failed")

    def _new(self, *args) -> None:
        self.bridge.call("new_thread", done=lambda _: self.window.toast("Started a fresh conversation. Memory carries over."))


# ---- activity ---------------------------------------------------------------------------

class ActivityPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.prefs = Adw.PreferencesPage()
        self.usage = Adw.PreferencesGroup(title="Today")
        self.usage_row = Adw.ActionRow(title="Tokens", use_markup=False)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=180)
        self.usage_row.add_suffix(self.level)
        self.usage.add(self.usage_row)
        self.delegations = Adw.PreferencesGroup(title="Delegated work", description="Agents working for Omni")
        self.turns = Adw.PreferencesGroup(title="Recent turns", description="Latency per stage and tokens")
        for group in (self.usage, self.delegations, self.turns):
            self.prefs.add(group)
        self.rows: list = []
        self.delegation_rows: list = []
        self.append(page("Activity", self.prefs))
        self.bridge.listen(self._event)
        self.refresh()

    def refresh(self) -> None:
        self.bridge.call("status", done=self._status)
        self.bridge.call("episodes", done=self._episodes, limit=25)

    def _status(self, status: dict) -> None:
        used, budget = status["tokens_today"]["billable"], status["budget"] or 1
        self.usage_row.set_subtitle(f"{used / 1000:.0f}k of {budget / 1000:.0f}k budget · "
                                    f"{status['tokens_today']['cached'] / 1000:.0f}k served from cache · model {status['model']}")
        self.level.set_value(min(1, used / budget))
        for row in self.delegation_rows:
            self.delegations.remove(row)
        self.delegation_rows = []
        for item in reversed(status["delegations"][-10:]):
            row = Adw.ActionRow(title=item["task"][:120], use_markup=False,
                                subtitle=f"{item['mode']} · {item['status']}" + (f" · {item['summary']}" if item["summary"] else ""))
            if item["status"] == "running" and item["mode"] == "background":
                stop = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
                stop.connect("clicked", lambda _b, ident=item["id"]: self.bridge.call("delegation.stop", delegation=ident))
                row.add_suffix(stop)
            self.delegations.add(row)
            self.delegation_rows.append(row)
        self.delegations.set_visible(bool(self.delegation_rows))

    def _episodes(self, episodes: list) -> None:
        for row in self.rows:
            self.turns.remove(row)
        self.rows = []
        for item in episodes:
            t = item["timings"]
            parts = [clock(item["started_at"]), item["source"]]
            for key, name in (("first_token", "first token"), ("first_audio", "audio"), ("completed", "done")):
                if key in t:
                    parts.append(f"{name} {t[key] / 1000:.1f}s")
            parts.append(f"{item['tokens_in'] / 1000:.1f}k in ({item['tokens_cached'] / 1000:.1f}k cached)")
            if item["tools"]:
                parts.append(f"{len(item['tools'])} step{'s' if len(item['tools']) != 1 else ''}")
            if item["status"] != "completed":
                parts.append(item["status"])
            row = Adw.ActionRow(title=item["request"][:140], subtitle=" · ".join(parts), use_markup=False)
            self.turns.add(row)
            self.rows.append(row)

    def _event(self, event: dict) -> None:
        if event.get("event") in ("turn.completed", "delegation", "budget"):
            self.refresh()


# ---- memory and skills -------------------------------------------------------------------

class MemoryPage(Gtk.Box):
    STORES = (("user", "About Nick", "USER.md · facts, people, preferences"),
              ("memory", "Omni's notes", "MEMORY.md · how to do things for Nick, lessons learned"))

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.prefs = Adw.PreferencesPage()
        self.groups, self.rows = {}, {"user": [], "memory": []}
        for store, title, description in self.STORES:
            group = Adw.PreferencesGroup(title=title, description=description)
            add = Adw.EntryRow(title="Add a note", show_apply_button=True)
            add.connect("apply", lambda row, s=store: self._add(s, row))
            group.add(add)
            self.groups[store] = group
            self.prefs.add(group)
        self.append(page("Memory", self.prefs))
        self.bridge.listen(lambda e: e.get("event") == "memory.changed" and self.refresh())
        self.refresh()

    def refresh(self) -> None:
        self.bridge.call("memory.list", done=self._render)

    def _render(self, stores: dict) -> None:
        for store, entries in stores.items():
            group = self.groups[store]
            for row in self.rows[store]:
                group.remove(row)
            self.rows[store] = []
            for entry in reversed(entries):
                origin = f"saved {entry['at']}" if entry["at"] else "added by hand"
                if entry.get("episode"):
                    origin += f" · from conversation #{entry['episode']}"
                row = Adw.ActionRow(title=entry["text"], subtitle=origin, use_markup=False, title_lines=0)
                edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Edit")
                edit.connect("clicked", lambda _b, e=entry: self._edit(e))
                delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Forget")
                delete.connect("clicked", lambda _b, e=entry: self._forget(e))
                row.add_suffix(edit)
                row.add_suffix(delete)
                group.add(row)
                self.rows[store].append(row)

    def _add(self, store: str, row: Adw.EntryRow) -> None:
        text = row.get_text().strip()
        if text:
            row.set_text("")
            self.bridge.call("memory.save", store=store, text=text, failed=self.window.toast)

    def _edit(self, entry: dict) -> None:
        field = Gtk.Entry(text=entry["text"], activates_default=True)
        dialog = Adw.AlertDialog(heading="Edit note", extra_child=field)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.connect("response", lambda _d, response: response == "save" and self.bridge.call(
            "memory.update", entry=entry["id"], text=field.get_text(), failed=self.window.toast))
        dialog.present(self.window)

    def _forget(self, entry: dict) -> None:
        def forgotten(removed):
            self.window.toast("Forgot: " + removed["text"][:60],
                              undo=lambda: self.bridge.call("memory.restore", entry=removed))
        self.bridge.call("memory.forget", entry=entry["id"], done=forgotten, failed=self.window.toast)


class SkillsPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.prefs = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Skills Omni saved",
                                          description="Procedures Omni wrote after tasks that worked. Codex reads them too.")
        self.prefs.add(self.group)
        self.rows: list = []
        self.empty = Adw.ActionRow(title="No skills yet", subtitle="Omni saves one after a multi-step task it expects to repeat.")
        self.append(page("Skills", self.prefs))
        self.bridge.listen(lambda e: e.get("event") == "memory.changed" and self.refresh())
        self.refresh()

    def refresh(self) -> None:
        self.bridge.call("skills.list", done=self._render)

    def _render(self, skills: list) -> None:
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        for skill in skills or [None]:
            if skill is None:
                row = self.empty
            else:
                row = Adw.ActionRow(title=skill["name"], subtitle=skill["description"], activatable=True, use_markup=False)
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _r, name=skill["name"]: self._open(name))
            self.group.add(row)
            self.rows.append(row)

    def _open(self, name: str) -> None:
        self.bridge.call("skills.read", name=name, done=lambda text: self._editor(name, text))

    def _editor(self, name: str, text: str) -> None:
        view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
                            left_margin=8, right_margin=8)
        view.get_buffer().set_text(text)
        scroller = Gtk.ScrolledWindow(child=view, min_content_height=360, min_content_width=560)
        dialog = Adw.AlertDialog(heading=name, extra_child=scroller)
        dialog.add_response("delete", "Delete")
        dialog.add_response("cancel", "Close")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def respond(_dialog, response):
            buffer = view.get_buffer()
            if response == "save":
                body = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
                self.bridge.call("skills.write", name=name, text=body, failed=self.window.toast)
            elif response == "delete":
                self.bridge.call("skills.delete", name=name, failed=self.window.toast, done=lambda old: self.window.toast(
                    f"Deleted {name}", undo=lambda: self.bridge.call("skills.restore", name=name, text=old)))
        dialog.connect("response", respond)
        dialog.present(self.window)


class HistoryPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.search = Gtk.SearchEntry(placeholder_text="Search everything Omni has done and heard", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.refresh())
        self.prefs = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup()
        self.prefs.add(self.group)
        self.rows: list = []
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Adw.Clamp(maximum_size=700, child=self.search, margin_top=12, margin_start=12, margin_end=12)
        box.append(bar)
        box.append(self.prefs)
        self.prefs.set_vexpand(True)
        self.append(page("History", box))
        self.refresh()

    def refresh(self) -> None:
        query = self.search.get_text().strip()
        if query:
            self.bridge.call("memory.search", query=query, done=lambda r: self._render(r["episodes"], r["facts"]))
        else:
            self.bridge.call("episodes", limit=60, done=lambda e: self._render(e, []))

    def _render(self, episodes: list, facts: list) -> None:
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        for fact in facts:
            row = Adw.ActionRow(title=fact["text"], subtitle="Saved note", use_markup=False)
            self.group.add(row)
            self.rows.append(row)
        for item in episodes:
            row = Adw.ExpanderRow(title=item["request"][:160], use_markup=False,
                                  subtitle=f"{clock(item['started_at'])} · {item['source']} · {item['kind']}")
            row.add_row(Gtk.ListBoxRow(child=label(item["reply"] or "(no reply)", selectable=True, margin_top=8,
                                                  margin_bottom=8, margin_start=12, margin_end=12), activatable=False))
            self.group.add(row)
            self.rows.append(row)


# ---- settings and setup -------------------------------------------------------------------

POLICY_TEMPLATE = """\
# Omni asks before these; everything else runs without prompting.
# ask = ["docker", "rclone"]            # add programs that should always ask
# allow = ["systemctl"]                 # remove built-in ones you trust
# safe_roots = ["~/scratch"]            # folders where deleting never asks
"""


class SettingsPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.prefs = Adw.PreferencesPage()
        self.append(page("Settings", self.prefs))
        self.bridge.call("settings.get", done=self._build)

    def _set(self, key, value) -> None:
        self.bridge.call("settings.set", key=key, value=value, failed=self.window.toast)

    def _build(self, s: dict) -> None:
        agent = Adw.PreferencesGroup(title="Agent", description="Runs on your ChatGPT (Codex) or Claude sign-in; memory and skills are shared")
        agents = ["codex", "claude"]
        which = Adw.ComboRow(title="Assistant", model=Gtk.StringList.new(["Codex (ChatGPT)", "Claude"]),
                             selected=agents.index(s["agent"]) if s["agent"] in agents else 0)
        which.connect("notify::selected", lambda row, _p: self._set("agent", agents[row.get_selected()]))
        agent.add(which)
        model = Adw.EntryRow(title="Model (blank = fastest available)", text=s["model"] or "", show_apply_button=True)
        model.connect("apply", lambda row: self._set("model", row.get_text().strip() or None))
        efforts = ["low", "medium", "high"]
        effort = Adw.ComboRow(title="Thinking effort for voice turns", model=Gtk.StringList.new(efforts),
                              selected=efforts.index(s["effort"]) if s["effort"] in efforts else 0)
        effort.connect("notify::selected", lambda row, _p: self._set("effort", efforts[row.get_selected()]))
        budget = self._spin("Daily token budget (thousands)", s["daily_token_budget"] // 1000, 100, 50000, 100,
                            lambda v: self._set("daily_token_budget", int(v) * 1000))
        for row in (model, effort, budget):
            agent.add(row)

        voice = Adw.PreferencesGroup(title="Voice")
        speech = Adw.SwitchRow(title="Speak answers", subtitle="Omni stays quiet after actions unless it asks something",
                               active=s["speech"])
        speech.connect("notify::active", lambda row, _p: self._set("speech", row.get_active()))
        wake = Adw.SwitchRow(title="Wake word", subtitle=f"Model: {s['wake_model']}", active=s["wake_word"])
        wake.connect("notify::active", lambda row, _p: (self._set("wake_word", row.get_active()),
                                                        self.bridge.call("voice", setting="wake", value=row.get_active())))
        continuous = Adw.SwitchRow(title="Hands-free (continuous) listening", subtitle="Any speech becomes a request")
        self.bridge.call("status", done=lambda status: continuous.set_active(bool(status["voice"].get("continuous"))))
        continuous.connect("notify::active", lambda row, _p: self.bridge.call("voice", setting="continuous", value=row.get_active(),
                                                                              failed=self.window.toast))
        voice_id = Adw.EntryRow(title="ElevenLabs voice ID", text=s["elevenlabs_voice_id"], show_apply_button=True)
        voice_id.connect("apply", lambda row: self._set("elevenlabs_voice_id", row.get_text().strip()))
        key = Adw.ActionRow(title="ElevenLabs API key", use_markup=False,
                            subtitle="Stored in the keyring" if s["elevenlabs_key_set"] else "Not set: run `omni key elevenlabs` in a terminal")
        test = Gtk.Button(label="Test voice", valign=Gtk.Align.CENTER)
        test.connect("clicked", lambda *_: self.bridge.call("say", text="Hi Nick, this is how I sound."))
        key.add_suffix(test)
        for row in (speech, wake, continuous, voice_id, key):
            voice.add(row)

        threads = Adw.PreferencesGroup(title="Conversation length",
                                       description="Omni starts a fresh conversation after these; memory carries over")
        idle = self._spin("Idle minutes", s["idle_minutes"], 5, 480, 5, lambda v: self._set("idle_minutes", int(v)))
        ceiling = self._spin("Context ceiling (thousand tokens)", s["context_ceiling"] // 1000, 20, 250, 10,
                             lambda v: self._set("context_ceiling", int(v) * 1000))
        threads.add(idle)
        threads.add(ceiling)

        work = Adw.PreferencesGroup(title="Delegation and approvals")
        modes = ["background", "visible"]
        mode = Adw.ComboRow(title="Default for delegated work", model=Gtk.StringList.new(modes),
                            selected=modes.index(s["delegate_default"]) if s["delegate_default"] in modes else 0)
        mode.connect("notify::selected", lambda row, _p: self._set("delegate_default", modes[row.get_selected()]))
        rules = Adw.ActionRow(title="Approval rules", subtitle="Commands Omni asks about before running", activatable=True)
        rules.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        rules.connect("activated", lambda *_: self.bridge.call("policy.get", done=self._policy_editor))
        work.add(mode)
        work.add(rules)
        for group in (agent, voice, threads, work):
            self.prefs.add(group)

    def _spin(self, title, value, low, high, step, changed) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(low, high, step)
        row.set_title(title)
        row.set_value(value)
        row.connect("notify::value", lambda r, _p: changed(r.get_value()))
        return row

    def _policy_editor(self, policy: dict) -> None:
        view = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        view.get_buffer().set_text(policy["text"] or POLICY_TEMPLATE)
        scroller = Gtk.ScrolledWindow(child=view, min_content_height=260, min_content_width=560)
        dialog = Adw.AlertDialog(heading="Approval rules", body="Built in: " + ", ".join(policy["ask"][:18]) + " …",
                                 extra_child=scroller)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def respond(_dialog, response):
            if response == "save":
                buffer = view.get_buffer()
                text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
                self.bridge.call("policy.set", text=text, done=lambda _: self.window.toast("Saved approval rules"),
                                 failed=self.window.toast)
        dialog.connect("response", respond)
        dialog.present(self.window)


class SetupPage(Gtk.Box):
    ICONS = {"ok": "emblem-ok-symbolic", "warn": "dialog-warning-symbolic", "fail": "dialog-error-symbolic"}

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.bridge = window, window.bridge
        self.prefs = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Setup", description="What Omni can use on this machine")
        self.prefs.add(self.group)
        self.rows: list = []
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Check again")
        refresh.connect("clicked", lambda *_: self.refresh())
        self.append(page("Check setup", self.prefs, refresh))
        self.refresh()

    def refresh(self) -> None:
        self.bridge.call("doctor", done=self._render, failed=lambda error: self._render([["omnid", "fail", error]]))

    def _render(self, checks: list) -> None:
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        for name, status, hint in checks:
            row = Adw.ActionRow(title=name, subtitle=hint, use_markup=False)
            icon = Gtk.Image(icon_name=self.ICONS.get(status, "dialog-question-symbolic"))
            if status != "ok":
                icon.add_css_class("warning" if status == "warn" else "error")
            else:
                icon.add_css_class("success")
            row.add_prefix(icon)
            self.group.add(row)
            self.rows.append(row)


# ---- window -------------------------------------------------------------------------------

class OmniWindow(Adw.ApplicationWindow):
    def __init__(self, app, bridge):
        super().__init__(application=app, title="Omni", default_width=1100, default_height=760)
        self.bridge = bridge
        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.pages = {}
        for key, title, _icon in PAGES:
            widget = {"conversation": ConversationPage, "activity": ActivityPage, "memory": MemoryPage,
                      "skills": SkillsPage, "history": HistoryPage, "settings": SettingsPage, "setup": SetupPage}[key](self)
            self.pages[key] = widget
            self.stack.add_named(widget, key)

        sidebar_list = Gtk.ListBox(css_classes=["navigation-sidebar"], vexpand=True)
        for key, title, icon in PAGES:
            row_box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6)
            row_box.append(Gtk.Image(icon_name=icon))
            row_box.append(Gtk.Label(label=title, xalign=0))
            row = Gtk.ListBoxRow(child=row_box)
            row.key = key
            sidebar_list.append(row)
        sidebar_list.connect("row-selected", self._select)
        self.indicator = StateIndicator()
        footer = Gtk.Box(margin_top=10, margin_bottom=12, margin_start=16, margin_end=12)
        footer.append(self.indicator)
        sidebar_view = Adw.ToolbarView()
        sidebar_view.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Omni")))
        sidebar_view.set_content(sidebar_list)
        sidebar_view.add_bottom_bar(footer)

        self.split = Adw.NavigationSplitView(
            sidebar=Adw.NavigationPage(title="Omni", child=sidebar_view),
            content=Adw.NavigationPage(title="Omni", child=self.stack), min_sidebar_width=200, max_sidebar_width=240)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        sidebar_list.select_row(sidebar_list.get_row_at_index(0))
        bridge.listen(self._event)

    def _select(self, _list, row) -> None:
        if row is not None:
            self.stack.set_visible_child_name(row.key)
            self.split.set_show_content(True)

    def _event(self, event: dict) -> None:
        if event.get("event") == "state":
            self.indicator.set_state(event["state"], event.get("detail", ""))
        elif event.get("event") == "connection" and not event["connected"]:
            self.indicator.set_state("offline")
        elif event.get("event") == "budget":
            self.toast(event["message"])

    def toast(self, text: str, undo=None) -> None:
        toast = Adw.Toast(title=text, timeout=5)
        if undo:
            toast.set_button_label("Undo")
            toast.connect("button-clicked", lambda *_: undo())
        self.toasts.add_toast(toast)
