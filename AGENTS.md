# Working on Omni

Read [PLAN.md](PLAN.md) first. Its "Build status" section says what is done, what was verified where, and what to do next. Update that section when you finish something; do not start a separate status file.

## Code map

| Path | What it owns |
| --- | --- |
| `omni/daemon.py` | `omnid`: Unix-socket JSON-lines API (`OPS`), event fan-out, `status.json` for the bar, approvals and questions (app + notification + voice at once), reminder delivery |
| `omni/session.py` | Warm Codex threads. `Runner` drives one thread (turn events, tool calls, approval requests). `Session` owns the foreground runner, retirement plus reflection, episodes, the token budget, and delegation |
| `omni/codex.py` | asyncio JSON-RPC client for `codex app-server` |
| `omni/policy.py` | What needs approval. Pure functions over the command string and cwd; tested in `tests/test_policy.py` |
| `omni/memory.py` | `USER.md`/`MEMORY.md` entries, `episodes.sqlite` (FTS5), `skills/*/SKILL.md`, and legacy migration |
| `omni/tools/` | Every tool the agent can call. `@tool(...)` registers name, JSON schema, `action` (keep quiet afterwards), `ask` (always approve) |
| `omni/voice/speak.py` | Sentence chunking, ElevenLabs streaming with Piper fallback, and `SpeechGate` (speak answers, stay quiet after actions) |
| `omni/voice/listen.py` | Mic loop: wake word, push-to-talk, hands-free, barge-in, faster-whisper |
| `omni/app/` | GTK 4 + libadwaita app: `quick.py` popover, `window.py` pages, `theme.py` Omarchy colors, `bridge.py` socket to GTK thread |
| `omni/issues.py` | Issue reports: evidence file (recent turns, journal, git, settings, doctor) in `DATA/issues/` and the fix task for the coding agent that `issue_report` / `omni issue` open |
| `omni/cli.py`, `omni/client.py` | The `omni` command and the blocking socket client |
| `integration/` | systemd user units, Hyprland Lua (`hypr/omni.lua`), Quickshell bar plugin, desktop entry |
| `legacy/` | The v0 build, kept for reference only. Do not import from it |

## Conventions

- **Latency is the product.** Measure before and after, using the per-turn `timings` in `episodes.sqlite` (`omni ask` prints them). Keep the developer instructions in `prompt.py` short and stable so the cached prefix stays warm. Do not put live data (window lists, app catalogs) in the prompt; tools return it.
- **No polling in Omni's own code.** The app and CLI subscribe to the socket. The one exception is the bar plugin's 2 s re-read of `status.json`, which re-arms its file watch after the atomic replace.
- **Tools are small and honest.** Return what happened, raise `ValueError`/`RuntimeError` with a sentence the model can act on, and mark desktop-changing tools `action=True`.
- **Policy stays consequence-based.** Add to the ask list only for things that are hard to undo or leave the machine. Never add prompts for opening, arranging, typing, or reading.
- **Private data never enters the repo.** Use temporary directories and the `OMNI_DATA`, `OMNI_CONFIG`, and `OMNI_RUNTIME` environment variables in tests and manual runs. Run `scripts/check_public.py` before committing.
- **Tests** cover behavior that could regress: policy, memory, speech gating, tool schemas, parsing. Run `python3 -m unittest discover -s tests`.

## Running a development daemon

```bash
export OMNI_DATA=/tmp/o/data OMNI_CONFIG=/tmp/o/config OMNI_RUNTIME=/tmp/o/run
python3 -m omni.daemon -v          # needs the venv (~/.local/share/omni/venv/bin/python) for voice
python3 -m omni.cli ask "hello"
python3 -m omni.app                # the app against the same socket
```

Stop the installed service first (`systemctl --user stop omnid`) if you want the real socket path, or keep the isolated one above.

## Codex app-server protocol

Omni uses the experimental API (`capabilities.experimentalApi`) for `dynamicTools`: tool specs go in `thread/start`, and calls come back as `item/tool/call` server requests. To see the exact protocol of the installed CLI, run:

```bash
codex app-server generate-json-schema --experimental --out /tmp/codex-schema
```

If a CLI update renames something, fix it in `codex.py` or `session.py` (`Runner.on_notification` / `on_request`) and record the CLI version in PLAN.md.
