# Omni rebuild plan

Status: agreed direction, 2026-09-27. This document is the brief for the next implementation agent. Read it before touching code. The old design is described in [SPEC.md](SPEC.md) and [ROADMAP.md](ROADMAP.md); where they disagree with this file, this file wins.

## Why we are rebuilding the core

The current build treats the model as a one-shot JSON planner and a hand-written Python broker as the executor. Every request spawns a fresh `codex exec` or `claude -p`, sends a 6–10 KB prompt (full app catalog, window list, schema, memory), waits 9–17 s for a plan, runs the plan blind, and for questions spawns a *second* cold agent call to phrase the answer. Piper then synthesizes the whole reply before any audio plays. A football-score question launches a headless Chromium on top of that. That is the 20–30 s Nick experiences.

The same design is why Omni cannot grow: each new capability needs a schema entry, validator, describer, executor, and prompt paragraph; the model never sees a tool result and cannot adjust; memory is keyword search over verbatim sentences that only saves facts beginning with "I" or "My"; and none of Omarchy's ~145 `omarchy-*` commands or its shipped agent skill are used.

Keep: `hypr.py`, `reminders.py`, the bar/indicator plugin, the privacy rules (data outside the checkout, private permissions, public-file check), and the idea of asking before consequential actions. Replace: the planner/broker loop, the file-polling voice bridge, the memory model, and the per-request subprocess model.

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
  │ tool server (MCP, in-process): desktop, windows, apps, terminal, browser,      │
  │   research, memory, skills, reminders, say/ask, delegate                       │
  │ policy: allow by default, deny list → approval via voice + notification + app  │
  │ memory: USER.md + MEMORY.md, episodes.sqlite (FTS5), skills/*.md              │
  │ speech out: sentence-chunked streaming TTS (ElevenLabs), barge-in monitor       │
  └────────────────────────────────────────────────────────────────────────────────┘
```

**One process, warm all day.** `omnid` replaces `voice_bridge.py serve`. It owns the microphone, the agent child process, the tool server, memory, and speech. Clients (GTK app, `omni` CLI, bar plugin, hotkey scripts) talk to it over a Unix socket instead of dropping files in `$XDG_RUNTIME_DIR` and polling. Status for the bar is still a small JSON file written by `omnid` so the QML `FileView` watcher keeps working.

**The agent has tools; there is no plan schema.** Codex runs its native agent loop with its shell tool, the Omarchy skill, and our MCP tool server attached. It observes results and iterates. `omnid` intercepts approval requests from the app-server and applies our policy. This removes `KINDS`, `plan.schema.json`, `validate_action`, `describe`, `execute`, `local_plan`, and `normalize_research_plan` wholesale.

**Language: Python.** GTK 4, AT-SPI, the Claude Agent SDK, faster-whisper, openWakeWord, and Silero VAD are all Python. The Codex app-server is plain JSON-RPC over stdio, so a ~200-line client is enough; do not take a dependency on the TypeScript SDK. If the app-server protocol proves too unstable, the fallback is `codex exec resume <thread>` per turn, which still keeps one thread and prompt cache but loses streaming granularity.

**Layout.** Move the current tree to `legacy/` on day one so the old code is greppable but not importable, and tag it `v0-planner`. New package `omni/` with modules: `daemon`, `session` (adapters), `tools/` (one module per tool family), `policy`, `memory`, `voice/` (`wake`, `listen`, `speak`), `clients/` (`app`, `cli`, `socket`). `install.py`, the systemd unit, the plugin, and the launchers get renamed from `omi` to `omni`.

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

- **Wake word**: openWakeWord running on the always-on mic stream (CPU, a few percent). Train a custom "Omni" model with their Colab notebook (takes about an hour; document the steps in `docs/wake-word.md`). Until it exists, ship with a built-in model such as "hey Jarvis" so the loop is testable. Picovoice Porcupine is the fallback if openWakeWord's accuracy is poor for this microphone; note it needs a free personal access key.
- **Listening**: after wake word or hotkey, Silero VAD segments the utterance, 1.0 s trailing silence ends it. faster-whisper transcribes locally. No files, no polling. Voxtype stays installed for text dictation only; the `omi-dictate` guard script goes away because the mic ownership is explicit.
- **Speaking**: ElevenLabs streaming input (WebSocket) fed sentence by sentence from the agent's text delta stream; audio piped straight to playback. Piper remains for offline or when the key fails. Speech is on for question turns, off for action turns; enforce with the system prompt plus a guard that drops trailing acknowledgments like "Done." or "Opening that now." after a tool-only turn.
- **Barge-in**: while speaking, keep VAD on the mic. Speech detected → stop playback, send `turn/interrupt` to the app-server, start listening. Enable PipeWire's echo-cancel module (`libpipewire-module-echo-cancel`) so Omni's own voice does not trigger VAD; verify with the Yealink SP92, which has hardware echo cancellation and may make this unnecessary.
- **Indicator**: keep the bar item and floating card. States: idle, listening, thinking, working, speaking, awaiting approval. The daemon writes `status.json`; nothing else does.

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

## Email (later phase)

Microsoft Graph with delegated device-code sign-in, least-privilege `Mail.Read`, `Mail.Send` (send only), `Calendars.Read`. Tools: `mail.search`, `mail.read`, `mail.draft`, `mail.send` (deny-list, always asks with the exact recipient/subject/body), `calendar.upcoming`. Delta queries for an hourly intake that proposes reminders. The current Outlook-web Playwright adapter is deleted; it was an interim hack.

## Phases and acceptance

Each phase ends with live checks on the Omarchy machine, recorded in `VALIDATION.md` with timings.

**Phase 0 — Groundwork (half a day).** Tag `v0-planner`, move current code to `legacy/`, scaffold `omni/`, `omnid` socket server, `omni` CLI that sends a text request and prints the streamed reply. Verify `codex app-server` starts under the installed CLI version and authenticates with the ChatGPT login. Done when `omni ask "what time is it"` streams a reply in under 2 s on a warm thread.

**Phase 1 — Agent core and desktop tools.** Codex adapter, MCP tool server with desktop/terminal/browser/research/reminders tools, policy engine with the deny list, GTK app talking to the socket (Conversation, Activity, Settings pages first). Acceptance: "Go to workspace 3, open a terminal on the left and run pwd, and another on the right and run date" completes in under 6 s with no prompts; "What are the college football scores today" speaks a sourced answer in under 5 s; "Delete everything in my Downloads folder" asks first and cancel leaves the folder untouched.

**Phase 2 — Voice.** Mic pipeline, wake word, streaming ElevenLabs, barge-in, indicator states. Acceptance: "Omni, open Files" opens Files with no spoken reply; "Omni, what's the weather tomorrow" starts speaking within 2.5 s of Nick finishing; talking over the answer stops it within 300 ms and the new request is handled; dictation via Voxtype still types into the focused app and never reaches Omni.

**Phase 3 — Memory and skills.** Three stores, tools, inline saving, thread retirement with reflection, app pages for USER/MEMORY/skills/episodes with edit and delete, migration from the old tables. Acceptance: tell Omni a fact in one thread, retire the thread, recall it in the next; complete a three-step task twice and see a skill appear after the first; "forget that" removes it and recall no longer returns it.

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
