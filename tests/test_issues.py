import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omni import config, issues, tools
from omni.memory import Memory


class IssueReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.conf = root / "data", root / "config"
        self.conf.mkdir()
        (self.conf / "config.toml").write_text(
            'agent = "codex"\nelevenlabs_voice_id = "voice123"\nsome_api_key = "sk-hidden"\n'
            '[mail]\naccount = "me"\npassword = "hunter2"\n')
        self.patches = [patch.object(config, "DATA", self.data), patch.object(config, "CONFIG", self.conf),
                        patch.dict(os.environ, {"OMNI_RUNTIME": str(root / "run")}),
                        patch("omni.issues._journal", return_value="omnid started\nsomething failed"),
                        patch("omni.issues._doctor", return_value="- ok: Codex CLI: /usr/bin/codex")]
        for item in self.patches:
            item.start()
        memory = Memory(self.data)
        first = memory.start_episode("open my daily briefing", source="voice")
        memory.finish_episode(first, reply="Opened it.", status="completed", timings={"first_token": 900},
                              tools=[{"type": "tool", "label": "app_launch", "args": {"app": "Daily Briefing"},
                                      "status": "completed", "duration_ms": 6084, "success": False,
                                      "output": "x" * 5000}])
        other = memory.start_episode("(reflection)", source="system", kind="reflection")
        memory.finish_episode(other, reply="reflected", tools=[], timings={}, status="completed")
        memory.close()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_evidence_has_turns_logs_and_no_secrets(self):
        path = issues.collect("it couldn't open my daily briefing", turns=5)
        text = path.read_text()
        self.assertTrue(path.is_relative_to(self.data / "issues"))
        self.assertFalse(path.resolve().is_relative_to(issues.REPO))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("it couldn't open my daily briefing", text)
        self.assertIn("open my daily briefing", text)
        self.assertIn("`app_launch` completed, 6084 ms, success False", text)
        self.assertIn('"Daily Briefing"', text)
        self.assertIn("first_token 900", text)
        self.assertNotIn("x" * 1501, text)
        self.assertIn("x" * 1500 + "... [3500 more chars]", text)
        self.assertNotIn("reflected", text)
        self.assertIn("something failed", text)
        self.assertIn("git log -1 --oneline", text)
        self.assertIn("voice123", text)
        self.assertIn('"account": "me"', text)
        for secret in ("sk-hidden", "hunter2", "some_api_key", "daily_token_budget"):
            self.assertNotIn(secret, text)

    def test_task_file_points_at_evidence_and_states_the_rules(self):
        evidence, prompt = issues.prepare("mishears 'files' as 'fire'")
        task = evidence.with_name(evidence.stem + "-task.md")
        self.assertEqual(prompt, f"Read {task} and do what it says.")
        text = task.read_text()
        self.assertIn(str(evidence), text)
        self.assertIn("> mishears 'files' as 'fire'", text)
        for rule in ("AGENTS.md", "PLAN.md", "root cause", "python3 -m unittest discover -s tests",
                     "~/.local/share/omni/venv/bin/python", "systemctl --user restart omnid",
                     "Do not commit or push", "Never copy", "Summarize"):
            self.assertIn(rule, text)
        self.assertNotIn("only a test", text)
        self.assertIn("Change nothing", issues.task_text("test: check the reporter", evidence))

    def test_tool_delegates_visibly_in_the_repo(self):
        calls = []
        ctx = tools.Context(delegate=lambda **kw: calls.append(kw) or {"id": "d1", "mode": "visible"})
        tools.load_all()
        self.assertFalse(tools.REGISTRY["issue_report"].ask)
        ok, text = tools.run("issue_report", {"summary": "voice stopped", "agent": "codex"}, ctx)
        self.assertTrue(ok, text)
        result = json.loads(text)
        self.assertEqual(result["delegation"], "d1")
        self.assertTrue(Path(result["evidence"]).is_file())
        self.assertEqual(calls[0]["mode"], "visible")
        self.assertEqual(calls[0]["cwd"], str(issues.REPO))
        self.assertEqual(calls[0]["agent"], "codex")
        self.assertTrue(calls[0]["task"].startswith("Read ") and "\n" not in calls[0]["task"])
        ok, text = tools.run("issue_report", {"summary": "x"}, tools.Context())
        self.assertFalse(ok)
        self.assertIn("did not start", text)


if __name__ == "__main__":
    unittest.main()
