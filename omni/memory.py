"""Hermes-shaped memory: two editable markdown files, an episode log, and skills.

Every entry Omni writes is a markdown bullet followed by a small HTML comment
carrying its id, date, and source episode, so Nick can open the files in any
editor and the app can still show provenance. Lines Nick adds by hand without a
comment are entries too; they get a stable id from their text.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import config

STORES = {"user": ("USER.md", "About Nick"), "memory": ("MEMORY.md", "Omni's notes")}
PROMPT_LIMIT = 4000
ENTRY = re.compile(r"^- (?P<text>.*?)(?:\s*<!-- id:(?P<id>[\w-]+)(?: at:(?P<at>\S+))?(?: ep:(?P<ep>\d+))? -->)?\s*$")
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


@dataclass
class Entry:
    id: str
    store: str
    text: str
    at: str = ""
    episode: int | None = None

    def line(self) -> str:
        meta = f"id:{self.id}" + (f" at:{self.at}" if self.at else "") + (f" ep:{self.episode}" if self.episode else "")
        return f"- {self.text} <!-- {meta} -->"


class Memory:
    def __init__(self, root: Path | None = None):
        self.root = config.private_dir(root or config.DATA)
        self.notes = config.private_dir(self.root / "memory")
        self.skills_dir = config.private_dir(self.root / "skills")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "episodes.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
                source TEXT NOT NULL DEFAULT 'text', agent TEXT NOT NULL DEFAULT 'codex',
                thread_id TEXT, parent_id INTEGER, kind TEXT NOT NULL DEFAULT 'turn',
                request TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', tools TEXT NOT NULL DEFAULT '[]',
                timings TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'running',
                tokens_in INTEGER NOT NULL DEFAULT 0, tokens_cached INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS episodes_started ON episodes(started_at);
            CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(request, reply, tools, content='episodes', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
                INSERT INTO episodes_fts(rowid, request, reply, tools) VALUES (new.id, new.request, new.reply, new.tools);
            END;
            CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
                INSERT INTO episodes_fts(episodes_fts, rowid, request, reply, tools) VALUES ('delete', old.id, old.request, old.reply, old.tools);
            END;
            CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE OF request, reply, tools ON episodes BEGIN
                INSERT INTO episodes_fts(episodes_fts, rowid, request, reply, tools) VALUES ('delete', old.id, old.request, old.reply, old.tools);
                INSERT INTO episodes_fts(rowid, request, reply, tools) VALUES (new.id, new.request, new.reply, new.tools);
            END;
        """)
        (self.root / "episodes.sqlite").chmod(0o600)

    def close(self) -> None:
        self.db.close()

    # ---- semantic notes ---------------------------------------------------

    def path(self, store: str) -> Path:
        if store not in STORES:
            raise ValueError("store must be 'user' or 'memory'")
        return self.notes / STORES[store][0]

    def entries(self, store: str) -> list[Entry]:
        path = self.path(store)
        if not path.is_file():
            return []
        result = []
        for line in path.read_text().splitlines():
            match = ENTRY.match(line)
            if not match or not match["text"].strip():
                continue
            text = match["text"].strip()
            ident = match["id"] or _stable_id(store, text)
            result.append(Entry(ident, store, text, match["at"] or "", int(match["ep"]) if match["ep"] else None))
        return result

    def _write(self, store: str, entries: list[Entry]) -> None:
        path = self.path(store)
        body = f"# {STORES[store][1]}\n\n" + "".join(entry.line() + "\n" for entry in entries)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(body)
        temporary.chmod(0o600)
        temporary.replace(path)

    def save(self, store: str, text: str, episode: int | None = None) -> Entry:
        text = " ".join(text.split())
        if not 3 <= len(text) <= 600:
            raise ValueError("A memory entry must be 3–600 characters; split longer notes")
        with self._lock:
            entries = self.entries(store)
            for entry in entries:
                if entry.text.lower() == text.lower():
                    return entry
            entry = Entry(_new_id(store), store, text, datetime.now().strftime("%Y-%m-%d"), episode)
            self._write(store, [*entries, entry])
            return entry

    def update(self, ident: str, text: str) -> Entry:
        text = " ".join(text.split())
        with self._lock:
            store, entries, index = self._find(ident)
            entries[index].text = text
            entries[index].at = datetime.now().strftime("%Y-%m-%d")
            self._write(store, entries)
            return entries[index]

    def forget(self, ident: str) -> Entry:
        with self._lock:
            store, entries, index = self._find(ident)
            removed = entries.pop(index)
            self._write(store, entries)
            return removed

    def restore(self, entry: Entry) -> None:
        with self._lock:
            entries = self.entries(entry.store)
            if all(existing.id != entry.id for existing in entries):
                self._write(entry.store, [*entries, entry])

    def _find(self, ident: str) -> tuple[str, list[Entry], int]:
        for store in STORES:
            entries = self.entries(store)
            for index, entry in enumerate(entries):
                if entry.id == ident:
                    return store, entries, index
        raise KeyError(f"No memory entry {ident}")

    def prompt_block(self) -> str:
        """The notes injected into every new thread, bounded so the prefix stays cheap."""
        parts = []
        for store, (_, title) in STORES.items():
            lines = [f"- {e.text} [{e.id}]" for e in self.entries(store)]
            body = "\n".join(lines) or "(empty)"
            if len(body) > PROMPT_LIMIT:
                body = body[-PROMPT_LIMIT:].split("\n", 1)[-1]
                body += f"\n(Over {PROMPT_LIMIT} characters: consolidate {STORES[store][0]} with memory_update/memory_forget when convenient.)"
            parts.append(f"## {title} ({STORES[store][0]})\n{body}")
        return "\n\n".join(parts)

    # ---- episodes ---------------------------------------------------------

    def start_episode(self, request: str, *, source: str = "text", agent: str = "codex", thread_id: str | None = None,
                      kind: str = "turn", parent_id: int | None = None) -> int:
        with self._lock:
            cursor = self.db.execute(
                "INSERT INTO episodes(started_at, source, agent, thread_id, kind, parent_id, request) VALUES (?,?,?,?,?,?,?)",
                (time.time(), source, agent, thread_id, kind, parent_id, request))
            self.db.commit()
            return int(cursor.lastrowid)

    def finish_episode(self, ident: int, *, reply: str, tools: list, timings: dict, status: str,
                       tokens: dict | None = None, thread_id: str | None = None) -> None:
        tokens = tokens or {}
        with self._lock:
            self.db.execute(
                "UPDATE episodes SET ended_at=?, reply=?, tools=?, timings=?, status=?, tokens_in=?, tokens_cached=?, tokens_out=?,"
                " thread_id=COALESCE(?, thread_id) WHERE id=?",
                (time.time(), reply, json.dumps(tools), json.dumps(timings), status, tokens.get("input", 0),
                 tokens.get("cached", 0), tokens.get("output", 0), thread_id, ident))
            self.db.commit()

    def episode(self, ident: int) -> dict | None:
        row = self.db.execute("SELECT * FROM episodes WHERE id=?", (ident,)).fetchone()
        return _episode(row) if row else None

    def recent(self, limit: int = 50, before: float | None = None) -> list[dict]:
        rows = self.db.execute("SELECT * FROM episodes WHERE started_at < ? ORDER BY started_at DESC LIMIT ?",
                               (before or time.time() + 1, limit)).fetchall()
        return [_episode(row) for row in rows]

    def delete_episode(self, ident: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM episodes WHERE id=?", (ident,))
            self.db.commit()

    def tokens_today(self) -> dict:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self.db.execute("SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_cached),0), COALESCE(SUM(tokens_out),0)"
                              " FROM episodes WHERE started_at >= ?", (start,)).fetchone()
        return {"input": row[0], "cached": row[1], "output": row[2], "billable": row[0] - row[1] + row[2]}

    def search(self, query: str, limit: int = 8) -> dict:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
        facts = []
        for store in STORES:
            for entry in self.entries(store):
                score = sum(term in entry.text.lower() for term in terms)
                if score:
                    facts.append((score, entry))
        facts.sort(key=lambda item: -item[0])
        episodes = []
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            rows = self.db.execute(
                "SELECT e.* FROM episodes_fts f JOIN episodes e ON e.id = f.rowid WHERE episodes_fts MATCH ?"
                " ORDER BY bm25(episodes_fts), e.started_at DESC LIMIT ?", (match, limit)).fetchall()
            episodes = [_episode(row) for row in rows]
        return {"facts": [asdict(entry) for _, entry in facts[:limit]], "episodes": episodes}

    # ---- skills -----------------------------------------------------------

    def save_skill(self, name: str, description: str, body: str) -> Path:
        name = name.strip().lower().replace(" ", "-")
        if not SKILL_NAME.match(name):
            raise ValueError("Skill names are lowercase words joined by hyphens")
        description = " ".join(description.split())
        if not 10 <= len(description) <= 300:
            raise ValueError("A skill description must be 10–300 characters and say when to use it")
        folder = config.private_dir(self.skills_dir / name)
        path = folder / "SKILL.md"
        path.write_text(f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n{body.strip()}\n")
        path.chmod(0o600)
        return path

    def skills(self) -> list[dict]:
        result = []
        for path in sorted(self.skills_dir.glob("*/SKILL.md")):
            text = path.read_text()
            match = re.search(r"^description:\s*(.+)$", text, re.M)
            description = match[1].strip() if match else ""
            if description.startswith('"'):
                try:
                    description = json.loads(description)
                except json.JSONDecodeError:
                    pass
            result.append({"name": path.parent.name, "description": description, "path": str(path),
                           "updated_at": path.stat().st_mtime})
        return result

    def read_skill(self, name: str) -> str:
        path = self.skills_dir / name / "SKILL.md"
        if not SKILL_NAME.match(name) or not path.is_file():
            raise KeyError(f"No skill named {name}")
        return path.read_text()

    def delete_skill(self, name: str) -> str:
        text = self.read_skill(name)
        path = self.skills_dir / name / "SKILL.md"
        path.unlink()
        path.parent.rmdir()
        return text

    # ---- migration ----------------------------------------------------------

    def migrate_legacy(self, legacy: Path | None = None) -> int:
        """Copy the old Omi memory/knowledge/conversations into the new stores once."""
        legacy = legacy or config.LEGACY_DB
        marker = self.root / ".migrated-v0"
        if marker.exists() or not legacy.is_file():
            return 0
        source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
        count = 0
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "memory" in tables:
            for (content,) in source.execute("SELECT content FROM memory ORDER BY id"):
                if content and 3 <= len(content.strip()) <= 600:
                    self.save("user", content)
                    count += 1
        if "knowledge" in tables:
            for title, body in source.execute("SELECT title, body FROM knowledge ORDER BY id"):
                text = f"{title}: {body}" if title else body
                if text and len(text) <= 600:
                    self.save("memory", text)
                    count += 1
        if "conversations" in tables:
            with self._lock:
                for agent, request, response, created in source.execute(
                        "SELECT agent, request, response, created_at FROM conversations ORDER BY id"):
                    try:
                        started = datetime.fromisoformat(created).timestamp()
                    except (TypeError, ValueError):
                        started = time.time()
                    self.db.execute("INSERT INTO episodes(started_at, ended_at, source, agent, kind, request, reply, status)"
                                    " VALUES (?,?,?,?,?,?,?,?)", (started, started, "legacy", agent, "turn", request, response, "completed"))
                    count += 1
                self.db.commit()
        source.close()
        marker.write_text(datetime.now().isoformat())
        return count


def _episode(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tools"] = json.loads(item["tools"] or "[]")
    item["timings"] = json.loads(item["timings"] or "{}")
    return item


def _stable_id(store: str, text: str) -> str:
    return store[0] + "-" + hashlib.sha1(text.encode()).hexdigest()[:8]


def _new_id(store: str) -> str:
    return store[0] + "-" + hashlib.sha1(f"{time.time_ns()}".encode()).hexdigest()[:8]


def day_start(days_ago: int = 0) -> float:
    return (datetime.now() - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
