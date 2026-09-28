# Validation log

Live checks for the rebuild, newest first. Record the machine, CLI versions, and timings. Timings come from `omni ask` output or the Activity page and are measured from when omnid receives the request (for voice, from the end of speech).

## 2026-09-27 · Approvals and desktop tools on the Omarchy desktop (tools called directly, workspace 9)

| Check | Result |
| --- | --- |
| "Delete everything in the folder ~/omni-approval-test" (a scratch folder, not Downloads) | Asked 4.1 s after the request (notification, app, one spoken line); declined; all three files intact; Omni replied that nothing was removed. The prompt headline was the raw `find … -exec rm` command, so approvals now show the policy reason as the headline (spoken) and the command on the next line |
| `app_launch` foot on workspace 9, left | 195 ms, detected and placed |
| `window_focus`, `window_place` right, `window_close`, `workspace_switch` | all work (10–30 ms) |
| `type_text` with submit | typed and ran the command |
| `keys_send` CTRL+L | failed after `type_text`: wtype's temporary keymap makes Hyprland's `send_shortcut` fail with "key not found". Now falls back to focusing the window and pressing the chord with wtype; verified the terminal cleared |
| `screen_read` | 1.7 s; OCR read the typed commands and output |
| Follow-up conversation (Nick, live) | 3 turns without the name worked; a request made more than 8 s after "what would you like me to do?" was dropped. Window is now 15 s, 30 s after a question |

## 2026-09-27 · Voice upgrade after phases 1–2 (Omarchy desktop, `scripts/voice_bench.py`, quiet machine)

Same bench, 19 cases, times from when speech ended. Mid-thought pauses are now spliced into one synthesized sentence (the old per-fragment synthesis ended each fragment with falling intonation, which no end-of-turn model should accept). "Before" = `silence` + `whisper`; "after" = the new defaults (`smart` + `moonshine`).

| Measure | Before, Piper | Before, ElevenLabs | After, ElevenLabs |
| --- | --- | --- | --- |
| End of turn (endpoint median) | 1047 ms | 1035 ms | **319 ms** |
| Transcription after end of turn (median) | 454 ms | 384 ms | **195 ms** (149 ms with Moonshine multi-threaded) |
| Text ready for the agent (median) | 1487 ms | 1419 ms | **524 ms** |
| Cut-offs on mid-thought pauses | 1 | 1–2 | **0** |
| Name heard | 10/14 | 12/14 | **14/14** |
| Mean word error rate | 0.254 | 0.167 | **0.021** |

Moonshine alone (1.0 s silence endpoint): transcription median 4–5 ms after the turn ends on a quiet machine, 677 ms (up to 1.8 s) during an Android build at load 25, because it runs on one core. Smart Turn inference 15–60 ms per pause. Pocket TTS: first audio 66 ms median warm, cold start 4–9 s, 660 MB while warm. Echo cancellation: see docs/voice.md. omnid resident memory after the switch: about 610 MB (Moonshine adds about 200 MB).

## 2026-09-27 · Voice upgrade baseline (Omarchy desktop, `scripts/voice_bench.py`)

Before the voice upgrade (PLAN.md "Voice upgrade"). The bench plays 19 Piper-synthesized utterances in real time through the real `VoiceLoop` (FIFO via `mic_command`, stub session); times are from when the audio's speech ended. Settings: `turn_detector = "silence"` (1.0 s), `stt = "whisper"` (`base.en` int8).

| Measure | Result |
| --- | --- |
| End of turn detected (endpoint) | median 1047 ms (answers "yes"/"go ahead" 1054 ms) |
| Transcription after end of turn | median 392 ms (300–760 ms) |
| Text ready for the agent | median 1446 ms after the speaker stopped |
| Mid-thought pause of 1.2 s ("Omni, what's the … what's the capital of Australia?") | cut in two; the second half is dropped as not addressed to Omni |
| Name heard (wake by name) | 8/14: Whisper drops a leading "Omni" before some long sentences, even on the full clip |

Fixed while building the bench: a pipe read shorter than one chunk ended the capture mid-utterance (`_chunks_continuing`), and the 0.6 s pre-roll lost "Omni," when a pause followed it (now 1.0 s).

## 2026-09-27 · Omarchy desktop (Hyprland, 16 cores, Python 3.14.7) · codex-cli 0.157.1 · model gpt-6-luna

First install on the Omarchy machine, replacing the old `omi-voice` service and bindings.

| Check | Target | Result |
| --- | --- | --- |
| `install.py --apply` | clean | Failed at first: openwakeword requires `tflite-runtime` on Linux, which has no wheels past 3.11. Fixed by installing openwakeword `--no-deps` (Omni only uses ONNX) |
| Unit tests | all pass | 3 policy tests failed on Linux only: `mkdtemp()` put the fake home under `/tmp`, a safe root. Test now uses `/var/tmp`; 31 pass |
| `omni doctor` | all ✓ except optional | All ✓, including ElevenLabs key and voice from the keyring |
| Legacy migration | old memories appear | "migrated 6 legacy records" |
| `omni ask "what time is it"` warm | first token ≤ 1.5 s | 1.14 s warm (isolated daemon); 4.4 s first turn on a fresh thread |
| "Go to workspace 3, open a terminal on the left and run pwd, and another on the right and run date" | ≤ 6 s, no prompts, both placed | 4.2 s, first token 0.85 s, two `terminal_open` calls, placed left and right, no prompts |
| Idle CPU of omnid with wake word on | a few percent of one core | 20% at first: pysilero-vad's bundled libgomp busy-waits between 32 ms calls. `OMP_WAIT_POLICY=PASSIVE` in `listen.py` brings it to ~6% (VAD alone 43% → 3%; whisper 5 s clip 0.36 → 0.39 s) |
| Bar indicator | shows Omni | `local.omni` plugin shows "Omni" |
| Keys | no conflicts | Super+Ctrl+H was Omarchy's Hardware menu; `omni.lua` now unbinds it for Stop Omni |
| "What are the college football scores today" (voice) | sourced answer, no loop | Looped at first. Speech played on the monitor's HDMI speakers while the mic was the Yealink, so its echo cancellation could not help: Omni heard its own words ("College football.", "Sorry.", "I'm doing well.") as barge-ins and answered them. After routing speech to the Yealink (`speaker_target`), room conversation still barged in and became requests. Fixed with `talk_over()`: while Omni speaks, only a stop phrase or speech starting with its name/wake word interrupts; echoes (stem overlap with recent spoken text) and other speech are ignored. Retest: full six-sentence answer played; one room phrase and one leaked echo ("Michigan 2019 and people…") were ignored. First token 6.4 s with one `sports_scores` call, above the 5 s first-audio target |

## 2026-09-27 · MacBook (macOS, no Hyprland) · codex-cli 0.157.1 · model gpt-6-luna

What could run off-Omarchy, run against a real Codex account with an isolated `OMNI_DATA`/`OMNI_CONFIG`/`OMNI_RUNTIME`.

| Check | Result |
| --- | --- |
| Warm-thread question ("Say hi in two words", "Capital of France") | First token 0.79 s and 0.99 s; done 1.0–1.2 s; 17.8k input tokens, 17.1k cached |
| First turn on a new thread | First token 2.1–4.3 s (cold prompt cache) |
| "How did Tennessee do in college football yesterday?" | One `sports_scores` call; "Tennessee lost to No. 1 Texas, 20–17." Done in 5.8 s on a new thread |
| "Remember that my favorite college team is…" | `memory_save` during the turn; entry in USER.md with episode provenance |
| Retire thread (`omni new`) | Reflection turn ran and completed; a fresh thread answered "What's my dog called?" from notes: "Biscuit.", 2.3 s |
| "Delete ~/omni-policy-test using rm -rf", declined | Policy asked ("delete ~/omni-policy-test"); decline left the folder intact |
| Background delegation ("have an agent fix calc.py") | Sub-agent thread fixed `a - b` → `a + b`, summary line spoken/reported; Omni's own turn done in 5.8 s |
| Voice loop with synthetic speech through a fake mic | "Hey Jarvis" woke it; transcript "What time is it in Tokyo right now?" 0.88 s after capture ended; barge-in interrupted and kept the full phrase via pre-roll (0.66 s) |
| App (GTK 4.24, libadwaita 1.10) | Rendered all pages in tokyo-night and catppuccin-latte from `colors.toml`; popover streamed a live answer with its tool step |
| Unit tests | 30 pass |

Not checked here: anything that needs Hyprland, PipeWire, a speaker, a real microphone, `notify-send`, Quickshell, or ElevenLabs.

## To do on the Omarchy machine

Copy this table into a new dated section and fill it in.

| Check | Target | Result |
| --- | --- | --- |
| `omni doctor` | all ✓ except optional | |
| `omni ask "what time is it"` warm | first token ≤ 1.5 s | |
| "Go to workspace 3, open a terminal on the left and run pwd, and another on the right and run date" | ≤ 6 s, no prompts, both placed | |
| "What are the college football scores today" (voice) | first audio ≤ 5 s, sourced | |
| "Delete everything in my Downloads folder" | asks first (voice + notification + app); cancel leaves it intact | |
| "Omni, open Files" (voice) | opens, no spoken reply | |
| "Omni, what's the weather tomorrow" | first audio ≤ 2.5 s after end of speech | |
| Talk over a long answer | stops within 300 ms; new request handled | |
| Voxtype dictation (Super+Ctrl+X) | types into focused app; never reaches Omni | |
| Popover (Super+H) | appears ≤ 150 ms; themed; closes on Esc | |
| App side by side with a stock Omarchy app in three themes | not foreign-looking | |
| Idle CPU of omnid with wake word on | a few percent of one core | |
| Legacy migration | old memories appear in Memory page; old DB untouched | |
| Visible delegation ("fix the failing test in ~/Documents/cork-and-note in a terminal") | Foot opens running Codex there | |
