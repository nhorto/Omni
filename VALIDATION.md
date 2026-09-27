# Validation log

Live checks for the rebuild, newest first. Record the machine, CLI versions, and timings. Timings come from `omni ask` output or the Activity page and are measured from when omnid receives the request (for voice, from the end of speech).

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
