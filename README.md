# Omni

A personal assistant for Omarchy Linux that acts on your desktop. Ask by voice or text, and Omni opens and arranges windows, launches apps, types, runs commands, looks things up, remembers what matters, and hands big jobs to coding agents. It runs on your existing Codex (ChatGPT) sign-in.

**Status: rebuild in progress.** The design and phases are in [PLAN.md](PLAN.md), which also tracks what is built and what is left. The previous planner/broker build is in [`legacy/`](legacy/) (tag `v0-planner`).

## How it works

`omnid` is one long-running user service. It keeps a warm Codex `app-server` thread, so a request streams its first words in about a second instead of spawning a fresh agent every time. Codex calls Omni's tools (windows, apps, terminals, typing, screen text, sports scores, web pages, reminders, memory, skills, delegation) and its own shell. Omni's policy lets ordinary work run without prompting and asks only before consequential commands: deleting outside safe folders, sudo, packages, `git push`, and system services.

Voice runs locally. A wake word or hotkey starts listening, Silero VAD finds the end of the utterance, and faster-whisper transcribes it. Answers stream to ElevenLabs sentence by sentence. Talking over Omni stops it. After actions Omni stays quiet unless it has a question.

Memory grows by itself. Omni saves facts about you to `USER.md` and its own lessons to `MEMORY.md`, logs every turn to a searchable episode database, and writes reusable skills after multi-step tasks that worked. Everything is plain files under `~/.local/share/omni/`, viewable and editable in the app.

## Install (Omarchy)

```bash
git clone https://github.com/nhorto/Omni.git ~/Documents/Omni
cd ~/Documents/Omni
sudo pacman -S --needed python-gobject gtk4 libadwaita mpv wtype grim tesseract tesseract-data-eng foot libnotify libsecret
python3 install.py            # preview
python3 install.py --apply    # venv, launchers, services, bar plugin, Hyprland file
```

Then follow the printed steps: add `require("hypr.omni")` to `~/.config/hypr/hyprland.lua`, enable `omnid.service` and `omni-app.service`, store the ElevenLabs key with `omni key elevenlabs`, and run `omni doctor`.

| Keys | What |
| --- | --- |
| Super+H | Quick ask popover |
| Super+Shift+H | Talk (press again to send early) |
| Super+Alt+H | Hands-free listening on/off |
| Super+Ctrl+H | Stop speaking and interrupt |

The wake word is "Omni": say "Omni, open Files" in one breath, or "Omni" and then the request. See [docs/voice.md](docs/voice.md).

## From a terminal

```bash
omni ask "put a terminal on the left running htop and files on the right"
omni ask --speak "how did Tennessee do yesterday"
omni status · omni doctor · omni new · omni stop · omni memory · omni memory search printer
```

## Privacy

Personal data lives outside the checkout, with private permissions: memory and episodes in `~/.local/share/omni/`, settings and `policy.toml` in `~/.config/omni/`, and the ElevenLabs key in the Secret Service keyring. Audio stays local; only transcripts go to the agent. Omni cannot change its own memory database, service, or credentials through the shell. `scripts/check_public.py` blocks accidental commits of private files.

## Development

```bash
python3 -m unittest discover -s tests
python3 scripts/check_public.py
OMNI_DATA=/tmp/o/data OMNI_CONFIG=/tmp/o/config OMNI_RUNTIME=/tmp/o/run python3 -m omni.daemon -v   # isolated daemon
```

See [AGENTS.md](AGENTS.md) for the code map and conventions.
