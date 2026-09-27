"""Durable local reminders delivered as desktop notifications (unchanged from v0, renamed)."""
from __future__ import annotations

from datetime import datetime
import sqlite3
import subprocess
import time


def setup(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY, content TEXT NOT NULL, due_at REAL NOT NULL,
        source TEXT NOT NULL, source_key TEXT UNIQUE,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, claimed_at REAL, delivered_at REAL,
        notification_id TEXT, error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status,due_at,retry_at)')


def parse_due(value: str) -> float:
    try:
        due = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError) as exc:
        raise ValueError('Reminder time must be an ISO date and time with timezone') from exc
    if due.tzinfo is None:
        raise ValueError('Reminder time must include a timezone offset')
    return due.timestamp()


def add(db: sqlite3.Connection, content: str, due_at: float, *, source: str = 'user', source_key: str | None = None, proposed: bool = False, clock: float | None = None) -> int:
    current = time.time() if clock is None else clock
    if not isinstance(content, str) or not content.strip() or len(content) > 2000:
        raise ValueError('Reminder text must contain 1–2000 characters')
    if not current < due_at <= current + 366 * 86400:
        raise ValueError('Choose a future reminder time within one year')
    status = 'proposed' if proposed else 'scheduled'
    if source_key:
        existing = db.execute('SELECT id FROM reminders WHERE source_key=?', (source_key,)).fetchone()
        if existing:
            return existing[0]
    cursor = db.execute('INSERT INTO reminders(content,due_at,source,source_key,status,created_at) VALUES(?,?,?,?,?,?)', (content.strip(), due_at, source, source_key, status, current))
    db.commit()
    return int(cursor.lastrowid)


def review(db: sqlite3.Connection, ident: int, accept: bool) -> None:
    cursor = db.execute("UPDATE reminders SET status=? WHERE id=? AND status='proposed'", ('scheduled' if accept else 'cancelled', ident))
    db.commit()
    if not cursor.rowcount:
        raise ValueError('That suggestion is no longer awaiting review')


def cancel(db: sqlite3.Connection, ident: int) -> None:
    cursor = db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status IN ('proposed','scheduled','failed')", (ident,))
    db.commit()
    if not cursor.rowcount:
        raise ValueError('That reminder is already delivered, dispatching, or cancelled')


def list_items(db: sqlite3.Connection) -> list[tuple]:
    return db.execute("SELECT id,content,due_at,status,source,error FROM reminders ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'scheduled' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END,due_at LIMIT 150").fetchall()


def summary(db: sqlite3.Connection) -> str:
    rows = [row for row in list_items(db) if row[3] in {'proposed', 'scheduled', 'failed'}]
    return '\n'.join(f'{ident}: {datetime.fromtimestamp(due).astimezone().strftime("%Y-%m-%d %H:%M %Z")} · {status} · {content}' for ident, content, due, status, _, _ in rows) or 'No upcoming reminders.'


def in_quiet_hours(config: dict, current: float) -> bool:
    if not config.get('quiet_hours_enabled', False):
        return False
    def minute(value: str) -> int:
        hour, mins = map(int, value.split(':'))
        if not (0 <= hour <= 23 and 0 <= mins <= 59):
            raise ValueError('Invalid quiet hours')
        return hour * 60 + mins
    start = minute(config.get('quiet_hours_start', '22:00'))
    end = minute(config.get('quiet_hours_end', '08:00'))
    local = datetime.fromtimestamp(current).astimezone()
    value = local.hour * 60 + local.minute
    return start <= value < end if start < end else (value >= start or value < end) if start > end else False


def notify(content: str, ident: int) -> str:
    result = subprocess.run(['notify-send', '--print-id', '--app-name=Omni', '--', 'Omni reminder', content], capture_output=True, text=True, timeout=10, check=True)
    return result.stdout.strip()[:80]


def deliver_due(db: sqlite3.Connection, config: dict, *, clock: float | None = None, sender=notify) -> int:
    current = time.time() if clock is None else clock
    if in_quiet_hours(config, current):
        return 0
    # Recover a worker interrupted before finishing notification delivery.
    db.execute("UPDATE reminders SET status='scheduled',retry_at=? WHERE status='dispatching' AND claimed_at<?", (current, current - 60))
    db.commit()
    delivered = 0
    for _ in range(10):
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT id,content,attempts FROM reminders WHERE status='scheduled' AND due_at<=? AND retry_at<=? ORDER BY due_at LIMIT 1", (current, current)).fetchone()
        if row is None:
            db.commit()
            break
        ident, content, attempts = row
        db.execute("UPDATE reminders SET status='dispatching',claimed_at=?,attempts=attempts+1 WHERE id=?", (current, ident))
        db.commit()
        try:
            notification_id = sender(content, ident)
        except Exception as exc:
            status = 'failed' if attempts + 1 >= 5 else 'scheduled'
            db.execute('UPDATE reminders SET status=?,retry_at=?,error=? WHERE id=?', (status, current + min(3600, 30 * 2 ** attempts), type(exc).__name__, ident))
            db.commit()
            continue
        db.execute("UPDATE reminders SET status='delivered',delivered_at=?,notification_id=?,error='' WHERE id=?", (current, str(notification_id), ident))
        db.commit()
        delivered += 1
    return delivered
