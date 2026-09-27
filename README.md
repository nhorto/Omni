# Omni

A personal desktop assistant for Omarchy Linux: describe what you want in text or speech, let your signed-in agent plan it, and review consequential actions before they run.

**A core rebuild is planned; read [PLAN.md](PLAN.md) first.** The notes below describe the current planner/broker build, which the plan replaces.

**Active development, not a completed release.** The assistant is currently called **Omi** in the app and commands. The public project is **Omni**. See [the roadmap](ROADMAP.md) for the full vision and remaining acceptance checks.

## What works today

- Native GTK app with conversations, editable local memory, searchable knowledge notes, activity review, and settings.
- Codex or Claude CLI adapters using their existing sign-in. Exact reminders, memories, workspace changes, URLs, and folder listings can run locally without an agent call; model overrides are available. Activity shows Codex token usage when its CLI reports it.
- Workspace switching, exact existing-window focus, approved navigation shortcuts, window moves and placement, app/site launches, and visible terminals. A small set of harmless terminal commands runs directly; other commands require review.
- Separate Chromium profile for browser reading, clicks, and form input, plus accessible desktop controls and named text fields via AT-SPI. Local screen OCR can read visible text; it does not store the captured image or raw OCR in Activity.
- Voxtype transcript intake, push-to-talk and continuous mode, a bar indicator and movable recording card. The indicator distinguishes Omi listening from text dictation. The Yealink SP92 was tested live on this development machine; other microphone setups still need validation.
- Spoken answers to questions via local Piper or a configured ElevenLabs voice. Action requests stay quiet.
- Public information questions research in the background and answer aloud without changing desktop windows. College football score questions read a headless ESPN scoreboard; other public searches use bounded search results. Say “open” or “show me the page” when you want a visible browser.
- Persistent local reminders with quiet hours, retry handling, a review queue, and an app page.
- Local action history with corrections, planning errors, cancelled voice reviews, terminal outcomes, and observed before/after desktop state. Only tasks reviewed as correct enter the candidate training export.
- Outlook draft preparation and guarded web send adapter. Live account validation and durable mail/calendar sync are unfinished.

Desktop plans stop at a failed or declined step. Visible terminal commands start in a pending state; the background service records their exit code and up to 4,000 trailing output characters when they finish, including after a restart. A running command is not eligible for reviewed training export. General visual computer use, notification triage beyond local reminders, and Outlook monitoring are under development.

## Setup

The desktop target is Omarchy with Hyprland's Lua dispatcher interface. Other Hyprland versions are not yet supported. Required components include Python 3.10+, PyGObject with GTK 4 and AT-SPI bindings, Foot, and a signed-in Codex or Claude CLI. Optional features need Voxtype/PipeWire, Piper plus `pw-play`, Playwright CLI plus Chromium, or Secret Service's `secret-tool` for ElevenLabs.

```bash
git clone https://github.com/nhorto/Omni.git
cd Omni
./launch.sh
```

Preview or install user launchers, desktop entry, service, and Omarchy plugin:

```bash
python3 install.py
python3 install.py --apply
```

The installer backs up replaced integration files. It does not install packages or change shortcuts. Follow [integration setup](integration/README.md) to enable the service and recording indicators. Keep the checkout in place; installed launchers refer to it.

Suggested shortcuts are **Super+H** for the app, **Super+Shift+H** for one voice utterance, and **Super+Alt+H** for continuous listening. On Omarchy, **Super+Ctrl+X** is Voxtype dictation into the focused text field. Dictation does not send a request to Omi. The bar label and floating card distinguish the two recording modes; click the bar item for an action menu with voice, dictation, app, and settings controls. Check existing bindings before assigning them.

Omi sends a spoken request automatically when recording finishes. Typed requests in the app use Send. Harmless desktop actions do not require confirmation; consequential actions open a review window. Settings offers automatic model choice or an explicit Codex model. Automatic choice currently uses GPT-6 Sol for requests needing an agent; exact local commands bypass the agent.

A question such as “What are the college football scores today?” reads public information in the background and speaks a brief sourced answer. “Open the scores page in the browser” is a desktop action and opens the page. Search snippets can be stale; Omi should say when they do not establish an answer. Background research uses public web sites and still sends the retrieved text to the selected agent for a spoken summary.

## Try it without a microphone

Type in the app, or use the same transcript queue as voice after enabling the service:

```bash
python3 voice_bridge.py inject "Open the file manager"
python3 voice_bridge.py status
python3 assistant.py ask "Go to workspace 3"
python3 assistant.py ask "Remember that I prefer concise replies"
python3 assistant.py doctor
```

For a desktop workflow, try: “Go to workspace 3, open a terminal on the left and run pwd, then open another terminal on the right and run date.” Commands outside the small read-only allowlist require approval. New-window placement stops if the target cannot be identified uniquely.

Use Settings to check setup and choose an available agent and model. Conversation threads provide bounded follow-up context; desktop commands go into Activity. Add project context under Knowledge, and edit background and tone under Profile.

## Reminders

Say or type “Remind me in 15 minutes to take a break.” This exact form uses no model call. The Reminders page also schedules and cancels local reminders. Settings can hold notifications during quiet hours. The background service catches up after restart and retries transient delivery failures; reminders still failing after five attempts remain visible as failed.

Suggestions from future intake adapters remain proposed until reviewed. No email monitoring or automatic calendar writes are active. Delivery is durable but not exactly once: a crash after the notification daemon accepts a message and before SQLite records it can result in a retry. These reminders currently use desktop notifications, not spoken interruptions.

## Privacy and public source

The repository contains code, generic examples, and synthetic tests. Personal data lives outside the checkout:

| Data | Local location |
| --- | --- |
| Memories, conversations, action history, knowledge | `~/.local/share/omi/memory.sqlite3` |
| Settings, profile, personality | `~/.config/omi/` |
| Browser sign-in and output | `~/.local/share/omi/browser-profile/` and `browser-output/` |
| Temporary recordings/transcripts and state | `$XDG_RUNTIME_DIR/omi/` |
| ElevenLabs API key | Local Secret Service keyring |

Local storage does not mean local inference: the selected agent receives the request and relevant context. ElevenLabs receives spoken text when selected. Exports can contain personal data; do not commit them. The repository ignore rules and [public-file check](scripts/check_public.py) help catch accidental additions, but are not a guarantee of redaction.

Use Activity to mark a task correct or record a correction. Full history retains failures; reviewed training export excludes them. Versioned exports now include observed window state before and after desktop tasks. This produces a candidate dataset, not a trained local model.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check_public.py
git diff --check
```

Tests use temporary data and synthetic fixtures. Live desktop checks require a graphical session and are kept separate. See [SPEC.md](SPEC.md), [ROADMAP.md](ROADMAP.md), and [CONTRIBUTING.md](CONTRIBUTING.md).
