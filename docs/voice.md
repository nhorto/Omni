# Voice setup and tuning

## Pieces

- **Mic**: `pw-record --rate 16000 --channels 1 --format s16 --raw -`. To pick a device, set `mic_target = "<node name>"` in `~/.config/omni/config.toml` (list nodes with `wpctl status` or `pw-cli ls Node`).
- **Voice activity**: Silero VAD via `pysilero-vad` in 32 ms chunks. An utterance ends after `end_silence` seconds (default 1.0) of trailing silence. Lower it to 0.7 for snappier turns if Omni does not cut you off mid-sentence.
- **Wake word**: openWakeWord, `wake_model` (default `hey_jarvis`) and `wake_threshold` (default 0.5).
- **Transcription**: faster-whisper on CPU with int8, `whisper_model` (default `base.en`). Try `small.en` if accuracy is poor and it still transcribes a 5-second utterance in under a second.
- **Speech**: ElevenLabs streaming (`elevenlabs_voice_id`, `elevenlabs_model`, default `eleven_flash_v2_5` for latency) piped into `mpv`. Piper is the fallback when the key or network fails. It uses `piper_voice` or the old Omi voice under `~/.local/share/omi/tts/`.

## Training the "Omni" wake word

openWakeWord ships pretrained phrases only. A custom model takes about an hour on Google Colab and needs no recordings:

1. Open the automatic training notebook linked from <https://github.com/dscripka/openWakeWord#training-new-models>.
2. Set the target phrase to `omni` (also try `hey omni`; two-word phrases have fewer false triggers), then run all cells.
3. Download the resulting `.onnx` file to `~/.local/share/omni/wake/omni.onnx`.
4. In `~/.config/omni/config.toml`, set `wake_model = "~/.local/share/omni/wake/omni.onnx"` and restart: `systemctl --user restart omnid`.
5. Tune `wake_threshold`: raise it (0.6–0.7) if it triggers on TV or conversation, lower it (0.35–0.45) if it misses you.

If accuracy stays poor with the Yealink, Picovoice Porcupine is the fallback. It needs a free personal access key and a small change in `listen.py`.

## Barge-in and echo

While Omni speaks, about 250 ms of sustained speech on the mic stops playback, interrupts the turn, and starts capturing (the preceding 0.6 s is kept, so your first word is not lost). This only works if Omni's own voice is not picked up as speech:

- The Yealink SP92 has hardware echo cancellation, so try it as is first. Ask something long, then talk over it. If Omni interrupts itself with no one talking, you need software AEC.
- For software AEC, load PipeWire's echo-cancel module. Put this in `~/.config/pipewire/pipewire.conf.d/echo-cancel.conf`, then set `mic_target = "omni-echo-cancel-source"` and `speaker_target = "omni-echo-cancel-sink"` in `config.toml`:

```
context.modules = [
  { name = libpipewire-module-echo-cancel
    args = {
      library.name = aec/libspa-aec-webrtc
      capture.props = { node.name = "omni-echo-cancel-capture" }
      source.props  = { node.name = "omni-echo-cancel-source" }
      sink.props    = { node.name = "omni-echo-cancel-sink" }
      playback.props = { node.name = "omni-echo-cancel-playback" }
    }
  }
]
```

- To turn barge-in off, set `barge_in = false` in `config.toml`.

## Dictation

Voxtype stays as Omarchy's dictation (Super+Ctrl+X types into the focused app). While Voxtype's state file says `recording`, Omni ignores the mic entirely, so dictation never becomes a request. The old `omi-dictate` guard binding is no longer needed; restore Omarchy's default binding if you replaced it.

## Checking it

```bash
journalctl --user -u omnid -f     # "listening (wake)", "heard '…' in 0.4s"
omni status                        # voice.whisper_ready, voice.error
```

The daemon logs how long each transcription took, and `omni ask` and the Activity page show per-stage timings (`first_token`, `first_audio`, `completed`) measured from the end of your speech.
