# Omni rebuild plan

Status: agreed direction, 2026-09-27. This document is the brief for the next implementation agent. Read it before touching code. The old design is described in [SPEC.md](SPEC.md) and [ROADMAP.md](ROADMAP.md); where they disagree with this file, this file wins.

## Why we are rebuilding the core

The current build treats the model as a one-shot JSON planner and a hand-written Python broker as the executor. Every request spawns a fresh `codex exec` or `claude -p`, sends a 6–10 KB prompt (full app catalog, window list, schema, memory), waits 9–17 s for a plan, runs the plan blind, and for questions spawns a *second* cold agent call to phrase the answer. Piper then synthesizes the whole reply before any audio plays. A football-score question launches a headless Chromium on top of that. That is the 20–30 s Nick experiences.

The same design is why Omni cannot grow: each new capability needs a schema entry, validator, describer, executor, and prompt paragraph; the model never sees a tool result and cannot adjust; memory is keyword search over verbatim sentences that only saves facts beginning with "I" or "My"; and none of Omarchy's ~145 `omarchy-*` commands or its shipped agent skill are used.

Keep: `hypr.py`, `reminders.py`, the bar/indicator plugin, the privacy rules (data outside the checkout, private permissions, public-file check), and the idea of asking before consequential actions. Replace: the planner/broker loop, the file-polling voice bridge, the memory model, and the per-request subprocess model.

## Build status (updated 2026-09-27)

Built from a Mac without Omarchy, so everything Hyprland-, PipeWire-, and speaker-shaped is written but unverified. "Verified" below means run for real against codex-cli 0.157.1 on macOS; see [VALIDATION.md](VALIDATION.md) for timings.

| Phase | State | Verified | Left to do on the Omarchy box |
| --- | --- | --- | --- |
| 0 Groundwork | Done. Tag `v0-planner`, `legacy/`, `omni/` package, `omnid`, `omni` CLI | Warm turn first token 0.8–1.0 s, "What is 17×23" end to end | Run `install.py --apply`, enable services, confirm the same timings on this CPU/network |
| 1 Agent core + desktop tools + app | Done in code. Codex adapter, 35 tools, policy, libadwaita app and popover, bar plugin, Hyprland file | Scores via ESPN in one tool call; declined `rm -rf` left folder intact; allow/ask/deny policy tests; app rendered in two Omarchy themes; popover streamed a live answer | Every desktop tool against real Hyprland: the Phase 1 acceptance request (workspace 3, two terminals); `app_launch` detection and placement; `keys_send`/`type_text`; `screen_read`; the popover window rule; bar plugin loads in Quickshell; notify-send approval buttons |
| 2 Voice | Done in code. Mic loop, wake word, push-to-talk, hands-free, barge-in, ElevenLabs streaming, Piper fallback | Wake word, capture, transcription (0.66–0.88 s), and barge-in on synthetic speech through a fake mic | Everything with a real mic and speakers: `pw-record --raw` flag, Yealink echo, ElevenLabs streaming with Nick's voice, first-audio timing, CPU at idle, the Phase 2 acceptance list, training the "Omni" wake word (docs/voice.md) |
| 3 Memory + skills | Done. Stores, tools, inline saving, retirement + reflection, app pages, legacy migration | Fact saved mid-turn, reflection on retirement, fresh thread recalled it; unit tests | Run migration against Nick's real `~/.local/share/omi/memory.sqlite3`; check a skill appears after a repeated multi-step task |
| 4 Delegation + Claude | Done in code. Delegation (visible terminal; background Codex or Claude). Claude Agent SDK adapter (`omni/claude.py`) with the same tools (in-process MCP), policy (`can_use_tool`), memory, and skills (local plugin); Assistant switch in Settings | Background Codex and background Claude agents each fixed a bug in a scratch repo and reported back; Claude as the assistant saved a fact via the tools, answered scores from ESPN, first token 1.2 s warm | Visible mode in Foot; Phase 4 acceptance list; check Claude's user settings allow rules do not pre-approve anything on the deny list (they bypass `can_use_tool`) |
| 5 Email | Gmail and Yahoo live; Microsoft 365 waiting on the Entra app. Local-first stack (mbsync + goimapnotify IDLE + notmuch + msmtp, aerc as terminal client); `omni/tools/mail.py` with search/read/draft/send/sync; `mail_send` asks with the full message; shell sending is on the ask list. See docs/email.md | Gmail fully synced (11.9k messages) and Yahoo syncing (Yahoo exposes only the newest 10k per folder and rate-limits); IDLE watchers and timer running; `omni ask` answered from mail (search ~10 ms); approved sends Yahoo→Gmail and Gmail→Yahoo delivered (Yahoo filed the second in Bulk); all three providers file SMTP mail in Sent themselves | Microsoft 365: Entra app registration + admin consent, then ortie browser sign-in and enabling the commented Outlook blocks. Triage step 1 (rules, sender table, filtered notifications, digest) is live; next is step 2, the AI sort of `triage/unsure` (see Email triage below) |

Decisions made while building, which override the text further down where they differ:

- **Tools are Codex dynamic tools, not an MCP server.** `thread/start` takes `dynamicTools`, and Codex calls them back over the same JSON-RPC pipe (`item/tool/call`). This needs no extra process or port. The Claude adapter should expose the same `omni.tools.REGISTRY` through the Agent SDK's in-process MCP server.
- **Approvals go through `approvalPolicy: "untrusted"` with `sandbox: "danger-full-access"`.** Codex asks omnid about every non-trivial command and file change, and `policy.py` answers in microseconds. Nothing reaches Nick unless it is on the deny list.
- **The model is picked at startup.** The first `*-luna` model in `model/list` (currently `gpt-6-luna`) for voice turns, CLI default for delegations. Override with `model` in `~/.config/omni/config.toml`.
- **Voice libraries:** `pysilero-vad` (ONNX-free Silero, 512-sample chunks), `openwakeword` 0.6 (`Model(wakeword_models=[...], inference_framework="onnx")`), `faster-whisper` `base.en` int8.
- **Speech is per-sentence HTTP streaming** to ElevenLabs (`/stream`, `eleven_flash_v2_5`) with one sentence of prefetch, not the WebSocket input stream. Switch only if first-audio misses 2.5 s because of it.
- **The app is resident** (`omni-app.service`) so Super+H is a D-Bus call (`gapplication action dev.omni.Omni quick-ask`).

- **Claude runs with `ENABLE_TOOL_SEARCH=false`** so Omni's tools are called directly instead of through a deferred-tool search round trip. Its sessions load user settings (`setting_sources=["user"]`) so the Omarchy skill is available; permission allow rules there skip Omni's policy.
- **Both adapters pre-warm** the foreground conversation when omnid starts.

**Next steps, in order:** (1) install on the Omarchy box and run the Phase 1 and Phase 2 acceptance lists, fixing what breaks and recording timings in VALIDATION.md; (2) train the Omni wake word; (3) email; (4) the voice upgrade below: Phase 3 live check with Nick.

## Voice upgrade (agreed 2026-09-27; phases 0–2 done, live check with Nick next)

Goal: the ChatGPT voice-mode feel without leaving the local pipeline. Research (2026-09-27) found four reasons Omni feels slow: a fixed 1.0 s silence ends every turn; Whisper runs on the whole clip only after that; barge-in waits for silence plus Whisper before deciding; there is no echo cancellation. Nick chose to adopt open-source parts piece by piece (not a Pipecat/LiveKit rebuild, which would redo approvals, tools, and the speech gate). OpenAI's GPT-Live-1 (full-duplex voice in front, our Codex/Claude session behind it via client delegation, about $3 per open hour) is a later experiment, not part of this work.

| Part | What | Owner file(s) |
| --- | --- | --- |
| B1 | PipeWire WebRTC echo cancellation, so Omni never hears itself on speakers | `integration/pipewire/`, `install.py`, `omni/doctor.py`, `docs/voice.md` (barge-in section) |
| B2 | Smart Turn v3.x (pipecat-ai, BSD-2, 8 MB int8 ONNX, CPU): a short VAD pause asks the model whether the user finished | `omni/voice/turn.py`, `tests/test_turn.py` |
| B3 | Streaming STT (Moonshine v2 streaming, MIT, CPU): transcribe while the user talks so the text is ready at end of turn; partials enable fast barge-in | `omni/voice/stt.py`, `tests/test_stt.py` |
| B4 | Kyutai Pocket TTS as the offline voice (streams, ~200 ms first audio on CPU), Piper kept as last resort | `omni/voice/pocket.py`, fallback branch of `Speaker._synthesize` in `speak.py`, `tests/test_speech.py` |

Machine facts that shape it: Ryzen 7 8745HS (16 threads), Radeon 780M and no CUDA, **11 GB RAM with ~3 GB free**, Python 3.14 venv with onnxruntime 1.30 and faster-whisper 1.2.1 already installed. Mic and speech both on the Yealink SP92 over Bluetooth (`mic_target`/`speaker_target` in `~/.config/omni/config.toml`); the default sink is the HDMI monitor, which is where the echo loop happened.

### Phase 0: groundwork (orchestrator, sequential, ~1 h) — done 2026-09-27

Done: seams (`turn.py`, `stt.py`), settings, `speech_end` timing (`endpoint`/`transcribe` in turn timings), `scripts/voice_bench.py`, baseline in VALIDATION.md. No checkpoint commit (not yet approved). Phase 1 agents launched the same day.


Done before any agent starts, so the four parts never edit the same file.

1. **Honest timing.** `t0` today is when capture ended, which hides the 1.0 s silence wait. Track `speech_end` (the last voiced chunk) in `listen.py`, pass it as `t0`, and add `endpoint` (capture end − speech end) and `transcribe` to the turn timings. Every later number is measured from when Nick actually stopped talking.
2. **Seams in `listen.py`.** Move the end-of-turn decision behind `turn.py` (`SilenceTurn`: `reset()`, `update(chunk, prob, silence) -> done`, today's behavior) and transcription behind `stt.py` (`Transcriber.load()`, `start() -> Stream`, `Stream.feed(chunk)`, `Stream.partial()`, `Stream.finish() -> str`; `WhisperBatch` is today's behavior). `listen.py` picks the implementation from settings and does not change again until Phase 2.
3. **Settings, once.** Add to `config.py`: `turn_detector = "silence"` (`"smart"`), `turn_threshold = 0.5`, `turn_max_silence = 2.0`, `stt = "whisper"` (`"moonshine"`), `stt_model = ""`, `offline_voice = "piper"` (`"pocket"`). Defaults keep today's behavior; Phase 2 flips them after the numbers are in.
4. **Bench harness.** `scripts/voice_bench.py` runs the real `VoiceLoop` capture path on WAV files (a `mic_command` setting replaces `pw-record`; a stub session records `ask()` text and timestamps). It prints per-utterance endpoint ms, transcribe ms, transcript, name detected, and premature cut-offs. Test utterances are synthesized (ElevenLabs or Piper) into `$OMNI_RUNTIME/bench/`, never the repo: short commands, long questions, sentences with a mid-thought pause ("Omni, remind me to… um… call the garage tomorrow"), bare "Omni" then the request, and yes/no approval answers.
5. **Baseline.** Run the bench and one live `omni ask`, record numbers in VALIDATION.md. Unit tests green.
6. **Checkpoint commit** of the current tree (only with Nick's OK), so any part can be rolled back.

### Phase 1: four agents in parallel (~2–3 h wall clock)

All work in the same checkout on their own files (table above). None edit `listen.py`, `config.py`, `requirements.txt`, `PLAN.md`, or `VALIDATION.md`; each reports needed dependencies and numbers back and the orchestrator merges them. Models and voice samples live under `~/.local/share/omni/models/` (downloaded on first use, checksum pinned), never in the repo. No commits. Test against a dev daemon with `OMNI_DATA`/`OMNI_CONFIG`/`OMNI_RUNTIME`, not the live `omnid`. Each ends with `python3 -m unittest discover -s tests` green.

- **B1 Echo cancellation.** Write `integration/pipewire/omni-echo-cancel.conf` (`libpipewire-module-echo-cancel`, `aec/libspa-aec-webrtc`, source and sink named `omni-echo-cancel-*`, pinned to the Yealink with `target.object` so other apps and the default devices are untouched), install it from `install.py`, add a doctor check. Measure with the bench: play 20 s of Omni speech through the echo-cancel sink on the Yealink and on the HDMI monitor, record the raw mic and the echo-cancel source, compare Silero speech fraction and level. Also decide whether the Yealink's own hardware AEC is enough on its own (software AEC on top can hurt). Check every playback path (mpv, pw-play, Pocket) honors `speaker_target`. Restarting PipeWire briefly drops audio and may reconnect the Bluetooth device: warn Nick with a notification first. *Accept:* 0 barge-in triggers during 20 s of Omni speech with nobody talking, on both speaker setups; real speech over it still reaches VAD.
- **B2 Smart Turn.** `SmartTurn` in `turn.py`: after 0.2 s of VAD silence, run the model on the last ≤8 s of the utterance; end the turn if p ≥ `turn_threshold`, otherwise re-check as the silence grows and fall back to `turn_max_silence`. The hotkey still ends a turn at once. Compute Whisper log-mel features with `faster_whisper.feature_extractor` instead of pulling in `transformers`; prove parity against the reference extractor in a throwaway venv under `/tmp`. Measure inference time on this CPU; if it would stall the mic thread, run it on a worker. *Accept on the bench:* median endpoint ≤ 400 ms on finished sentences, no cut-offs on mid-thought pauses, short answers ("yes", "go ahead") end fast, idle CPU unchanged.
- **B3 Streaming STT.** First confirm Moonshine v2 streaming installs on Python 3.14 with onnxruntime 1.30, and measure its resident memory (3 GB free). If it is not viable, fall back to incremental faster-whisper (re-transcribe the growing buffer about once a second on a worker so `finish()` only covers the tail) and say why. Evaluate against `WhisperBatch` on the bench: accuracy against the known text, "Omni" detected as the first word (the wake-by-name feature depends on it, and Whisper needed a prompt to hear it), transcript ready after endpoint, CPU while listening. Propose `WAKE_PHRASES` aliases for any consistent mishearing of the name. *Accept:* transcript ≤ 150 ms after endpoint, accuracy no worse than `base.en`, name detected on ≥ 95% of bench utterances.
- **B4 Pocket TTS.** Check the `pocket-tts` package and its dependencies. If it needs torch, keep it out of omnid's venv: a separate venv under `~/.local/share/omni/` running a small local server that streams PCM, started on demand when ElevenLabs fails and kept warm ten minutes. Stream raw PCM into `pw-play` so the first word plays before synthesis finishes. Order: ElevenLabs → Pocket → Piper. Option to try: clone Nick's ElevenLabs voice from a short sample generated once and kept under `~/.local/share/omni/`, so the fallback sounds like the main voice. *Accept:* with `speech_provider = "pocket"`, first audio ≤ 400 ms per sentence warm, cold start recorded; Piper still used when Pocket is missing.

**Results (2026-09-27).** All four met their bar; details in VALIDATION.md and docs/voice.md.

- B1: echo cancel is installed and live. Nick chose speech on the monitor through `omni-echo-cancel-sink`, with the Yealink as mic through `omni-echo-cancel-source` (0 self-barge-ins once converged). The Yealink's own AEC leaks about −55 dBFS on 3 of 10 runs, so barge-in also needs 8 chunks averaging ≥ `barge_floor` (−45 dBFS).
- B2: Smart Turn v3.2 in `smart_turn.py` asks once per pause (p rises with silence alone, so re-checking would cut mid-thought pauses), `turn_max_silence = 1.5`, replies fall back after 0.7 s. It has its own mel code because importing `faster_whisper` from two threads at startup deadlocks.
- B3: Moonshine v2 small-streaming (`moonshine-voice==0.1.5`, own onnxruntime, +200 MB RAM, one core). Hears "Omni" unprompted; `WAKE_PHRASES` gained its spellings and `CALLED` catches "…, Omni, stop" mid-sentence.
- B4: Pocket TTS 3.3.0 in `~/.local/share/omni/pocket-venv` via `integration/pocket/server.py`, about 70 ms to first audio warm. No voice clone: Nick's ElevenLabs voice is a library voice, so cloning it is not allowed; Nick picks a stock voice (`pocket_voice`) in Phase 3.

### Phase 2: integration (orchestrator, ~1–2 h) — done 2026-09-27

Defaults flipped (`stt = "moonshine"`, `turn_detector = "smart"`, `offline_voice = "pocket"`), installer and doctor steps added, omnid restarted on them. Barge-in now interrupts as soon as a streaming partial calls Omni by name. After a bare "Omni" ends a capture, the next utterance within 4 s counts as addressed. Bench, ElevenLabs voice: text ready 524 ms after speech ends (was 1419), 0 cut-offs (was 1–2), name 14/14. Left for Phase 3: the barge-in stop time on real speech, `barge_floor` against Nick's voice, and the Pocket voice choice.


1. Add the dependencies (`requirements.txt`, `install.py`), flip the defaults for parts that met their bar, restart `omnid`, re-run the bench and compare with the baseline.
2. **Fast barge-in.** With echo cancellation and streaming partials in place, run `talk_over()` on each partial while Omni is speaking and act as soon as it says stop or ask, instead of after the endpoint and a full transcription. *Accept:* speech stops within ~300 ms of "Omni, stop" ending; room talk and Omni's own voice never stop it.
3. `scripts/check_public.py`, full tests, then update this section, the Voice pipeline section, docs/voice.md, and VALIDATION.md.

### Phase 3: live check with Nick (~30 min)

Real voice on the Yealink and on the monitor speakers: five normal requests, a sentence with a long thinking pause, talking over a long answer, "Omni, stop", a voice approval, and the network off (Pocket speaks). Tune `turn_threshold` and `turn_max_silence` from what he says, then record the final numbers.

## Decisions (from Nick, 2026-09-27)

| Area | Decision |
| --- | --- |
| Agent runtime | Codex is preferred. Claude is a second adapter, not a fallback that has to be equal on day one. |
| Implementation | Full rewrite of the core is fine. Choose whatever is better; nothing is sacred. |
| Friction | Do not ask before opening windows, launching apps, switching workspaces, typing into apps, running ordinary commands. Ask only for the deny list below. |
| Memory | Omni writes its own memories and skills without pre-approval. Everything it writes stays viewable, editable, and deletable in the app. |
| Speech | ElevenLabs is the voice (Nick's voice ID already exists). Speak answers to questions. Stay silent after actions unless Omni has a question. Piper is a fallback only. |
| Wake word | Yes. Saying "Omni" should start a request. Push-to-talk and continuous stay as alternatives. |
| Barge-in | Yes. Talking over Omni stops it and starts a new request. Target the ChatGPT voice-mode feel. |
| Nested agents | Yes, both modes: a visible terminal Nick can watch, and background sub-agents Omni orchestrates and reports on. Omni picks the mode from the request; a setting fixes the default. |
| Email | Keep Outlook on the roadmap, via Microsoft Graph. Build it after the core loop is fast. |
| Cut for now | Training-data export and the reviewed/correct/incorrect task grading UI. They can return later as a separate tool over the episode log. |
| Context and cost | Bounded threads with compaction, per-turn token accounting, daily budget warnings. Memory files carry context between threads, not the transcript. |

## Target architecture

```text
  wake word / hotkey / bar / typed text            GTK app · CLI · bar plugin
                 │                                        ▲
                 ▼                                        │ Unix socket (JSON lines)
  ┌──────────────────────────────────── omnid (one long-running user service) ───┐
  │ mic pipeline: VAD → local Whisper → text                                       │
  │ session manager: one warm agent thread per adapter, compaction, budget         │
  │   ├─ Codex adapter: `codex app-server` child, JSON-RPC over stdio             │
  │   └─ Claude adapter: Claude Agent SDK (Python)                                │
  │ tools (Codex dynamic tools, in-process): desktop, windows, apps, terminal,    │
  │   research, memory, skills, reminders, say/ask, delegate                       │
  │ policy: allow by default, deny list → approval via voice + notification + app  │
  │ memory: USER.md + MEMORY.md, episodes.sqlite (FTS5), skills/*.md              │
  │ speech out: sentence-chunked streaming TTS (ElevenLabs), barge-in monitor       │
  └────────────────────────────────────────────────────────────────────────────────┘
```

**One process, warm all day.** `omnid` replaces `voice_bridge.py serve`. It owns the microphone, the agent child process, the tool server, memory, and speech. Clients (GTK app, `omni` CLI, bar plugin, hotkey scripts) talk to it over a Unix socket instead of dropping files in `$XDG_RUNTIME_DIR` and polling. Status for the bar is still a small JSON file written by `omnid` so the QML `FileView` watcher keeps working.

**The agent has tools; there is no plan schema.** Codex runs its native agent loop with its shell tool, the Omarchy skill, and our MCP tool server attached. It observes results and iterates. `omnid` intercepts approval requests from the app-server and applies our policy. This removes `KINDS`, `plan.schema.json`, `validate_action`, `describe`, `execute`, `local_plan`, and `normalize_research_plan` wholesale.

**Language: Python.** GTK 4, AT-SPI, the Claude Agent SDK, faster-whisper, openWakeWord, and Silero VAD are all Python. The Codex app-server is plain JSON-RPC over stdio, so a ~200-line client is enough; do not take a dependency on the TypeScript SDK. If the app-server protocol proves too unstable, the fallback is `codex exec resume <thread>` per turn, which still keeps one thread and prompt cache but loses streaming granularity.

**Layout.** Move the current tree to `legacy/` on day one so the old code is greppable but not importable, and tag it `v0-planner`. New package `omni/` with modules: `daemon`, `session`, `codex`, `tools/` (one module per tool family), `policy`, `memory`, `voice/` (`listen`, `speak`), `app/`, `cli`, `client`. `install.py`, the systemd unit, the plugin, and the launchers get renamed from `omi` to `omni`.

## Latency budget

Measured on the dev machine, voice request end to end:

| Stage | Today | Target | How |
| --- | --- | --- | --- |
| Transcript ready | Voxtype file + 0.4 s settle + 0.2 s poll | ≤ 0.8 s after end of speech | Own the mic; Silero VAD end-of-utterance; faster-whisper `small`/`base.en` on CPU, or `distil-large-v3` if the box has a GPU |
| First model token | 9–17 s cold `codex exec` | ≤ 1.5 s | Warm app-server thread, cached system prompt, no app catalog in the prompt (a tool lists apps on demand) |
| First spoken audio | after full answer + Piper | ≤ 2.5 s after end of speech | Stream agent text, cut at sentence boundaries, ElevenLabs streaming endpoint, pipe to `pw-play`/`mpv` |
| Simple desktop action done | 9–17 s | ≤ 3 s | Same warm thread; tools are direct `hyprctl`/`omarchy-*` calls |
| Football scores | 20–30 s (Chromium + two agent calls) | ≤ 5 s to first audio | `research.sports_scores` tool hits ESPN's public JSON scoreboard API with one HTTP GET; general questions use the agent's built-in web search |

Log every stage's timestamp per turn in the episode record so regressions are visible in the Activity page.

## Tool server (what Omni can do)

All tools live in one in-process MCP server so both adapters see the same surface. Keep each tool small and side-effect honest; the model composes them.

- **desktop**: `windows.list`, `windows.focus`, `windows.move`, `windows.place`, `workspace.switch`, `apps.list`, `apps.launch` (via `omarchy-launch-*` where one exists, else `gtk-launch`), `screen.read` (grim + tesseract), `desktop.controls`/`desktop.click`/`desktop.type` (AT-SPI, keep), `keys.send` (Hyprland `send_shortcut`, widened beyond the current 11-chord allowlist since it no longer needs approval).
- **terminal**: `terminal.open(cwd, command?, hold)` opens Foot and returns a job handle; `terminal.result(handle)` and `terminal.tail(handle)` read its output. The agent decides whether to wait. `shell.run` for headless commands is Codex's own shell tool, governed by policy.
- **browser**: keep the Playwright CLI adapter for the visible profile, expose `browser.open/read/click/fill/back`. Only used when Nick says "open", "show me", or "in the browser".
- **research**: `web.search` (Codex built-in search where available; Bing RSS fallback), `web.fetch(url)` returning readable text, `sports.scores(league, date)` via ESPN's public JSON.
- **memory**: `memory.save(kind, text, tags)`, `memory.update(id, text)`, `memory.forget(id)`, `memory.search(query)` over facts and episodes, `skills.save(name, markdown)`, `skills.list`. Details below.
- **reminders**: wrap the existing `reminders.py`: `reminder.add`, `reminder.list`, `reminder.cancel`.
- **voice**: `say(text)` for a deliberate short spoken status when Omni has something worth saying mid-task; `ask(question) → answer` for a spoken yes/no or short reply, used for clarifications and approvals.
- **delegate**: `delegate(task, mode, cwd, agent)`; see Nested agents.
- **omarchy**: expose `omarchy-*` discovery (`omarchy --help`, the shipped skill) rather than wrapping each command; the shell tool plus the skill already covers this.

## Policy: allow by default, ask for the deny list

`omnid` receives every approval request from the app-server (commands, file changes, MCP tool calls) and decides:

- **Allow silently**: everything not listed below. This includes launching apps, window and workspace changes, typing into fields, reading any file, running commands, browsing, `omarchy-theme-*`, `omarchy-notification-send`, git commands that don't push.
- **Ask first**: `rm`/`trash`/overwrites outside `~/Downloads`, `/tmp`, and the task's working directory; `sudo`, `pkexec`, `doas`; `omarchy-pkg-add/drop`, `pacman`, `yay`; `git push`, `git reset --hard`, `git clean`; sending mail or messages, purchases, posting on Nick's behalf; `systemctl` changes; anything the agent itself marks `destructive: true`.
- **Never**: modify `omnid`'s own service, the memory database, or credentials by shell; those go through tools.

Approval is spoken and visual at once: Omni says the one-line summary and asks; Nick answers by voice ("yes", "go ahead", "no") or clicks Approve/Cancel on the `notify-send` action buttons or in the app. A timeout of 30 s is a decline. The decision applies to that one request only. Keep the deny list in `~/.config/omni/policy.toml` so Nick can tune it without code.

## Voice pipeline

- **Wake word**: "Omni", detected by name in local Whisper transcripts of each utterance (prompted with the name so it is not heard as "on me"). No training needed; the request can follow the name in one breath. A trained openWakeWord model (Colab, about an hour; `docs/voice.md`) remains an option to cut CPU while others talk nearby, selected with `wake_model`. Picovoice Porcupine is the fallback if accuracy is poor; it needs a free personal access key.
- **Listening**: after wake word or hotkey, Silero VAD segments the utterance and Smart Turn decides at each short pause whether it is finished (`turn.py`, `smart_turn.py`). Moonshine transcribes while Nick talks (`stt.py`, `moonshine.py`); faster-whisper is the fallback. No files, no polling. Voxtype stays installed for text dictation only; the `omi-dictate` guard script goes away because the mic ownership is explicit.
- **Speaking**: ElevenLabs per-sentence HTTP streaming fed from the agent's text delta stream; audio piped straight to playback. Pocket TTS speaks when ElevenLabs fails, Piper while Pocket loads or if it is missing. Speech is on for question turns, off for action turns; enforce with the system prompt plus a guard that drops trailing acknowledgments like "Done." or "Opening that now." after a tool-only turn.
- **Barge-in**: while speaking, keep VAD on the mic. Loud sustained speech starts a capture; as soon as the streaming transcript calls Omni by name, playback stops and the turn is interrupted, and the finished utterance becomes the stop or the new request. PipeWire's echo-cancel module keeps Omni's own voice out of the mic (docs/voice.md).
- **Indicator**: keep the bar item and floating card. States: idle, listening, thinking, working, speaking, awaiting approval. The daemon writes `status.json`; nothing else does.

## The app

Nick's verdict on the current GTK app: it looks bad and works worse. It is one `Gtk.TextView` dumping "YOU / OMI" blocks, hand-written CSS with hard-coded navy hex values that ignore the Omarchy theme, seven sidebar buttons, and every list rebuilt from scratch on a 3–5 s timer. Rebuild it, do not restyle it.

- **Two surfaces, not one.** A small *quick ask* popover on `Super+H`: a single input, the streaming reply, the current listening/speaking state, and nothing else. It closes on Escape or when the reply is spoken. The *full window* is for browsing history, memory, skills, activity, and settings. Most days Nick should only see the popover.
- **Native and themed.** GTK 4 with libadwaita widgets (`AdwNavigationSplitView`, `AdwPreferencesPage`, `AdwActionRow`, toasts, status pages) so it looks like an Omarchy app rather than a custom skin. Read colors from Omarchy's current theme (`~/.config/omarchy/current/theme/`) and map them onto the libadwaita accent and background variables so Omni changes when the theme changes. No hard-coded palette anywhere.
- **Conversation view.** Real message rows (user, Omni, tool activity collapsed by default), streaming text that appears as tokens arrive, a spoken/typed indicator per turn, and inline approval cards when a request needs one. Threads listed by day, since threads now retire on a schedule.
- **Memory pages.** USER.md and MEMORY.md as editable documents with per-entry provenance shown on hover; skills as a list with an editor; episodes as a searchable timeline. Delete is one click with undo via toast.
- **Activity.** Per turn: request, timings for each latency stage, tokens, tools called, and any delegation still running with a Stop button. Today's token total and budget bar live at the top.
- **Settings.** Grouped preference pages: Agent (Codex/Claude, model, budget), Voice (wake word on/off, ElevenLabs voice, Piper fallback, speak-answers toggle), Policy (the deny list, editable), Email. A "Check setup" status page replaces the wall of text.
- **Live updates, not polling.** The app subscribes to `omnid` over the socket and updates rows in place. No `timeout_add` refreshers, no full list rebuilds.
- **Floating card and bar item.** Keep them in Quickshell, restyle to the same theme tokens, and make the card show the live transcript while listening and the current sentence while speaking.

Acceptance: the app passes a side-by-side with a stock Omarchy app in three themes without looking foreign; the popover appears within 150 ms of the hotkey; a streaming reply renders without visible frame drops.

## Memory: grow with Nick

Hermes-shaped, three stores, all under `~/.local/share/omni/`, all shown in the app.

1. **Semantic**: `memory/USER.md` (who Nick is, businesses, preferences, people) and `memory/MEMORY.md` (Omni's own notes on how to do things for Nick, what went wrong before). Omni edits these with `memory.save/update/forget`; each entry keeps a timestamp and the episode that produced it. Both files are injected into the system prompt, bounded to ~4 KB each; when they grow past that Omni is asked to consolidate.
2. **Episodic**: `episodes.sqlite` with one row per turn: request, final reply, tool calls summary, timings, tokens, thread id. FTS5 index. `memory.search` covers this so "what did we do about the printer last week" works. Add local embeddings only if FTS demonstrably misses; keep records readable and exportable either way.
3. **Procedural**: `skills/<name>/SKILL.md`. After a multi-step task succeeds, Omni is prompted (in the same turn, no extra call) to save a skill if the sequence is likely to recur. The skills directory is added to Codex's skills path and read by the Claude adapter so both agents see them. Nick can edit or delete any skill in the app.

Learning happens inline: the system prompt tells Omni to save durable facts and corrections as they come up, using the tools, during the turn. A separate reflection pass runs only when a thread is retired (see below), asking one cheap question: anything worth keeping that wasn't saved? This avoids a per-turn extra model call.

Migrate the existing `memory` and `knowledge` tables into `USER.md`/`MEMORY.md` entries and episodes on first run of the new daemon; keep the old database file untouched.

## Context and cost control

- **Threads are sessions, not history.** `omnid` keeps one active thread per adapter. It retires the thread and starts a fresh one when any of these hit: 45 minutes idle, a configurable token ceiling (default 150k), or a new calendar day. Memory files are what carry over, so a fresh thread still knows Nick.
- **Compaction on.** Use Codex's automatic compaction inside a thread; set the threshold below the model's limit so quality does not degrade near the edge.
- **Lean system prompt.** No app catalog, no window list, no knowledge dump. Tools return that data when asked, so the cached prefix stays stable and cheap across turns.
- **Accounting.** Record input/cached/output tokens per turn from the app-server events. Show today's total in the bar tooltip and Settings; a configurable daily budget triggers a spoken warning once and a visible banner.
- **Model choice.** Default to the smaller Codex model for voice turns and let Omni escalate by calling `delegate` for hard tasks; Settings keeps the manual override. Measure before locking this in: run the ten canonical requests below on each model and record latency, tokens, and success.

## Nested agents

`delegate(task, mode, cwd, agent)`:

- `mode: "visible"` opens a Foot terminal running `codex` (or `claude`) interactively in `cwd` with the task as the first prompt. Nick watches and can take over. Omni gets a handle and is told when the process exits; it does not read the transcript unless asked.
- `mode: "background"` starts a separate app-server thread (or Claude SDK session) with the task, headless, in `cwd`. Omni receives the final message and speaks a one-line summary or drops a report in `~/Documents/Omni Reports/<date>-<slug>.md` for long results. Background work is logged as its own episode.
- Default mode comes from the request ("in a terminal", "so I can watch" → visible) and a Settings default. Background delegations count against the same daily budget and show in Activity while running, with a Stop button.

## Email

Decided 2026-09-27 with Nick: a local-first IMAP stack rather than Microsoft Graph, for all three accounts (Microsoft 365 work first, Gmail, Yahoo). Mail syncs into `~/Mail` with mbsync, arrives by IMAP IDLE (goimapnotify), is indexed by notmuch, and is sent with msmtp. Graph push needs a public webhook, so on a laptop it would mean polling, and it covers only Outlook; it stays the fallback if IMAP is ever blocked. Nick is the tenant admin, so Microsoft 365 uses Omni's own Entra app with admin consent and the browser auth-code flow; Microsoft's 2025 defaults block device-code sign-in. Gmail and Yahoo use app passwords in the keyring. Omni may search, read, and draft freely; sending always asks with the exact recipients, subject, and body. Moving, archiving, and deleting are not tools yet. Nick reads mail through Omni plus notifications, or aerc. Setup and file layout: [docs/email.md](docs/email.md). Still to do: an intake that proposes reminders from new mail (from the notmuch post-new hook, not polling) and `calendar.upcoming`.

### Email triage, facts, and calendar (agreed 2026-09-27, step 1 done and live)

Layered like every serious mail product: cheap rules first, AI only for what is left. The known failure is important mail that looks like marketing (DMV, bank, inspection reminders come from the same bulk senders as ads), so bulk mail that mentions due dates, payments, expiry, appointments, or security is rescued instead of sent to the digest.

```
new mail ─▶ notmuch post-new ─▶ omnid mail intake (no polling)
  1 rules: per-sender corrections win · people Nick has written to → today · security → urgent · bulk → digest unless rescued
  2 AI sort (small model, no tools, only the unsure ~10–30%): urgent / today / digest / ignore + reason
  3 AI extract (mail with an action): what, when, where, amount + exact quote; code checks quote and date
```

Urgent: desktop notification, phone push, one short spoken line at the desk. Today: bar counter and an Inbox page. Digest: morning and evening. Proposals ("Car inspection due Oct 31. Add it?") become a calendar event with a reminder and a fact; repeat reminders update, not duplicate. Corrections ("always important", "never alert me") become per-sender rules. Memory: a `facts` table in `episodes.sqlite` (type, subject, value, due, valid until, source message) with a `facts_lookup` tool; vectors only if keyword recall visibly fails. Calendar: local `khal` (.ics), syncable later with `vdirsyncer`. Safety: the steps that read mail have no tools and return fixed-format data; code turns it into alerts and proposals; sending still always asks.

Build order: (1) rules, sender table, filtered notifications, digest; (2) AI sort and extract, proposals; (3) khal calendar and facts table; (4) phone push (ntfy.sh unless Nick picks Pushover) and spoken alerts; (5) one pass over the last ~60 days for anything still open.

Step 1 (done 2026-09-27): `omni/triage.py` (rules, keyword lists as module constants, `mail.sqlite` for sender rules and reasons), `mail.intake`/`mail.digest` ops, `omni mail intake|digest`, tools `mail_triage`, `mail_rule`, `mail_rules`, the versioned hook `integration/notmuch/post-new`, and `omni-mail-digest.timer` (07:30, 18:00). The rules add two steps to the agreed order: sign-in codes and reset links go to `today` before the security check, and replies in a thread Nick wrote in count as correspondents. Dry run over 14 days of real inbox mail: 520 digest, 28 unsure, 23 today, 23 urgent; over 90 days about 5% of mail notifies. Step 2 takes `tag:triage/unsure` (the `rule` column in `mail.sqlite` `decisions` says whether it was a stranger or a rescued bulk message). Details in docs/email.md.

## Phases and acceptance

Each phase ends with live checks on the Omarchy machine, recorded in `VALIDATION.md` with timings.

**Phase 0 — Groundwork (half a day).** Tag `v0-planner`, move current code to `legacy/`, scaffold `omni/`, `omnid` socket server, `omni` CLI that sends a text request and prints the streamed reply. Verify `codex app-server` starts under the installed CLI version and authenticates with the ChatGPT login. Done when `omni ask "what time is it"` streams a reply in under 2 s on a warm thread.

**Phase 1 — Agent core and desktop tools.** Codex adapter, MCP tool server with desktop/terminal/browser/research/reminders tools, policy engine with the deny list, and the new libadwaita app talking to the socket (quick-ask popover, Conversation, Activity, Settings first; see The app). Acceptance: "Go to workspace 3, open a terminal on the left and run pwd, and another on the right and run date" completes in under 6 s with no prompts; "What are the college football scores today" speaks a sourced answer in under 5 s; "Delete everything in my Downloads folder" asks first and cancel leaves the folder untouched.

**Phase 2 — Voice.** Mic pipeline, wake word, streaming ElevenLabs, barge-in, indicator states. Acceptance: "Omni, open Files" opens Files with no spoken reply; "Omni, what's the weather tomorrow" starts speaking within 2.5 s of Nick finishing; talking over the answer stops it within 300 ms and the new request is handled; dictation via Voxtype still types into the focused app and never reaches Omni.

**Phase 3 — Memory and skills.** Three stores, tools, inline saving, thread retirement with reflection, the Memory pages in the app (USER/MEMORY/skills/episodes with edit, delete, undo), migration from the old tables. Acceptance: tell Omni a fact in one thread, retire the thread, recall it in the next; complete a three-step task twice and see a skill appear after the first; "forget that" removes it and recall no longer returns it.

**Phase 4 — Delegation and Claude adapter.** `delegate` in both modes, Claude Agent SDK adapter sharing the same tool server and memory, adapter switch in Settings. Acceptance: "Fix the failing test in ~/Documents/cork-and-note in a terminal" opens a visible Codex session there; "in the background, summarize the last ten commits in that repo" produces a spoken summary and a report file; switching to Claude keeps memory and skills.

**Phase 5 — Email.** Graph sign-in, read/search/draft, approved send, hourly intake to proposed reminders.

## Verify on the machine before Phase 0 ends

- `codex --version` and `codex app-server --help` exist; note the protocol version the CLI speaks.
- ElevenLabs streaming endpoint works with Nick's voice ID and key from the keyring.
- `pw-record` from the Yealink SP92 at 16 kHz mono; whether echo cancellation is needed with TTS playing.
- openWakeWord installs and runs on this CPU without starving the desktop; measure CPU at idle.
- faster-whisper model size that keeps transcription under 1 s for a 5-second utterance on this hardware.

## Source notes

- Codex app-server protocol and persistent threads: <https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md>, <https://openai.com/index/unlocking-the-codex-harness/>
- Codex uses the ChatGPT subscription login across CLI, app, and app-server: <https://openai.com/index/introducing-the-codex-app/>
- Claude Agent SDK on a Claude plan (separate SDK credit since 2026-06-15, OAuth login default): <https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan>, <https://code.claude.com/docs/en/authentication>
- Omarchy agent skill and `omarchy-*` command discovery: <https://github.com/omacom/omarchy/blob/quattro/AGENTS.md>, <https://omarchy.org/manual/ai/>, <https://github.com/robzolkos/omarchy-skill>
- Hermes memory model (MEMORY.md/USER.md, FTS5 episodes, auto-generated skills): <https://hermes-agent.nousresearch.com/docs/user-guide/features/memory>
- openWakeWord: <https://github.com/dscripka/openWakeWord>; Silero VAD: <https://github.com/snakers4/silero-vad>; faster-whisper: <https://github.com/SYSTRAN/faster-whisper>
- ElevenLabs streaming TTS: <https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input>
