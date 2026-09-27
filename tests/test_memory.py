import sqlite3
import tempfile
import unittest
from pathlib import Path

from omni.memory import Memory


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.memory = Memory(self.root)
        self.addCleanup(self.memory.close)

    def test_save_update_forget_restore(self):
        entry = self.memory.save("user", "Nick prefers dark themes.")
        self.assertEqual(self.memory.save("user", "nick prefers DARK themes."), entry)  # no duplicates
        updated = self.memory.update(entry.id, "Nick prefers the Tokyo Night theme.")
        self.assertEqual(self.memory.entries("user")[0].text, updated.text)
        removed = self.memory.forget(entry.id)
        self.assertEqual(self.memory.entries("user"), [])
        self.memory.restore(removed)
        self.assertEqual(self.memory.entries("user")[0].id, entry.id)

    def test_hand_edits_are_entries(self):
        self.memory.save("memory", "Use foot for terminals.")
        path = self.memory.path("memory")
        path.write_text(path.read_text() + "- A line Nick typed himself\n")
        texts = [e.text for e in self.memory.entries("memory")]
        self.assertEqual(texts, ["Use foot for terminals.", "A line Nick typed himself"])
        ident = self.memory.entries("memory")[1].id
        self.assertEqual(self.memory.entries("memory")[1].id, ident)  # stable across reads
        self.memory.forget(ident)
        self.assertEqual(len(self.memory.entries("memory")), 1)

    def test_prompt_block_is_bounded(self):
        for index in range(120):
            self.memory.save("user", f"Fact number {index} about something Nick said that is fairly long.")
        block = self.memory.prompt_block()
        self.assertLess(len(block), 9500)
        self.assertIn("consolidate", block)
        self.assertIn("Fact number 119", block)

    def test_episodes_search_and_tokens(self):
        first = self.memory.start_episode("fix the printer on the office network")
        self.memory.finish_episode(first, reply="Restarted cups and re-added the HP printer.", tools=[{"label": "systemctl"}],
                                   timings={"completed": 900}, status="completed", tokens={"input": 1000, "cached": 800, "output": 50})
        self.memory.save("user", "The office printer is an HP LaserJet.")
        found = self.memory.search("what did we do about the printer")
        self.assertEqual(found["episodes"][0]["id"], first)
        self.assertIn("HP LaserJet", found["facts"][0]["text"])
        self.assertEqual(self.memory.tokens_today()["billable"], 250)
        self.memory.delete_episode(first)
        self.assertEqual(self.memory.search("printer")["episodes"], [])

    def test_skills(self):
        self.memory.save_skill("Tile Dev Workspace", "Open editor and two terminals on workspace 2 when Nick starts coding.", "1. ...")
        self.assertEqual(self.memory.skills()[0]["name"], "tile-dev-workspace")
        self.assertIn("Open editor", self.memory.skills()[0]["description"])
        with self.assertRaises(ValueError):
            self.memory.save_skill("../escape", "A description that is long enough.", "x")
        self.memory.delete_skill("tile-dev-workspace")
        self.assertEqual(self.memory.skills(), [])

    def test_legacy_migration_runs_once(self):
        legacy = self.root / "legacy.sqlite3"
        db = sqlite3.connect(legacy)
        db.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT, source TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY, title TEXT, body TEXT, source TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, agent TEXT, request TEXT, mode TEXT, response TEXT, created_at TEXT)")
        db.execute("INSERT INTO memory(content, source, created_at) VALUES ('My dog is named Rex', 'user', '2026-01-01')")
        db.execute("INSERT INTO knowledge(title, body, source, created_at, updated_at) VALUES ('Wifi', 'Office SSID is FIS', 'user', '', '')")
        db.execute("INSERT INTO conversations(agent, request, mode, response, created_at) VALUES ('codex', 'hi', 'answer', 'hello', '2026-01-01T10:00:00')")
        db.commit()
        db.close()
        self.assertEqual(self.memory.migrate_legacy(legacy), 3)
        self.assertEqual(self.memory.migrate_legacy(legacy), 0)
        self.assertEqual(self.memory.entries("user")[0].text, "My dog is named Rex")
        self.assertEqual(self.memory.entries("memory")[0].text, "Wifi: Office SSID is FIS")


if __name__ == "__main__":
    unittest.main()
