import asyncio
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from omni.voice import pocket, speak
from omni.voice.speak import Chunker, SpeechGate, Speaker, addressed, clean, is_echo, play_argv, talk_over


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



class EchoTest(unittest.TestCase):
    # Fragments the Yealink picked up from Omni's voice on the monitor speakers, 2026-09-27.
    spoken = ("Yesterday was a wild day in college football. Sorry, no worries. "
              "I'm doing well, thanks for asking. It feels like me too.")

    def test_own_words_are_echo(self):
        for heard in ("College football.", "Sorry.", "No worry.", "I'm doing well.", "like me too."):
            self.assertTrue(is_echo(heard, self.spoken), heard)

    def test_user_interruptions_are_not_echo(self):
        for heard in ("Stop.", "Actually, open my email instead.", "What about Tennessee?"):
            self.assertFalse(is_echo(heard, self.spoken), heard)

    def test_nothing_spoken_is_never_echo(self):
        self.assertFalse(is_echo("College football.", ""))



class TalkOverTest(unittest.TestCase):
    spoken = "Saturday was packed with upsets. No. 21 Florida stunned No. 4 Ole Miss 52 to 28."

    def test_room_speech_and_echo_are_ignored(self):
        # Heard on the Yealink while Omni answered, 2026-09-27: a conversation in the room.
        for heard in ("I don't know if", "like a discussion on this.", "Number four, this can be a V2 thing, I think.",
                      "Florida stunned Ole Miss.", "", " . "):
            self.assertEqual(talk_over(heard, self.spoken), ("ignore", ""), heard)

    def test_stop_phrases_stop(self):
        for heard in ("Stop.", "Wait", "Never mind.", "Okay, thanks.", "Omni, stop.", "Hey Jarvis."):
            self.assertEqual(talk_over(heard, self.spoken), ("stop", ""), heard)

    def test_addressed_requests_replace_the_answer(self):
        self.assertEqual(talk_over("Omni, open my email instead.", self.spoken), ("ask", "open my email instead."))
        self.assertEqual(talk_over("Hey Omni, what about Tennessee?", self.spoken), ("ask", "what about Tennessee?"))

    def test_the_name_mid_sentence_still_stops(self):
        self.assertEqual(talk_over("Yeah sounds good, Omni, stop.", self.spoken), ("stop", ""))

    def test_a_sentence_that_merely_starts_with_stop_is_not_a_stop(self):
        self.assertEqual(talk_over("Wait until you see what they did to the budget last quarter", self.spoken)[0], "ignore")



class AddressedTest(unittest.TestCase):
    def test_requests_that_call_omni_by_name(self):
        cases = {"Omni Open Files": "Open Files", "Hey Omni, what time is it?": "what time is it?",
                 "Ok Omni, put a terminal on the left.": "put a terminal on the left.",
                 "Yeah that sounds good. Omni, stop.": "stop.", "Omni.": ""}
        for heard, request in cases.items():
            self.assertEqual(addressed(heard), request, heard)

    def test_other_speech_is_not_for_omni(self):
        for heard in ("We should ask about the budget.", "I talked to omnivore people", "Whatever, Omnipotent.", ""):
            self.assertIsNone(addressed(heard), heard)

    def test_moonshine_spellings_of_the_name(self):
        cases = {"Hayamani what time is it": "what time is it", "Hey Amni, mute the volume.": "mute the volume.",
                 "Omni: stop": "stop", "Yeah sounds good, Omni, stop.": "stop."}
        for heard, request in cases.items():
            self.assertEqual(addressed(heard), request, heard)
        for heard in ("on me please", "I asked Omni about it yesterday", "Hi, I'm here", "Omnibus is late",
                      "Annie get your gun", "Hay is for horses"):
            self.assertIsNone(addressed(heard), heard)



class SpeakerStopTest(unittest.TestCase):
    def test_stopping_with_a_prefetched_sentence_leaves_omni_quiet(self):
        # 2026-09-27: "Okay, thanks" stopped Omni mid-answer, but a sentence prefetched for the old
        # answer left `speaking` stuck on, so later "Hey Omni" requests were treated as talk-over.
        async def scenario():
            states = []
            speaker = Speaker(types.SimpleNamespace(speech=True), on_state=states.append)
            playing = threading.Event()
            release = threading.Event()
            speaker._synthesize = lambda sentence: ("wav", iter([b""]))
            speaker._play = lambda chunks, generation: (playing.set(), release.wait(2))
            speaker.start()
            for sentence in ("One.", "Two.", "Three."):
                speaker.say(sentence)
            await asyncio.get_running_loop().run_in_executor(None, playing.wait, 2)
            speaker.stop()
            release.set()
            await asyncio.sleep(0.2)
            speaker._worker.cancel()
            return speaker.speaking, states
        speaking, states = asyncio.run(scenario())
        self.assertFalse(speaking)
        self.assertEqual(states[-1], False)


def settings(**overrides):
    values = dict(speech=True, speech_provider="elevenlabs", elevenlabs_voice_id="voice", elevenlabs_model="m",
                  offline_voice="pocket", piper_voice="", extra={})
    return types.SimpleNamespace(**{**values, **overrides})


class FakePocket:
    rate = 24000

    def __init__(self, available=True, fails=False):
        self._available, self.fails, self.waits = available, fails, []

    def available(self):
        return self._available

    def stream(self, text, wait=0):
        self.waits.append(wait)
        if self.fails:
            raise RuntimeError("Pocket is not ready")
        return iter([b"pcm"])


class FallbackTest(unittest.TestCase):
    def setUp(self):
        speak.log.disabled = True
        self.addCleanup(setattr, speak.log, "disabled", False)

    def speaker(self, pocket_voice, **overrides):
        speaker = Speaker(settings(**overrides))
        speaker._key = "key"
        speaker._pocket = pocket_voice
        self.elevenlabs_calls = 0

        def elevenlabs(sentence):
            self.elevenlabs_calls += 1
            raise OSError("offline")
        speaker._elevenlabs = elevenlabs
        speaker._piper = lambda sentence: iter([b"wav"])
        return speaker

    def test_elevenlabs_failure_uses_pocket_then_skips_elevenlabs_for_a_while(self):
        voice = FakePocket()
        speaker = self.speaker(voice)
        self.assertEqual(speaker._synthesize("Hello there.")[::2], ("pcm", 24000))
        self.assertEqual(speaker._synthesize("And again.")[0], "pcm")
        self.assertEqual(self.elevenlabs_calls, 1)
        self.assertEqual(voice.waits, [0, 0])  # as a fallback, a cold Pocket never holds up the sentence

    def test_missing_or_failing_pocket_uses_piper(self):
        for voice in (FakePocket(available=False), FakePocket(fails=True)):
            self.assertEqual(self.speaker(voice)._synthesize("Hello there.")[0], "wav")

    def test_piper_offline_voice_never_starts_pocket(self):
        voice = FakePocket()
        self.assertEqual(self.speaker(voice, offline_voice="piper")._synthesize("Hello there.")[0], "wav")
        self.assertEqual(voice.waits, [])

    def test_pocket_provider_skips_elevenlabs_and_waits_for_a_cold_start(self):
        voice = FakePocket()
        speaker = self.speaker(voice, speech_provider="pocket", offline_voice="piper")
        self.assertEqual(speaker._synthesize("Hello there.")[0], "pcm")
        self.assertEqual(self.elevenlabs_calls, 0)
        self.assertEqual(voice.waits, [speak.POCKET_COLD])


class PlayTest(unittest.TestCase):
    def test_pcm_streams_raw_into_pw_play_on_the_speaker_target(self):
        self.assertEqual(play_argv("pcm", "bluez_output.X", 24000),
                         ["pw-play", "--raw", "--format=s16", "--rate=24000", "--channels=1", "--target=bluez_output.X", "-"])
        self.assertNotIn("--target", " ".join(play_argv("pcm", None, 24000)))

    def test_stop_cuts_streaming_audio_and_cancels_the_source(self):
        class Endless:
            closed = False

            def __iter__(self):
                return self

            def __next__(self):
                time.sleep(0.01)
                return b"\0" * 480

            def close(self):
                self.closed = True
        source = Endless()
        speaker = Speaker(settings())
        done = threading.Event()
        with mock.patch.object(speak, "play_argv", return_value=["cat"]):
            thread = threading.Thread(target=lambda: (speaker._play(("pcm", source, 24000), 0), done.set()))
            thread.start()
            time.sleep(0.2)
            started = time.monotonic()
            speaker.stop()
            self.assertTrue(done.wait(2))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(source.closed)
        self.assertIsNotNone(speaker._player.poll())


FAKE_SERVER = r"""
import json, struct, sys
out = sys.stdout.buffer
def send(request, kind, payload=b""):
    out.write(struct.pack("<IBI", request, kind, len(payload)) + payload); out.flush()
send(0, 3, json.dumps({"rate": 16000}).encode())
for line in sys.stdin:
    message = json.loads(line)
    if message.get("text") == "crash":
        sys.exit(1)
    if "text" in message:
        for _ in range(3):
            send(message["id"], 0, b"\x01\x00" * 4)
        send(message["id"], 1)
"""


@mock.patch.object(pocket.shutil, "which", return_value="/usr/bin/pw-play")
class PocketClientTest(unittest.TestCase):
    def client(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        script = Path(folder.name) / "server.py"
        script.write_text(FAKE_SERVER)
        client = pocket.Pocket(argv=[sys.executable, str(script)])
        self.addCleanup(client.stop)
        return client

    def test_streams_framed_pcm_and_reports_the_rate(self, _which):
        client = self.client()
        self.assertEqual(b"".join(client.stream("Hello there.", wait=10)), b"\x01\x00" * 12)
        self.assertEqual(client.rate, 16000)
        self.assertEqual(client._streams, {})

    def test_cold_fallback_does_not_wait(self, _which):
        client = self.client()
        with self.assertRaises(RuntimeError):
            client.stream("Hello there.", wait=0)
        self.assertTrue(client.wait(10))

    def test_a_dead_server_fails_fast_and_backs_off(self, _which):
        pocket.log.disabled = True
        self.addCleanup(setattr, pocket.log, "disabled", False)
        client = self.client()
        client.start()
        client.wait(10)
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            client.stream("crash", wait=1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(client.available())

    def test_missing_venv_is_not_available(self, _which):
        self.assertFalse(pocket.Pocket(python="/nonexistent/python").available())


@unittest.skipUnless(pocket.PYTHON.is_file() and (pocket.MODELS / "models--kyutai--pocket-tts-without-voice-cloning").is_dir(),
                     "Pocket TTS is not installed")
class RealPocketTest(unittest.TestCase):
    def test_speaks_a_sentence(self):
        client = pocket.Pocket()
        self.addCleanup(client.stop)
        self.assertTrue(client.wait(0) or (client.start() or client.wait(120)))
        started = time.monotonic()
        audio = client.stream("Testing the offline voice.", wait=1)
        first = time.monotonic() - started
        data = b"".join(audio)
        self.assertLess(first, 1.5)
        self.assertGreater(len(data) / 2 / client.rate, 0.5)


if __name__ == "__main__":
    unittest.main()
