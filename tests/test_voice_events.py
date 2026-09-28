"""Live voice feedback: what the mic loop may emit while a capture runs, and the indicator that shows it."""

import threading
import types
import unittest
from unittest import mock

try:
    import numpy as np

    from omni.voice.listen import LEVEL_EVERY, VoiceLoop, level
except ImportError:  # the voice venv's packages (numpy, pysilero-vad, openwakeword) are not installed
    VoiceLoop = None

LOUD = b"" if VoiceLoop is None else (np.sin(np.arange(512) / 3) * 3000).astype(np.int16).tobytes()
QUIET = bytes(1024)


class FakeLoop:
    def call_soon_threadsafe(self, callback, *args):
        callback(*args)

    def call_later(self, delay, callback):
        return mock.Mock()


class ScriptedStream:
    """A streaming transcriber whose partial text follows a script, one entry per chunk fed."""

    def __init__(self, partials, final, preroll=0):
        self.partials, self.final, self.fed = partials, final, -preroll

    def feed(self, chunk):
        self.fed += 1

    def partial(self):
        return self.partials[min(self.fed, len(self.partials)) - 1] if self.fed > 0 and self.partials else ""

    def finish(self):
        return self.final


class FakeVad:
    def process_chunk(self, chunk):
        return 1.0 if chunk == LOUD else 0.0


class FakeTurn:
    def reset(self, purpose):
        pass

    def update(self, audio, probability, silence):
        return silence >= 0.3  # about 10 quiet chunks end the turn


@unittest.skipIf(VoiceLoop is None, "voice packages not installed")
class VoiceEventsTest(unittest.TestCase):
    def setUp(self):
        voice = object.__new__(VoiceLoop)
        voice.settings = types.SimpleNamespace(follow_up=15.0)
        voice.loop = FakeLoop()
        self.events, self.asked = [], []
        voice.daemon = types.SimpleNamespace(
            emit=self.events.append, set_listening=lambda value, transcript="": None,
            speaker=types.SimpleNamespace(recent_text=lambda seconds=30: self.said),
            session=types.SimpleNamespace(ask=lambda text, **kw: ("ask", text), interrupt=lambda: ("interrupt",)))
        voice._call = self.asked.append
        voice._once = None
        voice._ptt = mock.Mock()
        voice._stop_capture = threading.Event()
        voice._capturing = False
        voice._capture_ended = 0.0
        voice._named_until = 0.0
        voice._conversing = False
        voice._follow_up_until = 0.0
        voice._follow_up_timer = None
        voice._purpose = "request"
        voice.vad, voice.turn = FakeVad(), FakeTurn()
        voice._transcriber = types.SimpleNamespace(submit=lambda fn, *args: fn(*args))
        self.said = ""
        self.voice = voice

    def capture(self, partials, final, trigger="name", speech=20):
        """One capture: `speech` loud chunks, then silence until the turn ends."""
        stream = ScriptedStream(partials, final, preroll=6)
        self.voice.stt = types.SimpleNamespace(start=lambda: stream)
        self.voice._chunks_continuing = lambda: iter([LOUD] * speech + [QUIET] * 20)
        self.voice._capture([LOUD] * 6, heard_speech=True, trigger=trigger)

    def kinds(self):
        return [e["event"] + ("." + e["state"] if "state" in e else "") for e in self.events]

    def texts(self):
        return [e["text"] for e in self.events if e["event"] == "voice.partial"]

    def test_speech_not_for_omni_emits_nothing(self):
        self.capture(["I think", "I think we should", "I think we should get pizza"], "I think we should get pizza")
        self.assertEqual(self.events, [])
        self.assertEqual(self.asked, [])

    def test_an_addressed_capture_shows_levels_partials_and_sent(self):
        partials = ["", "", "Omni", "Omni, what's the", "Omni, what's the weather"]
        self.capture(partials, "Omni, what's the weather tomorrow?")
        kinds = self.kinds()
        self.assertEqual(kinds[0], "voice.capture.start")
        self.assertIn("voice.level", kinds)
        self.assertEqual(self.texts(), ["what's the", "what's the weather"])
        self.assertEqual(kinds[-2:], ["voice.capture.end", "voice.capture.sent"])
        self.assertEqual(self.events[-1]["text"], "what's the weather tomorrow?")
        self.assertEqual(self.asked, [("ask", "what's the weather tomorrow?")])

    def test_nothing_before_the_name_is_shown(self):
        partials = ["so yeah", "so yeah that works", "so yeah that works. Omni, open", "so yeah that works. Omni, open Files"]
        self.capture(partials, "So yeah, that works. Omni, open Files.")
        self.assertEqual(self.kinds()[0], "voice.capture.start")
        self.assertEqual(self.texts(), ["open", "open Files"])
        self.assertFalse(any("yeah" in str(e) for e in self.events))

    def test_a_conversation_shows_from_the_start(self):
        self.voice._conversing, self.voice._follow_up_until = True, float("inf")
        self.capture(["", "how about", "how about tomorrow"], "How about tomorrow?")
        self.assertEqual(self.kinds()[0], "voice.capture.start")
        self.assertEqual(self.texts(), ["how about", "how about tomorrow"])
        self.assertEqual(self.kinds()[-1], "voice.capture.sent")

    def test_thanks_in_a_conversation_is_dropped(self):
        self.voice._conversing, self.voice._follow_up_until = True, float("inf")
        self.capture(["thanks"], "Thanks.")
        self.assertEqual(self.kinds()[-1], "voice.capture.dropped")

    def test_the_hotkey_shows_from_the_start(self):
        self.capture([], "open Files", trigger="hotkey")
        self.assertEqual(self.kinds()[0], "voice.capture.start")
        self.assertEqual(self.kinds()[-1], "voice.capture.sent")

    def test_levels_are_throttled(self):
        self.capture([], "open Files", trigger="hotkey", speech=40)
        chunks = 40 + 20  # the turn ends ~10 quiet chunks in; count what was fed
        levels = [e for e in self.events if e["event"] == "voice.level"]
        self.assertLessEqual(len(levels), chunks // LEVEL_EVERY)
        self.assertGreaterEqual(len(levels), 40 // LEVEL_EVERY)
        self.assertTrue(all(0 <= e["level"] <= 1 for e in levels))

    def test_omnis_own_voice_over_it_is_not_shown(self):
        self.said = "It's 72 and sunny in Knoxville right now."
        self.voice._conversing, self.voice._follow_up_until = True, float("inf")
        self.capture(["72 and", "72 and sunny in Knoxville"], "72 and sunny in Knoxville", trigger="barge-in")
        self.assertEqual(self.events, [])

    def test_talking_over_omni_by_name_shows(self):
        self.said = "It's 72 and sunny in Knoxville right now."
        self.capture(["Omni", "Omni, stop"], "Omni, stop.", trigger="barge-in")
        self.assertEqual(self.kinds()[0], "voice.capture.start")
        self.assertEqual(self.kinds()[-1], "voice.capture.dropped")  # a stop hides at once

    def test_level_scale(self):
        self.assertEqual(level(0.0), 0.0)
        self.assertEqual(level(32768.0 ** 2), 1.0)
        self.assertAlmostEqual(level((32768 * 10 ** (-35 / 20)) ** 2), 0.5, places=2)


class DaemonEmitTest(unittest.TestCase):
    def test_voice_feedback_does_not_rewrite_status(self):
        from omni.daemon import Daemon
        daemon = object.__new__(Daemon)
        daemon.subscribers, daemon.voice = set(), None
        daemon._set_state = mock.Mock()
        daemon.emit({"event": "voice.level", "level": 0.5})
        daemon._set_state.assert_not_called()
        daemon.emit({"event": "listening", "value": True})
        daemon._set_state.assert_called_once()


def _hud_class():
    try:
        import omni.app  # noqa: F401 (loads gtk4-layer-shell first)
        from gi.repository import Gtk4LayerShell

        from omni.app.voice_hud import VoiceHud
    except (ImportError, ValueError):
        return None
    return VoiceHud if Gtk4LayerShell.is_supported() else None


class FakeWindow:
    visible = False

    def set_visible(self, value):
        self.visible = value

    def get_visible(self):
        return self.visible


class FakeBridge:
    def listen(self, callback):
        self.callback = callback


class VoiceHudTest(unittest.TestCase):
    """The indicator's states, without mapping anything on screen."""

    def setUp(self):
        hud_class = _hud_class()
        if hud_class is None:
            self.skipTest("GTK 4, a Wayland display, or gtk4-layer-shell is missing")
        self.hud = hud_class(None, FakeBridge())
        self.hud.window = FakeWindow()
        self.send = self.hud.on_event

    def tearDown(self):
        self.hud._stop()

    def test_a_capture_from_start_to_thinking_to_hidden(self):
        self.send({"event": "voice.capture", "state": "start"})
        self.assertTrue(self.hud.window.visible)
        self.assertTrue(self.hud._timer)
        self.send({"event": "voice.level", "level": 0.6})
        self.assertEqual(self.hud.target, 0.6)
        self.send({"event": "voice.partial", "text": "what's the weather"})
        self.assertEqual(self.hud.label.get_label(), "what's the weather")
        self.send({"event": "voice.capture", "state": "end"})
        self.send({"event": "voice.capture", "state": "sent", "text": "what's the weather tomorrow"})
        self.assertEqual(self.hud.mode, "thinking")
        self.send({"event": "turn.started"})
        self.assertEqual(self.hud.mode, "thinking")  # the dots stay a moment, then go
        from omni.app import voice_hud
        self.hud._stop()  # run one frame by hand
        with mock.patch.object(voice_hud.time, "monotonic", return_value=self.hud.changed_at + 0.9):
            self.hud._tick()
        self.assertFalse(self.hud.window.visible)
        self.assertEqual(self.hud._timer, 0)  # no work while hidden

    def test_dropped_hides_at_once(self):
        self.send({"event": "voice.capture", "state": "start"})
        self.send({"event": "voice.capture", "state": "dropped"})
        self.assertFalse(self.hud.window.visible)
        self.assertEqual(self.hud._timer, 0)

    def test_the_follow_up_window_shows_listening(self):
        self.send({"event": "listening", "value": True})
        self.assertEqual(self.hud.mode, "listening")
        self.assertEqual(self.hud.label.get_label(), "Listening…")
        self.send({"event": "voice.capture", "state": "start"})
        self.send({"event": "voice.capture", "state": "dropped"})
        self.assertEqual(self.hud.mode, "listening")  # still in the window
        self.send({"event": "listening", "value": False})
        self.assertFalse(self.hud.window.visible)

    def test_levels_are_ignored_unless_hearing(self):
        self.send({"event": "voice.level", "level": 0.9})
        self.assertFalse(self.hud.window.visible)
        self.assertEqual(self.hud.target, 0.0)

    def test_long_text_keeps_the_end(self):
        from omni.app.voice_hud import tail
        text = tail("word " * 40 + "the very end")
        self.assertTrue(text.startswith("…"))
        self.assertTrue(text.endswith("the very end"))
        self.assertLessEqual(len(text), 91)


if __name__ == "__main__":
    unittest.main()
