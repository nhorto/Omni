import gc
import threading
import time
import types
import unittest
import wave
from unittest import mock
from pathlib import Path

try:
    import numpy as np
except ImportError:  # the system python3 may lack numpy; the voice venv has it
    np = None

if np is not None:
    from omni.voice import moonshine
    from omni.voice.stt import WhisperBatch, make_transcriber

CHUNK = b"\x10\x00" * 512  # 32 ms of 16 kHz s16


def settings(**values):
    return types.SimpleNamespace(**{"stt": "moonshine", "stt_model": "", "whisper_model": "base.en", **values})


class FakeNative:
    def __init__(self, model):
        self.samples = 0
        self.threads = set()
        self.closed = threading.Event()
        self.updated = threading.Event()
        model.streams.append(self)

    def _result(self, prefix):
        self.threads.add(threading.current_thread().name)
        line = types.SimpleNamespace(text=f"{prefix} {self.samples},,")
        return types.SimpleNamespace(lines=[line])

    def start(self):
        self.threads.add(threading.current_thread().name)

    def add_audio(self, samples, rate):
        self.threads.add(threading.current_thread().name)
        self.samples += len(samples)

    def update_transcription(self):
        result = self._result("partial")
        self.updated.set()
        return result

    def stop(self):
        return self._result("final")

    def close(self):
        self.closed.set()


class FakeModel:
    def __init__(self):
        self.streams: list[FakeNative] = []

    def create_stream(self, update_interval):
        return FakeNative(self)


def fake_engine(model=None, fail=False):
    engine = moonshine.Moonshine(settings())

    def open_model():
        if fail:
            raise RuntimeError("no network")
        return model

    engine._open = open_model
    return engine


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@unittest.skipIf(np is None, "numpy is not installed")
class StreamTest(unittest.TestCase):
    def test_feed_partial_finish(self):
        model = FakeModel()
        engine = fake_engine(model)
        engine.load()
        self.assertTrue(engine.ready)
        stream = engine.start()
        for _ in range(40):  # 1.3 s, several streaming passes
            stream.feed(CHUNK)
        self.assertTrue(wait_for(lambda: model.streams))
        self.assertTrue(model.streams[0].updated.wait(3))
        self.assertTrue(wait_for(lambda: stream.partial().startswith("partial")))
        self.assertEqual(stream.finish(), "final 20480,")
        native = model.streams[0]
        self.assertTrue(native.closed.is_set())
        self.assertEqual(native.threads, {"moonshine"})  # never on the mic thread

    def test_short_capture_is_transcribed_at_finish(self):
        model = FakeModel()
        engine = fake_engine(model)
        engine.load()
        stream = engine.start()
        stream.feed(CHUNK)
        self.assertEqual(stream.partial(), "")
        self.assertEqual(stream.finish(), "final 512,")

    def test_abandoned_stream_is_freed(self):
        model = FakeModel()
        engine = fake_engine(model)
        engine.load()
        stream = engine.start()
        for _ in range(20):
            stream.feed(CHUNK)
        self.assertTrue(wait_for(lambda: model.streams))
        del stream
        gc.collect()
        self.assertTrue(model.streams[0].closed.wait(3))
        workers = [t for t in threading.enumerate() if t.name == "moonshine" and t.is_alive()]
        self.assertGreaterEqual(len(workers), 1)

    def test_capture_started_while_loading_waits_for_the_model(self):
        model = FakeModel()
        engine = fake_engine(model)
        stream = engine.start()
        stream.feed(CHUNK * 10)
        out = []
        finisher = threading.Thread(target=lambda: out.append(stream.finish()))
        finisher.start()
        time.sleep(0.05)
        self.assertEqual(out, [])
        engine.load()
        finisher.join(3)
        self.assertEqual(out, ["final 5120,"])

    def test_failed_load_falls_back_to_whisper(self):
        engine = fake_engine(fail=True)
        early = engine.start()  # began while the model was loading
        early.feed(CHUNK * 20)
        with mock.patch.object(WhisperBatch, "load", lambda self: setattr(self, "model", object())), \
                mock.patch.object(WhisperBatch, "transcribe", lambda self, audio: f"whisper {len(audio)}"):
            with self.assertLogs("omni.stt", "ERROR"):
                engine.load()
            self.assertIn("no network", engine.error)
            self.assertTrue(engine.ready)
            self.assertEqual(early.finish(), "")
            stream = engine.start()
            stream.feed(CHUNK)
            self.assertEqual(stream.finish(), "whisper 1024")

    def test_clean(self):
        self.assertEqual(moonshine.clean("Omni,, what's the weather  to-morrow?"), "Omni, what's the weather tomorrow?")
        self.assertEqual(moonshine.clean("我们会欢迎来自"), "")

    def test_factory(self):
        self.assertIsInstance(make_transcriber(settings()), moonshine.Moonshine)
        self.assertIsInstance(make_transcriber(settings(stt="whisper")), WhisperBatch)
        with self.assertRaises(ValueError):
            moonshine.Moonshine(settings(stt_model="huge"))


def _real_model() -> str:
    try:
        import moonshine_voice  # noqa: F401, PLC0415
    except ImportError:
        return "moonshine-voice is not installed"
    if not (moonshine.MODEL_DIR / "download.moonshine.ai/model/small-streaming-en").is_dir():
        return f"the small model is not downloaded to {moonshine.MODEL_DIR}"
    return ""


@unittest.skipIf(np is None, "numpy is not installed")
class RealModelTest(unittest.TestCase):
    def setUp(self):
        if reason := _real_model():
            self.skipTest(reason)

    def test_streams_the_bundled_sample(self):
        import moonshine_voice  # noqa: PLC0415
        with wave.open(str(Path(moonshine_voice.get_assets_path()) / "two_cities.wav")) as clip:
            self.assertEqual((clip.getframerate(), clip.getnchannels(), clip.getsampwidth()), (48000, 1, 2))
            samples = np.frombuffer(clip.readframes(48000 * 4), dtype=np.int16)  # the first four seconds
        audio = samples.reshape(-1, 3).mean(axis=1).astype(np.int16).tobytes()  # 48 -> 16 kHz
        engine = moonshine.Moonshine(settings())
        engine.load()
        self.assertTrue(engine.ready, engine.error)
        stream = engine.start()
        for i in range(0, len(audio), 1024):
            stream.feed(audio[i:i + 1024])
        text = stream.finish().lower()
        self.assertIn("best of times", text)


if __name__ == "__main__":
    unittest.main()


class TermsTest(unittest.TestCase):
    def test_misheard_names(self):
        try:
            from omni.voice.stt import fix_terms
        except ImportError:
            self.skipTest("numpy is not installed")
        self.assertEqual(fix_terms("open a quad-code instance and a terminal"), "open a Claude Code instance and a terminal")
        self.assertEqual(fix_terms("start cloud code in the repo"), "start Claude Code in the repo")
        self.assertEqual(fix_terms("launch a codecs agent"), "launch a Codex agent")
        self.assertEqual(fix_terms("move t3 code to workspace three"), "move T3 Code to workspace three")
        self.assertEqual(fix_terms("the cloud is code red"), "the cloud is code red")
