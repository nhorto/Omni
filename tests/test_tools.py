import json
import unittest
from unittest.mock import patch

from omni import tools
from omni.session import acted, describe, pick_fast_model
from omni.tools import hypr, research


class ToolSurfaceTest(unittest.TestCase):
    def test_every_tool_has_a_valid_schema(self):
        registry = tools.load_all()
        self.assertGreater(len(registry), 30)
        for name, spec in registry.items():
            self.assertTrue(set(spec.schema["required"]) <= set(spec.schema["properties"]), name)
            self.assertGreater(len(spec.description), 15, name)
            json.dumps(spec.spec())

    def test_run_reports_errors_to_the_model(self):
        tools.load_all()
        ok, text = tools.run("window_focus", {"address": "not-an-address"}, tools.Context())
        self.assertFalse(ok)
        self.assertIn("address", text)
        ok, text = tools.run("nope", {}, tools.Context())
        self.assertFalse(ok)

    def test_action_detection(self):
        tools.load_all()
        self.assertTrue(acted({"type": "dynamicToolCall", "tool": "app_launch"}))
        self.assertFalse(acted({"type": "dynamicToolCall", "tool": "sports_scores"}))
        self.assertFalse(acted({"type": "commandExecution", "commandActions": [{"type": "read"}]}))
        self.assertTrue(acted({"type": "commandExecution", "commandActions": [{"type": "unknown"}]}))
        self.assertEqual(describe({"id": "1", "type": "webSearch", "query": "weather"})["label"], "weather")

    def test_fast_model_choice(self):
        models = [{"id": "gpt-6-astra", "isDefault": True}, {"id": "gpt-6-luna"}, {"id": "gpt-5.6-luna"}]
        self.assertEqual(pick_fast_model(models), "gpt-6-luna")
        self.assertEqual(pick_fast_model([{"id": "x", "isDefault": True}]), "x")


class HyprTest(unittest.TestCase):
    def test_placement_respects_scale_rotation_and_bars(self):
        monitor = dict(id=1, name="DP-1", width=1920, height=1080, x=-1080, y=0, scale=1, transform=1, reserved=[0, 40, 0, 0])
        x, y, width, height = hypr.placement_geometry(monitor, "bottom-right")
        self.assertLessEqual(x + width, 0)
        self.assertLessEqual(y + height, 1920)
        left = hypr.placement_geometry(monitor, "bottom-left")
        self.assertLess(left[0] + left[2], x)

    def test_dispatch_requires_ok(self):
        with patch.object(hypr.subprocess, "run") as run:
            run.return_value.stdout = "error: nope"
            with self.assertRaises(RuntimeError):
                hypr.dispatch("hl.dsp.focus({ workspace = \"2\" })")


class KeyChordTest(unittest.TestCase):
    def test_wtype_fallback_chords(self):
        from omni.tools.desktop import _wtype_chord
        self.assertEqual(_wtype_chord(["CTRL", "L"]), ["-M", "ctrl", "-k", "l", "-m", "ctrl"])
        self.assertEqual(_wtype_chord(["CTRL", "SHIFT", "C"]), ["-M", "ctrl", "-M", "shift", "-k", "c", "-m", "shift", "-m", "ctrl"])
        self.assertEqual(_wtype_chord(["SUPER", "RETURN"]), ["-M", "logo", "-k", "Return", "-m", "logo"])
        self.assertEqual(_wtype_chord(["ALT", "F4"]), ["-M", "alt", "-k", "F4", "-m", "alt"])

    def test_a_command_approval_is_spoken_as_its_reason_only(self):
        from omni.daemon import spoken_prompt
        text = "delete files found by find\nfind /home/example/x -mindepth 1 -exec rm -rf -- {} +"
        self.assertEqual(spoken_prompt("approval", text), "Should I delete files found by find?")


class ResearchTest(unittest.TestCase):
    def test_scoreboard_parsing(self):
        payload = {"day": {"date": "2026-09-26"}, "events": [{"date": "2026-09-26T16:00Z", "competitions": [{
            "status": {"type": {"shortDetail": "Final", "state": "post"}}, "broadcasts": [{"names": ["ABC"]}],
            "competitors": [
                {"homeAway": "home", "score": "17", "curatedRank": {"current": 14}, "team": {"displayName": "Tennessee Volunteers", "abbreviation": "TENN"}},
                {"homeAway": "away", "score": "20", "curatedRank": {"current": 1}, "team": {"displayName": "Texas Longhorns", "abbreviation": "TEX"}},
            ]}]}]}
        with patch.object(research, "get", return_value=json.dumps(payload).encode()):
            result = research.scoreboard("ncaaf", "2026-09-26", "tenn")
        self.assertEqual(result["games"][0]["away"], "#1 Texas Longhorns 20")
        self.assertEqual(result["games"][0]["home"], "#14 Tennessee Volunteers 17")
        self.assertIn("groups=80", result["source"])

    def test_readable_text(self):
        title, text = research.readable("<html><title>T</title><script>x()</script><nav>menu</nav><p>Hello</p><p>World</p></html>")
        self.assertEqual(title, "T")
        self.assertEqual(text, "T\nHello\nWorld")


if __name__ == "__main__":
    unittest.main()
