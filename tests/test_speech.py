import unittest

from omni.voice.speak import Chunker, SpeechGate, clean


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


class ChunkerTest(unittest.TestCase):
    def test_sentences_stream_out_as_they_complete(self):
        chunker = Chunker()
        out = []
        for delta in ["Tennessee lost to ", "Texas 20 to 17. The game ", "was close all day! Next week", " they play Georgia."]:
            out += chunker.feed(delta)
        self.assertEqual(out, ["Tennessee lost to Texas 20 to 17.", "The game was close all day!"])
        self.assertEqual(chunker.flush(), ["Next week they play Georgia."])

    def test_short_fragments_merge(self):
        chunker = Chunker()
        self.assertEqual(chunker.feed("Yes. It is. "), [])
        self.assertEqual(chunker.flush(), ["Yes. It is."])

    def test_markup_is_not_spoken(self):
        self.assertEqual(clean("**Paris** is the [capital](https://x.y) — see https://example.com"), "Paris is the capital — see")


class GateTest(unittest.TestCase):
    def test_answers_stream_immediately(self):
        speaker = FakeSpeaker()
        gate = SpeechGate(speaker, True)
        gate.delta("It is sunny and seventy degrees. ")
        self.assertEqual(speaker.said, ["It is sunny and seventy degrees."])

    def test_actions_stay_quiet(self):
        speaker = FakeSpeaker()
        gate = SpeechGate(speaker, True)
        gate.acted = True
        gate.delta("Opened Files on the left.")
        gate.finish()
        self.assertEqual(speaker.said, [])

    def test_actions_speak_when_asking(self):
        speaker = FakeSpeaker()
        gate = SpeechGate(speaker, True)
        gate.acted = True
        gate.delta("I opened two terminals. Which folder should the second one use?")
        gate.finish()
        self.assertEqual(len(speaker.said), 2)
        self.assertTrue(gate.spoken)

    def test_disabled(self):
        speaker = FakeSpeaker()
        gate = SpeechGate(speaker, False)
        gate.delta("Hello there, this is long enough. ")
        gate.finish()
        self.assertEqual(speaker.said, [])


if __name__ == "__main__":
    unittest.main()
