#!/usr/bin/env python3
"""Native Omi control app. The broker in assistant.py remains the only executor."""

from __future__ import annotations

import argparse
import json
import re
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Gdk, Gio  # noqa: E402

import assistant as omi


def textview(editable: bool = True) -> Gtk.TextView:
    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_editable(editable)
    view.set_cursor_visible(editable)
    view.set_top_margin(15)
    view.set_bottom_margin(15)
    view.set_left_margin(15)
    view.set_right_margin(15)
    view.set_vexpand(True)
    return view


def scroll(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    box = Gtk.ScrolledWindow()
    box.set_hexpand(True)
    box.set_vexpand(True)
    box.set_child(child)
    return box


def label(text: str, css: str = "") -> Gtk.Label:
    item = Gtk.Label(label=text)
    item.set_xalign(0)
    item.set_wrap(True)
    if css:
        item.add_css_class(css)
    return item


def buffer_text(view: Gtk.TextView) -> str:
    buf = view.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


class OmiWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, page: str = "chat", review: Path | None = None):
        super().__init__(application=app, title="Omi approval" if review is not None else "Omi")
        self.set_default_size(1040, 720)
        self.busy = False
        self.review_mode = review is not None
        self.session_id: int | None = None
        self.rebuilding_sessions = False
        self.approval_event: threading.Event | None = None
        self.approval_result = False
        self.connect("close-request", self.on_close)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_child(outer)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        sidebar.set_size_request(210, -1)
        sidebar.set_margin_top(24)
        sidebar.set_margin_bottom(18)
        sidebar.set_margin_start(16)
        sidebar.set_margin_end(16)
        sidebar.add_css_class("sidebar")
        outer.append(sidebar)
        sidebar.append(label("OMI", "brand"))
        sidebar.append(label("Your desktop assistant", "muted"))
        spacer = Gtk.Box()
        spacer.set_size_request(-1, 14)
        sidebar.append(spacer)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        for title, key in (("Conversation", "chat"), ("Memory", "memory"), ("Knowledge", "knowledge"), ("Reminders", "reminders"), ("Activity", "activity"), ("Profile", "profile"), ("Settings", "settings")):
            button = Gtk.Button(label=title)
            button.add_css_class("flat")
            button.connect("clicked", lambda _button, chosen=key: self.show_page(chosen))
            sidebar.append(button)
        sidebar.append(Gtk.Box(vexpand=True))
        self.status_label = label("Ready", "muted")
        sidebar.append(self.status_label)
        outer.append(self.stack)

        self.build_chat()
        self.build_memory()
        self.build_knowledge()
        self.build_reminders()
        self.build_activity()
        self.build_profile()
        self.build_settings()
        self.show_page(page)
        GLib.timeout_add_seconds(3, self.poll_activity)
        if review is not None:
            GLib.idle_add(self.start_review, review)

    def on_close(self, *_args) -> bool:
        if self.approval_event:
            self.approval_result = False
            self.approval_event.set()
        return False

    def panel(self, title: str, subtitle: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.append(label(title, "page-title"))
        box.append(label(subtitle, "muted"))
        key = "chat" if title == "Conversation" else title.lower().split()[0]
        self.stack.add_named(scroll(box) if key == "settings" else box, key)
        return box

    def show_page(self, page: str) -> None:
        self.stack.set_visible_child_name(page)
        if page == "memory":
            self.refresh_memory()
        elif page == "knowledge":
            self.refresh_knowledge()
        elif page == "reminders":
            self.refresh_reminders()
        elif page == "activity":
            self.refresh_activity()
        elif page == "settings":
            self.refresh_settings()

    def build_chat(self) -> None:
        box = self.panel("Conversation", "Ask a question or give Omi an action. Commands go into Activity; questions stay in this thread.")
        row = Gtk.Box(spacing=12)
        row.set_vexpand(True)
        box.append(row)
        threads = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        threads.set_size_request(215, -1)
        row.append(threads)
        create = Gtk.Button(label="+ New conversation")
        create.connect("clicked", self.create_session)
        threads.append(create)
        self.session_list = Gtk.ListBox()
        self.session_list.add_css_class("boxed-list")
        threads.append(scroll(self.session_list))
        self.session_list.connect("row-selected", self.select_session)
        middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        middle.set_hexpand(True)
        row.append(middle)
        self.chat_view = textview(False)
        middle.append(scroll(self.chat_view))
        composer = Gtk.Box(spacing=8)
        middle.append(composer)
        self.request_entry = Gtk.Entry()
        self.request_entry.set_placeholder_text("Ask Omi or describe an action…")
        self.request_entry.set_hexpand(True)
        self.request_entry.connect("activate", self.send_request)
        composer.append(self.request_entry)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("suggested-action")
        self.send_button.connect("clicked", self.send_request)
        composer.append(self.send_button)
        self.refresh_sessions()
        if self.session_id is None:
            self.create_session(None)
        else:
            self.load_session()

    def refresh_sessions(self) -> None:
        self.rebuilding_sessions = True
        while child := self.session_list.get_first_child():
            self.session_list.remove(child)
        with omi.database() as db:
            rows = db.execute("SELECT id,title FROM sessions ORDER BY updated_at DESC LIMIT 100").fetchall()
        if self.session_id is None and rows:
            self.session_id = rows[0][0]
        for ident, title in rows:
            row = Gtk.ListBoxRow()
            row.session_id = ident
            row.set_child(label(title[:54]))
            self.session_list.append(row)
            if ident == self.session_id:
                self.session_list.select_row(row)
        self.rebuilding_sessions = False

    def create_session(self, _button: Gtk.Button | None) -> None:
        with omi.database() as db:
            self.session_id = omi.new_session(db)
        self.refresh_sessions()
        self.load_session()

    def select_session(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None and not self.rebuilding_sessions:
            self.session_id = row.session_id
            self.load_session()

    def load_session(self) -> None:
        with omi.database() as db:
            rows = db.execute("SELECT request,response FROM conversations WHERE session_id=? ORDER BY id", (self.session_id,)).fetchall()
        content = "\n\n".join(f"YOU\n{request}\n\nOMI\n{response}" for request, response in rows)
        self.chat_view.get_buffer().set_text(content or "Start a conversation. You can ask Omi about your work, or give it a desktop task.")

    def append_chat(self, role: str, content: str) -> None:
        buf = self.chat_view.get_buffer()
        end = buf.get_end_iter()
        buf.insert(end, f"\n\n{role.upper()}\n{content}")
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.chat_view.scroll_mark_onscreen(mark)

    def send_request(self, _widget: Gtk.Widget) -> None:
        request = self.request_entry.get_text().strip()
        if self.busy or not request:
            return
        self.request_entry.set_text("")
        self.append_chat("You", request)
        self.start_worker(request, omi.settings().get("agent", "codex"), None)

    def start_review(self, path: Path) -> bool:
        pending = (omi.RUNTIME / "pending").resolve()
        path = path.resolve()
        if path.parent != pending or not path.is_file():
            self.append_chat("Error", "The pending review is unavailable.")
            return False
        payload = json.loads(path.read_text())
        path.unlink()
        self.append_chat("Voice request", payload["request"])
        self.start_worker(payload["request"], payload["agent"], payload["plan"])
        return False

    def start_worker(self, request: str, agent: str, plan: dict | None) -> None:
        self.busy = True
        self.send_button.set_sensitive(False)
        self.status_label.set_text("Omi is working…")
        session_id = self.session_id if plan is None else None

        def work() -> None:
            try:
                with omi.database() as db:
                    omi.run_request(request, agent, db, plan=plan, session_id=session_id,
                                    emit=lambda kind, message: GLib.idle_add(self.on_event, kind, message),
                                    approve=self.ask_approval, source="voice" if plan is not None else "app")
            except Exception as exc:
                GLib.idle_add(self.on_event, "error", str(exc))
            finally:
                GLib.idle_add(self.worker_done)

        threading.Thread(target=work, daemon=True).start()

    def on_event(self, kind: str, message: str) -> bool:
        if kind in {"answer", "result", "error", "memory"}:
            self.append_chat("Omi" if kind == "answer" else kind, message)
        return False

    def worker_done(self) -> bool:
        self.busy = False
        self.send_button.set_sensitive(True)
        self.status_label.set_text("Ready")
        self.refresh_sessions()
        if self.review_mode:
            GLib.timeout_add_seconds(3, self.close)
        return False

    def ask_approval(self, description: str) -> bool:
        event = threading.Event()
        self.approval_event = event
        self.approval_result = False

        def show() -> bool:
            dialog = Gtk.Dialog(transient_for=self, modal=True, title="Approve Omi action")
            dialog.set_default_size(580, 260)
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Approve once", Gtk.ResponseType.ACCEPT)
            content = dialog.get_content_area()
            content.set_margin_top(20)
            content.set_margin_bottom(20)
            content.set_margin_start(20)
            content.set_margin_end(20)
            content.append(label("Omi wants to do this:", "page-title"))
            details = textview(False)
            details.get_buffer().set_text(description)
            details.set_size_request(-1, 150)
            content.append(scroll(details))

            def respond(_dialog: Gtk.Dialog, response: int) -> None:
                self.approval_result = response == Gtk.ResponseType.ACCEPT
                self.approval_event = None
                dialog.destroy()
                event.set()

            dialog.connect("response", respond)
            dialog.present()
            self.status_label.set_text("Waiting for your approval")
            return False

        GLib.idle_add(show)
        event.wait()
        return self.approval_result

    def build_memory(self) -> None:
        box = self.panel("Memory", "Facts Omi can recall. Automatic memories keep their source and can be edited or removed.")
        controls = Gtk.Box(spacing=8)
        box.append(controls)
        self.memory_search = Gtk.SearchEntry()
        self.memory_search.set_placeholder_text("Search memories")
        self.memory_search.set_hexpand(True)
        self.memory_search.connect("search-changed", lambda *_: self.refresh_memory())
        controls.append(self.memory_search)
        add = Gtk.Button(label="+ Add memory")
        add.connect("clicked", lambda *_: self.edit_memory(None, ""))
        controls.append(add)
        self.memory_list = Gtk.ListBox()
        self.memory_list.add_css_class("boxed-list")
        box.append(scroll(self.memory_list))

    def refresh_memory(self) -> None:
        while child := self.memory_list.get_first_child():
            self.memory_list.remove(child)
        query = self.memory_search.get_text().strip()
        with omi.database() as db:
            rows = db.execute("SELECT id,content,source,created_at FROM memory WHERE content LIKE ? ORDER BY id DESC LIMIT 150", (f"%{query}%",)).fetchall()
        for ident, content, source, created in rows:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_top(9)
            row.set_margin_bottom(9)
            row.set_margin_start(12)
            row.set_margin_end(12)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            info.set_hexpand(True)
            info.append(label(content))
            info.append(label(f"{source} · {created[:10]}", "muted"))
            row.append(info)
            edit = Gtk.Button(label="Edit")
            edit.connect("clicked", lambda _b, i=ident, c=content: self.edit_memory(i, c))
            row.append(edit)
            remove = Gtk.Button(label="Delete")
            remove.connect("clicked", lambda _b, i=ident: self.delete_memory(i))
            row.append(remove)
            self.memory_list.append(row)

    def edit_memory(self, ident: int | None, content: str) -> None:
        dialog = Gtk.Dialog(transient_for=self, modal=True, title="Edit memory" if ident else "Add memory")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.ACCEPT)
        area = dialog.get_content_area()
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        entry = Gtk.Entry()
        entry.set_text(content)
        entry.set_width_chars(70)
        area.append(entry)

        def respond(_dialog: Gtk.Dialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT and entry.get_text().strip():
                with omi.database() as db:
                    if ident:
                        db.execute("UPDATE memory SET content=?,updated_at=? WHERE id=?", (entry.get_text().strip(), omi.now(), ident))
                    else:
                        stamp = omi.now()
                        db.execute("INSERT INTO memory(content,source,created_at,updated_at) VALUES(?,?,?,?)", (entry.get_text().strip(), "user app", stamp, stamp))
                    db.commit()
                self.refresh_memory()
            dialog.destroy()

        dialog.connect("response", respond)
        dialog.present()

    def delete_memory(self, ident: int) -> None:
        with omi.database() as db:
            db.execute("DELETE FROM memory WHERE id=?", (ident,))
            db.commit()
        self.refresh_memory()

    def build_knowledge(self) -> None:
        box = self.panel("Knowledge", "Add longer notes about projects, businesses, people, or past work. Omi retrieves matching excerpts when you ask.")
        controls = Gtk.Box(spacing=8)
        box.append(controls)
        self.knowledge_search = Gtk.SearchEntry()
        self.knowledge_search.set_placeholder_text("Search your notes")
        self.knowledge_search.set_hexpand(True)
        self.knowledge_search.connect("search-changed", lambda *_: self.refresh_knowledge())
        controls.append(self.knowledge_search)
        add = Gtk.Button(label="+ Add note")
        add.connect("clicked", lambda *_: self.edit_knowledge(None, "", ""))
        controls.append(add)
        import_button = Gtk.Button(label="Import Markdown")
        import_button.connect("clicked", self.import_knowledge)
        controls.append(import_button)
        export_button = Gtk.Button(label="Export notes")
        export_button.connect("clicked", self.export_knowledge)
        controls.append(export_button)
        self.knowledge_list = Gtk.ListBox()
        self.knowledge_list.add_css_class("boxed-list")
        box.append(scroll(self.knowledge_list))

    def refresh_knowledge(self) -> None:
        while child := self.knowledge_list.get_first_child():
            self.knowledge_list.remove(child)
        query = self.knowledge_search.get_text().strip()
        with omi.database() as db:
            rows = db.execute("SELECT id,title,body,updated_at FROM knowledge WHERE title LIKE ? OR body LIKE ? ORDER BY updated_at DESC LIMIT 100", (f"%{query}%", f"%{query}%")).fetchall()
        for ident, title, body, updated in rows:
            row = Gtk.Box(spacing=8)
            row.set_margin_top(9)
            row.set_margin_bottom(9)
            row.set_margin_start(12)
            row.set_margin_end(12)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            info.set_hexpand(True)
            info.append(label(title, "section-title"))
            info.append(label(body[:180] + ("…" if len(body) > 180 else "")))
            info.append(label(f"Updated {updated[:10]}", "muted"))
            row.append(info)
            edit = Gtk.Button(label="Edit")
            edit.connect("clicked", lambda _b, i=ident, t=title, b=body: self.edit_knowledge(i, t, b))
            row.append(edit)
            delete = Gtk.Button(label="Delete")
            delete.connect("clicked", lambda _b, i=ident: self.delete_knowledge(i))
            row.append(delete)
            self.knowledge_list.append(row)

    def edit_knowledge(self, ident: int | None, title: str, body: str) -> None:
        dialog = Gtk.Dialog(transient_for=self, modal=True, title="Edit knowledge note" if ident else "Add knowledge note")
        dialog.set_default_size(700, 500)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.ACCEPT)
        area = dialog.get_content_area()
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        area.set_spacing(8)
        area.append(label("Title"))
        title_entry = Gtk.Entry()
        title_entry.set_text(title)
        area.append(title_entry)
        area.append(label("Context or history"))
        body_view = textview()
        body_view.get_buffer().set_text(body)
        area.append(scroll(body_view))

        def respond(_dialog: Gtk.Dialog, response: int) -> None:
            name = title_entry.get_text().strip()
            content = buffer_text(body_view).strip()
            if response == Gtk.ResponseType.ACCEPT and name and content:
                with omi.database() as db:
                    stamp = omi.now()
                    if ident:
                        db.execute("UPDATE knowledge SET title=?,body=?,updated_at=? WHERE id=?", (name, content, stamp, ident))
                    else:
                        db.execute("INSERT INTO knowledge(title,body,source,created_at,updated_at) VALUES(?,?,?,?,?)", (name, content, "user app", stamp, stamp))
                    db.commit()
                self.refresh_knowledge()
            dialog.destroy()

        dialog.connect("response", respond)
        dialog.present()

    def delete_knowledge(self, ident: int) -> None:
        with omi.database() as db:
            db.execute("DELETE FROM knowledge WHERE id=?", (ident,))
            db.commit()
        self.refresh_knowledge()

    def import_knowledge(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserNative(title="Import a Markdown or text note", transient_for=self, action=Gtk.FileChooserAction.OPEN, accept_label="Import", cancel_label="Cancel")

        def respond(_chooser: Gtk.FileChooserNative, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                selected = chooser.get_file()
                path = Path(selected.get_path()) if selected else None
                if path is not None:
                    try:
                        if path.suffix.lower() not in {".md", ".txt"} or path.stat().st_size > 2_000_000:
                            raise ValueError("Choose a Markdown or text file smaller than 2 MB")
                        body = path.read_text(errors="replace").strip()
                        if not body:
                            raise ValueError("This file is empty")
                        with omi.database() as db:
                            stamp = omi.now()
                            db.execute("INSERT INTO knowledge(title,body,source,created_at,updated_at) VALUES(?,?,?,?,?)", (path.stem[:120], body, f"imported {path}", stamp, stamp))
                            db.commit()
                        self.refresh_knowledge()
                        self.status_label.set_text(f"Imported {path.name}")
                    except (OSError, ValueError) as exc:
                        self.status_label.set_text(str(exc))
            chooser.destroy()

        chooser.connect("response", respond)
        chooser.show()

    def export_knowledge(self, _button: Gtk.Button) -> None:
        directory = omi.DATA / "exports"
        omi.private_dir(directory)
        target = directory / f"omi-knowledge-{omi.now()[:10]}.json"
        suffix = 2
        while target.exists():
            target = directory / f"omi-knowledge-{omi.now()[:10]}-{suffix}.json"
            suffix += 1
        with omi.database() as db:
            rows = db.execute("SELECT id,title,body,source,created_at,updated_at FROM knowledge ORDER BY id").fetchall()
        content = [dict(zip(("id", "title", "body", "source", "created_at", "updated_at"), row)) for row in rows]
        target.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")
        target.chmod(0o600)
        self.status_label.set_text(f"Exported {len(rows)} notes to {target}")

    def build_activity(self) -> None:
        box = self.panel("Activity", "Recent desktop actions and subscription agent calls. Commands do not fill conversation context.")
        controls = Gtk.Box(spacing=8)
        box.append(controls)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh_activity())
        controls.append(refresh)
        all_button = Gtk.Button(label="Export full history")
        all_button.connect("clicked", lambda *_: self.export_activity(False))
        controls.append(all_button)
        reviewed_button = Gtk.Button(label="Export reviewed training data")
        reviewed_button.connect("clicked", lambda *_: self.export_activity(True))
        controls.append(reviewed_button)
        box.append(label("Desktop tasks · mark correct ones before training export", "section-title"))
        self.task_list = Gtk.ListBox()
        self.task_list.add_css_class("boxed-list")
        task_scroll = scroll(self.task_list)
        task_scroll.set_size_request(-1, 280)
        box.append(task_scroll)
        box.append(label("Detailed action log and model use", "section-title"))
        self.activity_view = textview(False)
        box.append(scroll(self.activity_view))

    def refresh_activity(self) -> None:
        with omi.database() as db:
            actions = db.execute("SELECT request,status,result,created_at FROM actions ORDER BY id DESC LIMIT 60").fetchall()
            calls = db.execute("SELECT agent,model,purpose,duration_ms,created_at FROM agent_calls ORDER BY id DESC LIMIT 30").fetchall()
            tasks = db.execute("SELECT id,request,status,feedback,created_at FROM task_runs WHERE mode='action' ORDER BY id DESC LIMIT 40").fetchall()
        self.activity_pending = any(status == "awaiting_commands" for _, _, status, _, _ in tasks)
        while child := self.task_list.get_first_child():
            self.task_list.remove(child)
        for ident, request, status, feedback, created in tasks:
            row = Gtk.Box(spacing=8)
            row.set_margin_top(8)
            row.set_margin_bottom(8)
            row.set_margin_start(10)
            row.set_margin_end(10)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            info.set_hexpand(True)
            info.append(label(request[:160]))
            info.append(label(f"#{ident} · {created[:16]} · {status} · {feedback}", "muted"))
            row.append(info)
            if status == "complete":
                correct = Gtk.Button(label="Correct")
                correct.connect("clicked", lambda _button, i=ident: self.mark_task(i, "correct"))
                row.append(correct)
            wrong = Gtk.Button(label="Needs correction")
            wrong.connect("clicked", lambda _button, i=ident: self.correct_task(i))
            row.append(wrong)
            self.task_list.append(row)
        lines = ["ACTIONS"]
        lines.extend(f"{created[:19]} · {status} · {request}\n{result[:350]}" for request, status, result, created in actions)
        lines.append("\nAGENT CALLS")
        lines.extend(f"{created[:19]} · {agent} / {model} · {purpose} · {duration} ms" for agent, model, purpose, duration, created in calls)
        self.activity_view.get_buffer().set_text("\n\n".join(lines))

    def poll_activity(self) -> bool:
        if self.stack.get_visible_child_name() == "activity" and getattr(self, "activity_pending", False):
            self.refresh_activity()
        return True

    def mark_task(self, ident: int, verdict: str, note: str = "") -> None:
        try:
            with omi.database() as db:
                omi.set_task_feedback(db, ident, verdict, note)
            self.refresh_activity()
        except ValueError as exc:
            self.status_label.set_text(str(exc))

    def export_activity(self, training: bool) -> None:
        directory = omi.DATA / "exports"
        omi.private_dir(directory)
        stem = "omi-training" if training else "omi-history"
        target = directory / f"{stem}-{omi.now()[:10]}.jsonl"
        suffix = 2
        while target.exists():
            target = directory / f"{stem}-{omi.now()[:10]}-{suffix}.jsonl"
            suffix += 1
        with omi.database() as db:
            count = omi.export_training(db, target) if training else omi.export_tasks(db, target)
        self.status_label.set_text(f"Exported {count} tasks to {target}")

    def correct_task(self, ident: int) -> None:
        dialog = Gtk.Dialog(transient_for=self, modal=True, title="What should Omi have done?")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save correction", Gtk.ResponseType.ACCEPT)
        area = dialog.get_content_area()
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Describe the mistake and expected behavior")
        entry.set_width_chars(75)
        area.append(entry)

        def respond(_dialog: Gtk.Dialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                self.mark_task(ident, "incorrect", entry.get_text().strip())
            dialog.destroy()

        dialog.connect("response", respond)
        dialog.present()

    def build_reminders(self) -> None:
        box = self.panel("Reminders", "Local reminders and suggestions. Notifications run while the Omi background service is active.")
        row = Gtk.Box(spacing=8)
        self.reminder_text = Gtk.Entry(hexpand=True)
        self.reminder_text.set_placeholder_text("What should Omi remind you about?")
        row.append(self.reminder_text)
        self.reminder_minutes = Gtk.SpinButton.new_with_range(1, 525600, 1)
        self.reminder_minutes.set_value(15)
        row.append(self.reminder_minutes)
        row.append(label("minutes"))
        add = Gtk.Button(label="Add reminder")
        add.connect("clicked", self.add_reminder)
        row.append(add)
        box.append(row)
        self.reminder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.append(scroll(self.reminder_list))
        GLib.timeout_add_seconds(5, self.poll_reminders)

    def poll_reminders(self) -> bool:
        if self.stack.get_visible_child_name() == "reminders":
            self.refresh_reminders()
        return True

    def add_reminder(self, _button: Gtk.Button) -> None:
        try:
            with omi.database() as db:
                omi.reminders.add(db, self.reminder_text.get_text(), omi.time.time() + self.reminder_minutes.get_value_as_int() * 60)
            self.reminder_text.set_text("")
            self.refresh_reminders()
            self.status_label.set_text("Reminder scheduled locally")
        except ValueError as exc:
            self.status_label.set_text(str(exc))

    def change_reminder(self, ident: int, operation: str) -> None:
        try:
            with omi.database() as db:
                if operation == "cancel":
                    omi.reminders.cancel(db, ident)
                else:
                    omi.reminders.review(db, ident, operation == "accept")
            self.refresh_reminders()
        except ValueError as exc:
            self.status_label.set_text(str(exc))

    def refresh_reminders(self) -> None:
        while child := self.reminder_list.get_first_child():
            self.reminder_list.remove(child)
        with omi.database() as db:
            items = omi.reminders.list_items(db)
        if not items:
            self.reminder_list.append(label("No reminders yet. Try ‘Remind me in 15 minutes to take a break.’"))
        for ident, content, due, status, source, error in items:
            row = Gtk.Box(spacing=12)
            row.set_margin_top(10)
            row.set_margin_bottom(10)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            info.append(label(content))
            date = omi.datetime.fromtimestamp(due).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            info.append(label(f"{date} · {status} · {source}" + (f" · {error}" if error else ""), "muted"))
            row.append(info)
            controls = [("Accept", "accept"), ("Dismiss", "dismiss")] if status == "proposed" else [("Cancel", "cancel")] if status in {"scheduled", "failed"} else []
            for title, operation in controls:
                button = Gtk.Button(label=title)
                button.connect("clicked", lambda _b, i=ident, op=operation: self.change_reminder(i, op))
                row.append(button)
            self.reminder_list.append(row)

    def build_profile(self) -> None:
        box = self.panel("Profile", "Tell Omi about your work, businesses, preferences, and response style. These notes are stored locally.")
        box.append(label("About you", "section-title"))
        self.profile_view = textview()
        self.profile_view.set_size_request(-1, 260)
        box.append(scroll(self.profile_view))
        box.append(label("Personality and tone", "section-title"))
        self.personality_view = textview()
        self.personality_view.set_size_request(-1, 130)
        box.append(scroll(self.personality_view))
        save = Gtk.Button(label="Save profile")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.save_profile)
        box.append(save)
        for view, filename in ((self.profile_view, "profile.md"), (self.personality_view, "personality.md")):
            path = omi.CONFIG / filename
            view.get_buffer().set_text(path.read_text() if path.exists() else "")

    def save_profile(self, _button: Gtk.Button) -> None:
        omi.private_dir(omi.CONFIG)
        for view, filename in ((self.profile_view, "profile.md"), (self.personality_view, "personality.md")):
            path = omi.CONFIG / filename
            path.write_text(buffer_text(view).strip() + "\n")
            path.chmod(0o600)
        self.status_label.set_text("Profile saved locally")

    def build_settings(self) -> None:
        box = self.panel("Settings", "Choose your subscription agent, model, memory behavior, and spoken replies.")
        setup = Gtk.Button(label="Check setup")
        setup.connect("clicked", self.check_setup)
        box.append(setup)
        self.setup_state = label("Check desktop, voice, and agent readiness here.", "muted")
        box.append(self.setup_state)
        box.append(label("Subscription agent", "section-title"))
        self.agent_drop = Gtk.DropDown.new_from_strings(["Codex", "Claude"])
        box.append(self.agent_drop)
        self.agent_state = label("", "muted")
        box.append(self.agent_state)
        box.append(label("Codex model", "section-title"))
        self.codex_model = Gtk.Entry()
        self.codex_model.set_placeholder_text("auto uses gpt-6-sol; enter another model to override")
        box.append(self.codex_model)
        box.append(label("Claude model", "section-title"))
        self.claude_model = Gtk.Entry()
        self.claude_model.set_placeholder_text("auto uses Claude CLI default")
        box.append(self.claude_model)
        self.auto_memory = Gtk.CheckButton(label="Save clear, non-sensitive facts I state automatically")
        box.append(self.auto_memory)
        self.speech = Gtk.CheckButton(label="Speak conversational answers aloud")
        box.append(self.speech)
        self.quiet_hours = Gtk.CheckButton(label="Hold reminder notifications during quiet hours")
        box.append(self.quiet_hours)
        self.quiet_start = Gtk.Entry()
        self.quiet_start.set_placeholder_text("Quiet hours start, local time (22:00)")
        box.append(self.quiet_start)
        self.quiet_end = Gtk.Entry()
        self.quiet_end.set_placeholder_text("Quiet hours end, local time (08:00)")
        box.append(self.quiet_end)
        box.append(label("Speech voice", "section-title"))
        self.speech_provider = Gtk.DropDown.new_from_strings(["Local Piper", "ElevenLabs custom voice"])
        box.append(self.speech_provider)
        self.elevenlabs_id = Gtk.Entry()
        self.elevenlabs_id.set_placeholder_text("ElevenLabs voice ID (after creating your voice)")
        box.append(self.elevenlabs_id)
        self.elevenlabs_key_entry = Gtk.Entry()
        self.elevenlabs_key_entry.set_placeholder_text("API key · leave blank to keep existing key")
        self.elevenlabs_key_entry.set_visibility(False)
        box.append(self.elevenlabs_key_entry)
        box.append(label("The API key is saved in the local Secret Service keyring. ElevenLabs receives the text Omi speaks when this provider is selected.", "muted"))
        test = Gtk.Button(label="Test voice")
        test.connect("clicked", self.test_voice)
        box.append(test)
        save = Gtk.Button(label="Save settings")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.save_settings)
        box.append(save)

    def check_setup(self, _button: Gtk.Button) -> None:
        self.setup_state.set_text("Checking local setup…")

        def work() -> None:
            try:
                report = "\n".join(f"{name}: {state}" for name, state in omi.doctor())
            except Exception as exc:
                report = f"Setup check failed: {type(exc).__name__}"
            GLib.idle_add(self.setup_state.set_text, report)

        threading.Thread(target=work, daemon=True).start()

    def refresh_settings(self) -> None:
        current = omi.settings()
        self.agent_drop.set_selected(0 if current.get("agent", "codex") == "codex" else 1)
        self.codex_model.set_text(current.get("codex_model", "auto"))
        self.claude_model.set_text(current.get("claude_model", "auto"))
        self.auto_memory.set_active(current.get("automatic_memory", True))
        self.speech.set_active(current.get("speech_enabled", True))
        self.quiet_hours.set_active(current.get("quiet_hours_enabled", False))
        self.quiet_start.set_text(current.get("quiet_hours_start", "22:00"))
        self.quiet_end.set_text(current.get("quiet_hours_end", "08:00"))
        self.speech_provider.set_selected(0 if current.get("speech_provider", "piper") == "piper" else 1)
        self.elevenlabs_id.set_text(current.get("elevenlabs_voice_id", ""))
        self.agent_state.set_text(f"Codex: {omi.agent_status('codex')} · Claude: {omi.agent_status('claude')}")

    def save_settings(self, _button: Gtk.Button) -> bool:
        current = omi.settings()
        chosen_agent = "codex" if self.agent_drop.get_selected() == 0 else "claude"
        if omi.agent_status(chosen_agent) != "ready":
            self.status_label.set_text(f"{chosen_agent.capitalize()} needs CLI login before Omi can use it")
            return False
        current["agent"] = chosen_agent
        current["codex_model"] = self.codex_model.get_text().strip() or "auto"
        current["claude_model"] = self.claude_model.get_text().strip() or "auto"
        current["automatic_memory"] = self.auto_memory.get_active()
        current["speech_enabled"] = self.speech.get_active()
        current["quiet_hours_enabled"] = self.quiet_hours.get_active()
        current["quiet_hours_start"] = self.quiet_start.get_text().strip()
        current["quiet_hours_end"] = self.quiet_end.get_text().strip()
        try:
            omi.reminders.in_quiet_hours({**current, "quiet_hours_enabled": True}, omi.time.time())
        except (ValueError, TypeError):
            self.status_label.set_text("Enter quiet hours as HH:MM, for example 22:00 and 08:00")
            return False
        current["speech_provider"] = "piper" if self.speech_provider.get_selected() == 0 else "elevenlabs"
        voice_id = self.elevenlabs_id.get_text().strip()
        if voice_id and not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", voice_id):
            self.status_label.set_text("Enter a valid ElevenLabs voice ID")
            return False
        current["elevenlabs_voice_id"] = voice_id
        if self.elevenlabs_key_entry.get_text().strip():
            try:
                omi.store_elevenlabs_key(self.elevenlabs_key_entry.get_text())
                self.elevenlabs_key_entry.set_text("")
            except Exception as exc:
                self.status_label.set_text(str(exc))
                return False
        omi.save_settings(current)
        omi.set_state("idle", current["agent"])
        self.status_label.set_text("Settings saved")
        self.refresh_settings()
        return True

    def test_voice(self, _button: Gtk.Button) -> None:
        if not self.save_settings(_button):
            return
        self.status_label.set_text("Testing voice…")

        def work() -> None:
            try:
                omi.speak_text("Hello. I'm Omi, your personal assistant.", omi.settings()["agent"])
                GLib.idle_add(self.status_label.set_text, "Voice test complete")
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, f"Voice test failed: {exc}")
            finally:
                omi.set_state("idle", omi.settings()["agent"])

        threading.Thread(target=work, daemon=True).start()


class OmiApp(Gtk.Application):
    def __init__(self, page: str, review: Path | None):
        super().__init__(application_id="local.omi.Assistant", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.page = page
        self.review = review
        self.window: OmiWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            css = Gtk.CssProvider()
            css.load_from_data(b"""
                window { background: #10151d; color: #e9eef4; }
                .sidebar { border-right: 1px solid #293241; }
                .brand { font-size: 29px; font-weight: 800; color: #86d8ca; }
                .page-title { font-size: 24px; font-weight: 700; }
                .section-title { font-size: 16px; font-weight: 700; }
                .muted { color: #a6b2bf; }
                textview, entry, listbox { background: #19212c; color: #e9eef4; border-radius: 8px; }
            """)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window = OmiWindow(self, self.page, self.review)
        self.window.present()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", choices=["chat", "memory", "knowledge", "reminders", "activity", "profile", "settings"], default="chat")
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    app = OmiApp(args.page, args.review)
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
