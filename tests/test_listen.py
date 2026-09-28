import time
import types
import unittest
from unittest import mock

try:
    from omni.voice.listen import VoiceLoop
except ImportError:  # the voice venv's packages (numpy, pysilero-vad, openwakeword) are not installed
    VoiceLoop = None


class FakeLoop:
    def call_soon_threadsafe(self, callback, *args):
        callback(*args)

    def call_later(self, delay, callback):
        return mock.Mock()


class FakeStream:
    def __init__(self, text):
        self.text = text

    def finish(self):
        return self.text


@unittest.skipIf(VoiceLoop is None, "voice packages not installed")
class FollowUpTest(unittest.TestCase):
    """After Omni answers a spoken request, a reply within `follow_up` seconds needs no name."""

    def setUp(self):
        voice = object.__new__(VoiceLoop)
        voice.settings = types.SimpleNamespace(follow_up=15.0)
        voice.loop = FakeLoop()
        voice.listening = []
        voice.daemon = types.SimpleNamespace(
            set_listening=lambda value, transcript="": voice.listening.append(value),
            speaker=types.SimpleNamespace(recent_text=lambda seconds=30: voice.said),
            session=types.SimpleNamespace(ask=lambda text, **kw: ("ask", text), interrupt=lambda: ("interrupt",)))
        voice.asked = []
        voice._call = voice.asked.append
        voice._once = None
        voice._ptt = mock.Mock()
        voice._capturing = False
        voice._named_until = 0.0
        voice._conversing = False
        voice._follow_up_until = 0.0
        voice._follow_up_timer = None
        voice.speaking = False
        voice.said = "It's 72 and sunny in Knoxville right now."
        self.voice = voice

    def hear(self, text, trigger="name"):
        """An utterance ends now; whether it is in the conversation is decided as the mic loop does."""
        now = time.monotonic()
        conversation = self.voice._conversing and now < self.voice._follow_up_until
        self.voice._finish(FakeStream(text), now, now, "request", trigger, conversation)

    def answered(self):
        """Omni took a spoken request and finished saying its answer."""
        self.voice._conversing = True
        self.voice.set_speaking(True)
        self.voice.set_speaking(False)

    def test_a_reply_after_an_answer_needs_no_name(self):
        self.answered()
        self.assertEqual(self.voice.listening[-1], True)  # the bar shows Omni is still listening
        self.hear("How about tomorrow?")
        self.assertEqual(self.voice.asked, [("ask", "How about tomorrow?")])

    def test_without_a_conversation_the_name_is_required(self):
        self.voice.set_speaking(True)
        self.voice.set_speaking(False)
        self.hear("How about tomorrow?")
        self.assertEqual(self.voice.asked, [])

    def test_the_window_closes(self):
        self.answered()
        self.voice._follow_up_until = time.monotonic() - 1
        self.hear("How about tomorrow?")
        self.assertEqual(self.voice.asked, [])

    def test_thanks_ends_the_conversation(self):
        self.answered()
        self.hear("Thanks.")
        self.assertEqual(self.voice.asked, [])
        self.assertFalse(self.voice._conversing)
        self.assertEqual(self.voice.listening[-1], False)

    def test_omnis_own_words_are_not_a_reply(self):
        self.answered()
        self.hear("72 and sunny in Knoxville")
        self.assertEqual(self.voice.asked, [])

    def test_a_typed_request_ends_the_conversation(self):
        self.answered()
        self.voice.end_conversation()
        self.hear("How about tomorrow?")
        self.assertEqual(self.voice.asked, [])

    def test_a_question_gets_a_longer_window(self):
        self.voice.said = "Not yet. What would you like me to do?"
        self.answered()
        self.assertGreater(self.voice._follow_up_until - time.monotonic(), 20)
        self.voice.said = "Done."
        self.answered()
        self.assertLess(self.voice._follow_up_until - time.monotonic(), 20)

    def test_talking_while_omni_thinks_or_speaks_counts(self):
        self.voice._close_follow_up(conversing=True)  # a spoken request was just sent
        self.hear("and also check the forecast for Saturday")
        self.assertEqual(self.voice.asked, [("ask", "and also check the forecast for Saturday")])

    def test_talking_over_omni_in_a_conversation_is_a_reply(self):
        self.voice._close_follow_up(conversing=True)
        self.voice.set_speaking(True)
        self.hear("actually open it on workspace four instead", trigger="barge-in")
        self.assertEqual(self.voice.asked, [("ask", "actually open it on workspace four instead")])

    def test_a_decision_made_in_the_window_survives_a_slow_transcript(self):
        self.answered()
        now = time.monotonic()
        self.voice._close_follow_up()  # the window closes while a long utterance is still transcribing
        self.voice._finish(FakeStream("so what else can you do for me today"), now, now, "request", "name", True)
        self.assertEqual(self.voice.asked, [("ask", "so what else can you do for me today")])

    def test_a_turn_that_never_finishes_cannot_hold_the_conversation_open(self):
        loop = mock.Mock()
        loop.call_soon_threadsafe = lambda callback, *args: callback(*args)
        self.voice.loop = loop
        self.voice._close_follow_up(conversing=True)  # request sent; no answer ever arrives
        delay, close = loop.call_later.call_args.args
        self.assertEqual(delay, 120.0)
        close()
        self.hear("an unrelated sentence much later")
        self.assertEqual(self.voice.asked, [])

    def test_follow_up_zero_turns_it_off(self):
        self.voice.settings.follow_up = 0
        self.answered()
        self.hear("How about tomorrow?")
        self.assertEqual(self.voice.asked, [])


if __name__ == "__main__":
    unittest.main()
