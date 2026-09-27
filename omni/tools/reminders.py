"""Reminder tools over omni.reminders; the daemon delivers due reminders every 20 s."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

from .. import config, reminders
from . import tool

_lock = threading.Lock()


def database() -> sqlite3.Connection:
    config.private_dir(config.DATA)
    db = sqlite3.connect(config.DATA / "reminders.sqlite", check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    reminders.setup(db)
    return db


@tool("Schedule a desktop reminder. `at` is an ISO 8601 time with timezone offset, e.g. 2026-09-28T09:00:00-04:00.",
      text={"type": "string"}, at={"type": "string"})
def reminder_add(ctx, text: str, at: str) -> str:
    with _lock:
        db = database()
        ident = reminders.add(db, text, reminders.parse_due(at))
    when = datetime.fromtimestamp(reminders.parse_due(at)).astimezone().strftime("%a %b %-d %-I:%M %p")
    return f"Reminder {ident} set for {when}"


@tool("List upcoming reminders.")
def reminder_list(ctx) -> str:
    with _lock:
        return reminders.summary(database())


@tool("Cancel a reminder by id.", id={"type": "integer"})
def reminder_cancel(ctx, id: int) -> str:
    with _lock:
        reminders.cancel(database(), id)
    return f"Cancelled reminder {id}"
