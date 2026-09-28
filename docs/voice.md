# Voice setup and tuning

## Pieces

- **Mic**: `pw-record --rate 16000 --channels 1 --format s16 --raw -`. To pick a device, set `mic_target = "<node name>"` in `~/.config/omni/config.toml` (list nodes with `wpctl status` or `pw-cli ls Node`).
- **Voice activity**: Silero VAD via `pysilero-vad` in 32 ms chunks.
- **End of turn** (`turn_detector`, default `"smart"`): after 0.2 s of silence, Smart Turn v3.2 (pipecat-ai, BSD-2, 8 MB ONNX in `~/.local/share/omni/models/smart-turn/`, about 15–50 ms per check) judges from the last 8 s of audio whether you finished. It ends the turn when p ≥ `turn_threshold` (0.5); otherwise it waits for more speech or `turn_max_silence` (1.5 s). Replies to a question or approval fall back after 0.7 s. Raise `turn_max_silence` if long thinking pauses still get cut; lower `turn_threshold` if Omni waits after finished sentences. `"silence"` ends every turn after `end_silence` (1.0 s), which is also the fallback if the model cannot load.
- **Wake word**: by default (`wake_model = "omni"`) Omni transcribes each utterance locally and acts only on ones that call it by name: "Omni, open Files", "Hey Omni, …", or "Omni" followed by a pause and the request. The name can also start a later sentence ("Sounds good. Omni, stop."). Utterances not addressed to Omni are dropped without being logged. Whisper is prompted with the name, since unprompted it hears "on me" or "Aminee". This costs a transcription per utterance, so CPU rises while people talk nearby; a trained openWakeWord model (below) avoids that. Setting `wake_model` to anything else (such as `hey_jarvis`) uses openWakeWord with `wake_threshold` (default 0.5).
- **Conversation**: after Omni answers a spoken request (out loud or by doing it), it keeps listening for `follow_up` seconds (default 15, doubled when Omni ends on a question; 0 turns it off) and the bar shows listening. A reply in that window needs no name, so "How about tomorrow?" continues the conversation. The window is also open while Omni is still thinking or speaking: three or more words said over its answer interrupt it and become the next request. Whether an utterance belongs to the conversation is decided when it ends, so a slow transcript of a long reply is not lost. Each answer reopens the window. "Thanks" or "never mind" ends it, as do silence and typed requests; Omni's own words are never taken as a reply.
- **Transcription** (`stt`, default `"moonshine"`): Moonshine v2 small-streaming (`moonshine-voice`, MIT, about 200 MB more RAM than Whisper) transcribes while you talk, so the text is ready 5–200 ms after the turn ends and partial text is available mid-utterance. It hears "Omni" without prompting. It runs on one core; `MOONSHINE_ORT_SINGLE_THREAD=0` in omnid's environment makes it slightly faster at several cores' CPU while anyone talks. `"whisper"` is faster-whisper `whisper_model` (`base.en`) on the whole clip after the turn, and the fallback if Moonshine cannot load.
- **Speech**: ElevenLabs streaming (`elevenlabs_voice_id`, `elevenlabs_model`, default `eleven_flash_v2_5` for latency) piped into `mpv`. When the key or network fails, ElevenLabs is skipped for 30 s and `offline_voice` speaks: `"pocket"` (default) is Kyutai Pocket TTS (MIT code, CC-BY-4.0 weights) in its own torch venv (`~/.local/share/omni/pocket-venv`), started on first use, streaming PCM to `pw-play` about 70 ms after each sentence, about 660 MB RAM while warm and stopped after 10 idle minutes. Piper speaks while Pocket loads (4–9 s) and whenever Pocket is missing; it uses `piper_voice` or the old Omi voice under `~/.local/share/omi/tts/`. `pocket_voice` picks a stock Pocket voice (default `alba`); cloning a voice from a sample needs Kyutai's gated weights and the speaker's consent. `speech_provider = "pocket"` uses Pocket for everything.

## Feedback

While you talk to Omni, a small pill at the bottom of the focused monitor shows moving level bars and the live transcript, then three dots for a moment once the request is sent. During a conversation's follow-up window it shows a quiet "Listening…". It is a layer-shell overlay (namespace `omni-voice`) that never takes focus and lets clicks through; `voice_hud = false` in `config.toml` turns it off. It only ever shows speech addressed to Omni: a capture started by room speech appears once its streaming transcript calls Omni by name, and then only from the name on. Hotkey captures, approval answers, and captures inside a conversation show from the start. Room speech never reaches the pill, the socket, or the log. The mic loop sends `voice.capture` (`start`, `end`, `sent` with the text, or `dropped`), `voice.level` (0–1, about 15 a second), and `voice.partial` events; the indicator does no work while hidden.

## Training an "Omni" wake-word model (optional)

Name detection above needs no training. A dedicated model is cheaper while others talk nearby. openWakeWord ships pretrained phrases only. A custom model takes about an hour on Google Colab and needs no recordings:

1. Open the automatic training notebook linked from <https://github.com/dscripka/openWakeWord#training-new-models>.
2. Set the target phrase to `omni` (also try `hey omni`; two-word phrases have fewer false triggers), then run all cells.
3. Download the resulting `.onnx` file to `~/.local/share/omni/wake/omni.onnx`.
4. In `~/.config/omni/config.toml`, set `wake_model = "~/.local/share/omni/wake/omni.onnx"` and restart: `systemctl --user restart omnid`.
5. Tune `wake_threshold`: raise it (0.6–0.7) if it triggers on TV or conversation, lower it (0.35–0.45) if it misses you.

If accuracy stays poor with the Yealink, Picovoice Porcupine is the fallback. It needs a free personal access key and a small change in `listen.py`.

## Barge-in and echo

While Omni speaks, about 250 ms of sustained speech on the mic (8 voiced 32 ms chunks averaging at least `barge_floor`, default −45 dBFS) starts a barge-in capture. Omni keeps talking until the streaming transcript calls it by name ("Omni, stop", "Yeah, Omni, …"), then stops at once; the finished utterance then stops the turn or becomes the new request. Unaddressed stop phrases ("stop", "never mind") also stop it, once the utterance ends. Words matching what Omni just said are ignored as echo. That check is a safety net. If Omni hears its own voice, every sentence still costs a transcription and a spoken "Omni" in an answer could cut it off. Keep the voice out of the mic acoustically.

Measured on 2026-09-27 with `scripts/echo_check.py` (20 s of Piper speech, Yealink SP92 mic on Bluetooth HFP, nobody talking). A barge-in means 8 voiced chunks in a row:

| Speech plays on | Mic | Barge-ins in 20 s |
| --- | --- | --- |
| Monitor (HDMI, 50%) | Yealink as is | 9 to 16 on every run: the self-hearing loop |
| Monitor (HDMI, 50%) | Echo-cancel source | 0 once converged (4 runs); 2 in the first 2 s of the first answer after the module loads (1 of 3 runs) |
| Yealink (33%) | Yealink as is (its hardware AEC) | 0 on most runs, but 10 to 14 on 2 of 10: a quiet leak (about −55 dBFS) that Silero still calls speech |
| Yealink (33%) | Echo-cancel source on top | Leaks reduced, not removed (14 → 9, 10 → 4) |

Real speech from the room still gets through. A second voice played on the monitor speakers, not through the echo-cancel sink, started a barge-in in 3 of 3 bursts in every setup. With the talker about 10 dB louder than the echo at the mic, Whisper transcribed it correctly from the echo-cancel source. When the echo was as loud as the talker (monitor at 50%, both from the same speakers), WebRTC also garbled the talker.

What to use:

- **Speech on the monitor speakers** needs software echo cancellation. Put these in `~/.config/omni/config.toml`:

  ```toml
  echo_cancel_mic = "bluez_input.XX:XX:XX:XX:XX:XX"          # the real mic
  echo_cancel_speaker = "alsa_output.pci-0000_65_00.1.hdmi-stereo"  # the real speakers
  mic_target = "omni-echo-cancel-source"
  speaker_target = "omni-echo-cancel-sink"
  ```

  Then run `python3 install.py --apply` and `systemctl --user restart pipewire pipewire-pulse wireplumber`. The restart drops audio for a moment and may reconnect Bluetooth. `omni doctor` checks that both nodes exist. The installed drop-in is `integration/pipewire/omni-echo-cancel.conf` with your devices filled in. It loads PipeWire's `libpipewire-module-echo-cancel` with `aec/libspa-aec-webrtc` and pins its capture and playback to those two devices with `target.object`, so default devices and other apps are untouched. The cost is about 2% of one core. Cancellation only removes sound played through `omni-echo-cancel-sink`, so every Omni player must honor `speaker_target` (mpv and pw-play do; the ffplay fallback does not). Music or video from other apps is not cancelled. With the monitor's display asleep, HDMI audio may drop out.
- **Speech on the Yealink** relies on its hardware AEC. WebRTC on top neither hurt nor fixed the occasional leak, so leave `mic_target` and `speaker_target` on the Yealink nodes and skip the drop-in. The leak is 20 to 30 dB quieter than a person talking. Requiring the 8 barge-in chunks to average at least −45 dBFS removed every leak trigger in the recordings and kept every real one. `echo_check.py` reports that count in its `>=-45dB` column.
- To try a setup without installing anything: `~/.local/share/omni/venv/bin/python scripts/echo_check.py --play omni-echo-cancel-sink --record <mic> --record omni-echo-cancel-source --echo-cancel <mic> <speaker>`. This runs the module in a separate `pipewire -c` client for one run, with no restart. Add `--talker <sink>` for a second voice.
- To turn barge-in off, set `barge_in = false` in `config.toml`.

## Dictation

Voxtype stays as Omarchy's dictation (Super+Ctrl+X types into the focused app). While Voxtype's state file says `recording`, Omni ignores the mic entirely, so dictation never becomes a request. The old `omi-dictate` guard binding is no longer needed; restore Omarchy's default binding if you replaced it.

## Checking it

```bash
journalctl --user -u omnid -f     # "turn: p(finished) …", "heard '…' {'endpoint': …, 'transcribe': …}"
omni status                        # voice.stt, voice.turn, voice.stt_error, voice.error
```

Per-turn timings (`endpoint`, `transcribe`, `first_token`, `first_audio`, `completed`) are measured from when you stopped talking; `omni ask` and the Activity page show them. To compare settings without talking, `scripts/voice_bench.py` plays synthesized requests through the real mic loop (`--set turn_detector=silence`, `--voice elevenlabs`, `-v`); results go to `$OMNI_RUNTIME/bench/`.
