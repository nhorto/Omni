import hashlib
import importlib.util
import threading
import types
import unittest
from unittest import mock

from omni.voice.turn import SilenceTurn, make_turn_detector

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
CHUNK = 0.032


def settings(**overrides):
    values = {"turn_detector": "smart", "turn_threshold": 0.5, "turn_max_silence": 2.0, "end_silence": 1.0}
    return types.SimpleNamespace(**{**values, **overrides})


def speak(turn, chunks=10, audio=None):
    for _ in range(chunks):
        assert not turn.update(audio if audio is not None else bytearray(b"\x01\x00" * 16000), 0.9, 0.0)


def pause(turn, seconds, audio=None, start=CHUNK):
    """Feed trailing-silence chunks up to `seconds`; the silence at which the turn ended, or None."""
    audio = audio if audio is not None else bytearray(b"\x01\x00" * 16000)
    silence = start
    while silence <= seconds + 1e-9:
        if turn.update(audio, 0.1, silence):
            return round(silence, 3)
        silence += CHUNK
    return None


class SilenceTurnTest(unittest.TestCase):
    def test_ends_after_end_silence(self):
        turn = SilenceTurn(settings(end_silence=1.0))
        turn.reset()
        self.assertFalse(turn.update(bytearray(), 0.9, 0.0))
        self.assertFalse(turn.update(bytearray(), 0.1, 0.99))
        self.assertTrue(turn.update(bytearray(), 0.1, 1.0))
        self.assertTrue(turn.ready)

    def test_detector_follows_the_setting(self):
        self.assertIsInstance(make_turn_detector(settings(turn_detector="silence")), SilenceTurn)
        if not HAVE_NUMPY:
            return
        with mock.patch("omni.voice.smart_turn.SmartTurn") as smart:
            make_turn_detector(settings(turn_detector="smart"))
        smart.assert_called_once()


class StubModel:
    def __init__(self, *answers, gate=None):
        self.answers = list(answers)
        self.heard = []
        self.gate = gate

    def predict(self, samples):
        self.heard.append(len(samples))
        if self.gate is not None:
            self.gate.wait(5)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SmartTurnTest(unittest.TestCase):
    def make(self, model, **overrides):
        from omni.voice.smart_turn import SmartTurn  # noqa: PLC0415
        turn = SmartTurn(settings(**overrides), model=model)
        turn.reset()
        speak(turn)
        return turn

    def test_finished_turn_ends_at_the_first_short_pause(self):
        from omni.voice.smart_turn import CHECK  # noqa: PLC0415
        model = StubModel(0.9)
        ended = pause(self.make(model), 2.0)
        self.assertGreaterEqual(ended, CHECK)
        self.assertLess(ended, CHECK + CHUNK)
        self.assertEqual(len(model.heard), 1)

    def test_unfinished_turn_waits_for_max_silence_without_asking_again(self):
        model = StubModel(0.2)
        self.assertAlmostEqual(pause(self.make(model, turn_max_silence=1.5), 3.0), 1.5, delta=CHUNK)
        self.assertEqual(len(model.heard), 1)

    def test_threshold_is_the_setting(self):
        self.assertIsNone(pause(self.make(StubModel(0.6), turn_threshold=0.7, turn_max_silence=9), 1.0))
        self.assertIsNotNone(pause(self.make(StubModel(0.7), turn_threshold=0.7, turn_max_silence=9), 1.0))

    def test_resumed_speech_gets_a_fresh_question(self):
        model = StubModel(0.1, 0.95)
        turn = self.make(model)
        self.assertIsNone(pause(turn, 0.9))           # "Omni, remind me to..." (unfinished)
        self.assertFalse(turn.update(bytearray(b"\0\0" * 16000), 0.9, 0.0))  # "...call the garage."
        self.assertIsNotNone(pause(turn, 0.5))
        self.assertEqual(len(model.heard), 2)

    def test_slow_answer_is_taken_on_a_later_chunk(self):
        gate = threading.Event()
        turn = self.make(StubModel(0.9, gate=gate))
        audio = bytearray(b"\0\0" * 16000)
        self.assertFalse(turn.update(audio, 0.1, 0.22))   # asks, the model is still thinking
        self.assertFalse(turn.update(audio, 0.1, 0.25))
        gate.set()
        turn._pending.result(5)
        self.assertTrue(turn.update(audio, 0.1, 0.29))

    def test_answer_about_an_earlier_pause_is_ignored(self):
        gate = threading.Event()
        model = StubModel(0.9, 0.1, gate=gate)
        turn = self.make(model)
        audio = bytearray(b"\0\0" * 16000)
        self.assertFalse(turn.update(audio, 0.1, 0.22))
        pending = turn._pending
        self.assertFalse(turn.update(audio, 0.8, 0.0))     # speech resumed before the answer came back
        gate.set()
        pending.result(5)
        self.assertIsNone(pause(turn, 1.0))                 # the stale 0.9 does not end the new pause

    def test_answers_to_a_question_do_not_wait_long(self):
        from omni.voice.smart_turn import ANSWER_SILENCE  # noqa: PLC0415
        turn = self.make(StubModel(0.1, 0.1))
        turn.reset("answer")
        speak(turn)
        self.assertAlmostEqual(pause(turn, 3.0), ANSWER_SILENCE, delta=CHUNK)
        turn.reset("request")
        speak(turn)
        self.assertAlmostEqual(pause(turn, 3.0), 2.0, delta=CHUNK)

    def test_model_hears_at_most_eight_seconds(self):
        from omni.voice.smart_turn import WINDOW  # noqa: PLC0415
        model = StubModel(0.9, 0.9)
        long = bytearray(b"\0\0" * 20 * 16000)
        turn = self.make(model)
        speak(turn, audio=long)
        pause(turn, 1.0, audio=long)
        pause(self.make(model), 1.0, audio=bytearray(b"\0\0" * 16000))
        self.assertEqual(model.heard, [WINDOW, 16000])

    def test_a_blip_before_speech_does_not_end_the_turn(self):
        model = StubModel(0.99, 0.99)
        turn = self.make(model, turn_max_silence=2.0)
        turn.reset("answer")
        speak(turn, 2)                                    # a click passes VAD, then silence before "Yes"
        self.assertIsNone(pause(turn, 1.5))
        self.assertEqual(model.heard, [])
        speak(turn, 10)                                   # "Yes."
        self.assertIsNotNone(pause(turn, 0.4))

    def test_without_a_model_it_ends_like_silence(self):
        from omni.voice import smart_turn  # noqa: PLC0415
        with mock.patch.object(smart_turn, "fetch_model", side_effect=OSError("offline")), \
             self.assertLogs("omni.turn", "WARNING"):
            turn = smart_turn.SmartTurn(settings(end_silence=1.0))
            for _ in range(100):
                if turn.ready:
                    break
                threading.Event().wait(0.02)
        self.assertTrue(turn.ready)
        speak(turn)
        self.assertAlmostEqual(pause(turn, 3.0), 1.0, delta=CHUNK)

    def test_a_failing_model_falls_back_to_silence(self):
        turn = self.make(StubModel(RuntimeError("bad input")), end_silence=1.0)
        with self.assertLogs("omni.turn", "WARNING"):
            self.assertAlmostEqual(pause(turn, 3.0), 1.0, delta=CHUNK)


def model_available():
    if not HAVE_NUMPY or importlib.util.find_spec("onnxruntime") is None:
        return False
    from omni.voice.smart_turn import MODEL_DIR, MODEL_FILE  # noqa: PLC0415
    return (MODEL_DIR / MODEL_FILE).is_file()


@unittest.skipUnless(model_available(), "Smart Turn model or onnxruntime not installed")
class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from omni.voice.smart_turn import Model, fetch_model  # noqa: PLC0415
        cls.path = fetch_model()
        cls.model = Model(cls.path, threads=1)

    def test_checksum(self):
        from omni.voice.smart_turn import MODEL_SHA256  # noqa: PLC0415
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), MODEL_SHA256)

    def test_features_match_the_reference_layout(self):
        import numpy as np  # noqa: PLC0415
        rng = np.random.default_rng(0)
        short = (rng.normal(0, 3000, 16000)).astype(np.int16)
        features = self.model.features(short)
        self.assertEqual(features.shape, (1, 80, 800))
        self.assertEqual(features.dtype, np.float32)
        # Left-padded: the first 7 s are the padding floor, the last second is the clip.
        self.assertLess(float(features[0, :, :690].std(axis=1).max()), 1e-3)
        self.assertGreater(float(features[0, :, 710:].std(axis=1).max()), 1e-2)
        long = np.concatenate([rng.normal(0, 3000, 5 * 16000), short]).astype(np.int16)
        np.testing.assert_allclose(self.model.features(long), self.model.features(long[-8 * 16000:]))

    def test_features_are_faster_whispers(self):
        try:
            from faster_whisper.feature_extractor import FeatureExtractor  # noqa: PLC0415
        except ImportError:
            self.skipTest("faster-whisper not installed")
        import numpy as np  # noqa: PLC0415
        from omni.voice.smart_turn import MEL, log_mel  # noqa: PLC0415
        extractor = FeatureExtractor(feature_size=80, chunk_length=8)
        x = np.random.default_rng(2).normal(0, 0.1, 8 * 16000).astype(np.float32)
        np.testing.assert_allclose(MEL, extractor.mel_filters, atol=1e-7)
        np.testing.assert_allclose(log_mel(x), extractor(x, padding=0), atol=1e-4)

    def test_prediction_is_a_probability(self):
        import numpy as np  # noqa: PLC0415
        p = self.model.predict(np.random.default_rng(1).normal(0, 1000, 32000).astype(np.int16))
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)


if __name__ == "__main__":
    unittest.main()
