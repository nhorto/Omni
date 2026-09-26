# Personal Assistant for Omarchy — product and technical spec

Status: working product specification, 2026-09-26. Repository: **Omni**. Assistant name and existing runtime identifiers: **Omi**.

## Implementation status

The installed build has a native GTK control app with conversation threads, memory, longer searchable knowledge notes, activity review/export, profile, and agent/model settings. `Super+H` opens it; `Super+Shift+H` records one Omi utterance; `Super+Alt+H` toggles continuous listening. The Omarchy bar and movable floating card show recording/work state. Voxtype transcription feeds the same request pipeline as typed text; simulated transcripts have opened the file manager. Piper speech is installed and a simulated question completed playback. Codex planning, bounded action approval, local memory, and task history work. Simple exact commands can run without an agent call. Omi can switch workspaces, move windows, position windows on a monitor, and open visible terminals to run approved commands; these operations passed live checks. Omi's separate Playwright browser has passed a live approved click; an AT-SPI adapter has activated an accessible Files control. Outlook draft and guarded send logic passed simulated tests. ElevenLabs voice ID/key configuration and TTS API adapter are implemented but need the user's voice/account for a live test. Claude is selectable when its CLI has available subscription access. Live microphone capture, Outlook sign-in/monitoring, calendar access, notification triage, and general visual computer use are pending.

## Goal

Build a persistent, personalized digital assistant for this Omarchy computer. Omi must act across the desktop, files, apps, browser, mail, and calendar; converse naturally about questions and the user's work; and learn useful facts, relationships, events, and workflows over time. Voice is the main input once hardware is connected, while the native app is the place to converse, review activity and memories, personalize Omi, and manage integrations. The current build is a foundation for this product, not the completed product.

## Decisions already made

| Area | Decision |
| --- | --- |
| Agent access | Use the user's existing signed-in agent CLIs and allow a choice between Codex and Claude. No separate API key is required for the first version. |
| Action approval | Ask before consequential actions such as deleting or moving important files, installing software, changing system settings, sending information, or spending money. |
| Memory location | Keep assistant-owned memory in local files and a local database by default. |
| Name | Omi. |
| Recording indicator | Both a movable floating widget and an Omarchy bar indicator. |
| Spoken replies | Speak conversational answers to questions. Complete action requests quietly, with status and an action log. |
| Automatic memory | Save clear, non-sensitive user-stated facts automatically. Explicit “remember this” always works. Keep provenance and let the user edit/delete. |
| Outlook monitoring | After sign-in, check mail and calendar changes periodically; propose reminders/calendar entries for review. |
| Connected account | Outlook is the first mail and calendar provider. Account sign-in and consent are still pending. |
| Model use | Keep simple actions deterministic. Use an appropriate subscription model for interpretation; expose provider and model overrides and show a per-call history. A stronger model is not the default for every request. |
| Task dataset | Save desktop requests, proposed plans, executed steps, results, and corrections locally. Training export includes only completed actions the user marked correct. |
| Target platform | Omarchy with Hyprland/Wayland and Lua dispatchers. Agent authentication is managed locally by each CLI. |

## User experience

1. Press a configurable hotkey to open the assistant. Initially, type a request. With a microphone, press to speak or toggle a listening session.
2. A bar indicator shows `idle`, `recording`, `transcribing`, `thinking`, `awaiting approval`, and `speaking`; a movable floating card appears while active. Its stop control ends recording and continuous listening; the hotkey also stops a single utterance.
3. In a listening session, speech is split into utterances. Each utterance is transcribed, handled, and acknowledged. The microphone can stay enabled between utterances until explicitly stopped. The indicator must remain visible whenever audio capture is active.
4. The assistant answers questions conversationally in text and, once the voice is configured, aloud. Action requests such as opening an app or moving a file do not trigger spoken acknowledgements. Voice, speaking rate, manner, and personality are user settings.
5. A visible agent selector chooses Codex or Claude. Agent changes do not split personal memory. If an agent is unavailable or logged out, the UI explains why and offers the available agent.
6. Before a consequential action, show the proposed action and affected targets. The native app presents `Approve once` and `Cancel`; CLI requests use a terminal prompt. Editing a proposed action is a future UI improvement. Approval applies only to that action, not to future actions.
7. Show a concise action log and outcome. Add undo where the underlying operation supports it. App and URL launch requests are reported as requests until success can be verified.

### Interaction lanes and context budget

| Lane | Example | Context and output |
| --- | --- | --- |
| Desktop task | “On workspace 3 open a browser and terminal, then place them” | One bounded plan of up to 12 sequential actions and a task trace. It does not enter a conversation thread. Routine exact requests can skip an agent call. No spoken acknowledgement. |
| Conversation | “What should I do next?”, “Explain this” | Belongs to a selected thread. Only the most recent five turns from that thread, a bounded profile, and retrieved relevant memories are sent to the chosen agent. Answer in text and speech. |
| Background intake | New Outlook message or calendar change | Deterministic incremental fetch and cheap metadata rules first. Store the event and cursor locally; ask an agent only for ambiguous or potentially useful items. Surface a proposed reminder or calendar change for review. |
| Explicit memory | “Omi, remember…” | Save locally with source and timestamp; no conversation window needs to stay open. |

The native app lets the user start a new conversation. Switching Codex and Claude keeps Omi's local memory while conversation threads stay separate. Commands are recorded in Activity, including the request, plan, step results, and feedback, so they can be audited and later used to build a local action model.

### Memory and learning model

1. **Profile and personality:** Human-editable Markdown for stable background, businesses, projects, communication preferences, and Omi's tone. These are bounded before injection into an agent call.
2. **Facts and relationships:** Structured local records with content, source, creation/update time, and eventually entity links and validity. The current build has SQLite FTS5 search and allows automatic save only for verbatim, non-sensitive user-stated facts; it does not yet extract facts from mail or infer relationships. Longer user-added knowledge notes have their own full-text index, and only matching excerpts enter a request.
3. **Episodes:** Immutable source events such as a task attempt, email, meeting, or notification with provenance and timestamp. Action task traces are implemented. Outlook/calendar intake is pending sign-in.
4. **Procedures:** Reviewable reusable workflows or skill files learned from successful task traces. None should be silently generated and executed from an unreviewed failure.
5. **Retrieval:** Use exact/FTS search and bounded summaries first. Add local embeddings and a vector index when realistic recall tests show misses; keep the underlying records readable and exportable. A vector index is a retrieval aid, not the source of truth.

The assistant must preserve corrections and provenance. User feedback marks task traces `correct` or `incorrect`; an incorrect task remains in full history but is excluded from the training export. The current reviewed export is a dataset candidate, not a model training pipeline. Before fine-tuning, add privacy filtering, deduplication, schema versioning, outcome verification, and a held-out evaluation set.

### Outlook, calendar, and notifications

The target connector is delegated Microsoft Graph access for the user's Outlook account. Begin with least-privilege mail and calendar read permissions; request write/send permissions only for features the user enables. After sign-in, an hourly or user-configured job should use Graph delta cursors for Inbox messages and a bounded calendar view, so unchanged items are not reread. The local intake ledger should store IDs, timestamps, selected metadata, classification decisions, and pointers to source items; full message bodies should be fetched only when the task requires them. A simple rule pass can reject bulk mail and duplicates before agent review. A proposed reminder or calendar entry must be shown in the app for approval, and sending email remains an explicit per-message approval.

Omi should distinguish a notification from a durable fact: an inspection due email may create a proposed reminder, while a customer exchange may update a relationship or project episode after source-aware extraction. It should report uncertain conclusions as suggestions. Native desktop notifications are a separate source to integrate after mail/calendar intake works. No mailbox or calendar monitor is active in the current build.

### Voice and personal presence

Local Voxtype transcription stays the input adapter. Questions get spoken answers; desktop commands remain quiet. The user can select a local Piper voice now. An ElevenLabs output adapter accepts a voice the user creates in ElevenLabs Voice Design, plus their voice ID and an API key stored in the local keyring; a live account test remains. Local Piper stays available. The control app lets the user test the selected voice; richer style controls and a choice to show sensitive answers as text remain to be added. The bar and movable widget remain visible whenever recording is active.

### Model routing and cost

`auto` should use deterministic local handlers for unambiguous commands and the selected subscription agent's ordinary model for ambiguous actions or conversations. A stronger model can be selected manually or routed to for a measured class of complex tasks after a benchmark exists. Each agent call is logged with provider, model, purpose, and elapsed time; token/cost accounting can be added when both CLIs expose reliable values. The selected agent proposes bounded structured actions; the local broker executes them after validation and approval. No agent receives direct unrestricted command execution as part of Omi's normal action path.

## Functional scope

### First working version

- Text command panel and CLI, usable with no microphone.
- Agent selector for Codex and Claude, with login/availability checks.
- Natural-language requests for common actions: open an app or URL, locate files, move/copy files, run a command, change a desktop setting through a reviewed action, and answer questions about local context.
- Local action broker that receives *proposed* structured actions from an agent, validates them, applies approval policy, executes, and records results. The agent must not execute unrestricted tool calls behind the broker's back.
- Memory: explicit `remember`, `forget`, and recall, plus timestamped action history. Store source and last update; make every memory viewable, editable, exportable, and deletable. The native app has profile and memory editors.
- Floating status/control panel plus Omarchy bar indicator.

### Voice-ready milestone

- Local speech-to-text, initially Voxtype, connected to the same text request pipeline. Keep ordinary dictation into an app separate from assistant commands.
- Hotkey for one utterance and toggle for an extended listening session.
- Recording/transcribing indicator and immediate stop control. Test with the new microphone and actual audio hardware.
- Text-to-speech for conversational answers, with selectable local Piper model and speaking rate. Action acknowledgements stay silent. A future privacy control should distinguish ordinary from sensitive answers.

### Broader computer control

- Browser actions using Playwright in a separate visible Chromium profile, including reading pages, clicking, typing, and checking outcomes. The initial open/read/click path works; more complex workflows and sensitive action classification remain to be refined. Ask before purchases, posting, sending messages, or sharing personal data.
- GUI control for apps that lack a suitable CLI or API. Prefer an app's supported interface where available; keyboard/mouse automation is a fallback.
- Email: create drafts locally, open a prefilled Outlook compose view, and require exact-message approval immediately before sending. Live inbox and send checks follow account sign-in.
- Reusable personal workflows and skills, with user-editable instructions. Add MCP servers only when they offer a real capability that the local broker or agent cannot provide cleanly.
- Semantic memory retrieval with local embeddings/vector index after recall evaluations show where FTS misses. The underlying memory records stay portable and readable.

## Architecture

```text
hotkey / text panel / speech-to-text
              ↓
     request and session manager  ←→  status indicator
              ↓
     selected agent adapter
       (Codex or Claude)
              ↓ structured proposals
      local action broker  ←→  approval UI
              ↓
   OS, apps, files, browser tools
              ↓
      verified result / reply
              ↕
      local memory + audit log
```

- **Agent adapters:** Codex's noninteractive CLI and Claude Code's print mode both support scripted calls and structured output. Their subscription authentication remains managed by their own CLIs. Avoid `bare` mode for Claude when using subscription login, because it does not read OAuth credentials. Use bounded, inspectable responses for planning and broker execution for actions. The app exposes provider and model overrides; `auto` uses Codex Sol or the Claude CLI default after local command routing.
- **Action broker:** Each action has a typed schema, input validation, risk classification, execution method, verification step, and optional undo. A shell command proposal is displayed verbatim before running when it changes state or cannot be classified safely. No blanket permission bypass to an agent.
- **Memory:** SQLite for durable records with an FTS5 full-text index; Markdown for the human-editable profile and personality. Automatic memory currently accepts only non-sensitive snippets that appear verbatim in the user's request; it does not treat an agent's guess as fact. A vector index remains a future retrieval layer.
- **Personality:** `~/.config/omi/personality.md` provides a user-editable response style shared by both agent adapters. Voice selection will be a separate setting.
- **Voice input:** Voxtype supports local transcription, per-recording file output, state reporting, hotkey toggle, and an on-screen display. An assistant-specific hotkey writes transcriptions to a private runtime file/queue, while ordinary dictation retains its normal type-at-cursor behavior. The input adapter must avoid reprocessing the same utterance.
- **Status UI:** The assistant owns the end-to-end state. Voxtype's recording state supplies the mic state; the assistant adds agent/approval/speech state. The indicator can move or have configurable bar placement depending on the chosen design.
- **Data paths:** Code in `~/Documents/omi/`; private data under `~/.local/share/omi/`; configuration under `~/.config/omi/`; temporary audio/transcripts under `$XDG_RUNTIME_DIR/omi/`. Private files use owner-only permissions.

## Action policy

| Class | Examples | Behavior |
| --- | --- | --- |
| Read-only | Answer a question, list files, inspect an app state | Run and report. |
| Low impact and reversible | Open app/URL, switch workspace, create a note | Run and report; offer undo where useful. |
| Consequential | Delete/overwrite/move important files, install software, change system settings, run an opaque shell command, send data, spend money | Show exact plan and require approval before execution. |
| Elevated privilege | `sudo`/`pkexec` or credential access | Explicit per-action approval and normal OS authentication. |

The user can later tune rules, but a remembered approval never silently turns into global authority. All agent output and web content are untrusted input to the broker. The broker logs proposed and executed actions separately.

## Memory policy

- Add a readable `profile.md` editor for name, preferences, relationships, and recurring work; keep factual memories in SQLite with timestamps and provenance. The current personality Markdown is user editable.
- Support `remember`, `forget`, correction, export, and complete local deletion.
- Save clear, non-sensitive facts stated by the user automatically, with a setting to turn this off. Never auto-save sensitive facts, guesses, or facts from external content. Suggestions drawn from mail/calendar or uncertain inference need review.
- Keep local conversation threads separate from factual memory and desktop task history. The last five turns from the active thread provide follow-up context; each turn can be listed, exported, or removed. Switching Codex ↔ Claude draws on the same relevant local memories without merging separate threads.
- Local storage does not mean offline inference: the selected subscription agent may send request context to its provider. Show what memory is being shared with the agent for a request and allow the user to restrict categories.

## Acceptance checks

1. With no mic, type “Open the file manager”; it opens and the assistant reports the result.
2. Type “Remember that I prefer concise replies,” restart the app, switch agents, and recall that preference.
3. Type a file deletion or system setting change; the affected item and exact action appear in an approval prompt. Cancel leaves the system untouched.
4. Pick Claude while logged out; the UI reports that status and remains usable with Codex.
5. Once a mic is connected, start/stop an utterance by hotkey; the visible indicator matches actual audio capture and the transcript enters the same request path.
6. In extended listening mode, multiple utterances execute in sequence and the stop control ends capture immediately.
7. Export memories and delete one; subsequent recall must not return the deleted item.

## Remaining delivery sequence

1. Test real microphone capture and continuous listening, tune silence detection, and verify the recording indicator against actual hardware.
2. Sign in to the chosen Outlook account in Omi's browser, check mailbox type, and test read/draft/send with exact-message approval. Design delegated Graph connection for durable mail and calendar sync; the browser flow is only an interim adapter.
3. Implement Graph delta intake, local event ledger, incremental triage, and reviewable reminder/calendar suggestions. Add a daily briefing that uses only changed or relevant records.
4. After the user designs an ElevenLabs voice, enter its voice ID/API key and perform a live output test; tune quality, cost, and sensitive-answer behavior. The adapter and Piper fallback are already in place.
5. Expand visual computer use to apps without accessible controls, deeper window manipulation, outcome checks, undo, and task repair. Workspace switching, approved window moves, edge/corner/center placement, and a visible terminal command action are available. Evaluate these flows before exporting them as training examples.
6. Add richer typed/linked memory, retrieval evaluation, and optional local vector search. Build a separate local action-model training experiment from reviewed task traces.

## Open experience choices

- Preferred voice qualities and personality examples. These can be tuned after the interaction loop works.

## Source notes

- [OpenAI Codex CLI reference](https://developers.openai.com/codex/cli/reference/) documents `codex exec` and JSONL output.
- [Claude Code programmatic use](https://code.claude.com/docs/en/headless) documents `claude -p`, structured output, and the `--bare` authentication limitation.
- [Voxtype README](https://github.com/peteonrails/voxtype) and [configuration](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md) document file output, recording state, toggle operation, and the display.
- [Playwright CLI configuration](https://playwright.dev/agent-cli/configuration) documents headed sessions, persistent profiles, and browser executable selection.
- [Piper voice catalog](https://huggingface.co/rhasspy/piper-voices) supplies the local speech models.
- [Microsoft Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0) is a possible future API-backed Outlook adapter; the current first version uses an approved Outlook web compose flow.
- [Microsoft Graph message delta](https://learn.microsoft.com/en-us/graph/delta-query-messages) and [calendar event delta](https://learn.microsoft.com/en-us/graph/delta-query-events) support incremental intake. [Delegated access](https://learn.microsoft.com/en-us/graph/auth-v2-user) requires account sign-in and consent.
- [Hyprland's Lua dispatchers](https://wiki.hypr.land/Configuring/Basics/Dispatchers/) define workspace focus and exact window moves on this Omarchy version.
- [ElevenLabs Voice Design](https://elevenlabs.io/docs/eleven-creative/voices/voice-design) creates a custom voice; [text to speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert) uses its voice ID for synthesis.
- [Hermes memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory), [sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions), and [skills](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/work-with-skills.md) informed the separation between facts, episodic history, and procedures. Omi uses its own local broker and storage.
