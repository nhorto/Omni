# Full-release roadmap

The definition of done is the complete personal-assistant vision, including proactive notifications and broader computer control. The current app is a working foundation. A passing unit suite is not proof that every desktop or live integration works.

| Area | Current state | Remaining acceptance checks |
| --- | --- | --- |
| Desktop workflows | Workspace changes, window moves/placement, app launches and approved visible terminal commands | Run realistic multi-step requests end to end; handle existing windows, focus, scaled/rotated monitors, partial failures, cancellation, and recovery. Verify command outcomes rather than only launch success. |
| Broader computer control | Browser snapshots/clicks/input and AT-SPI controls | Add observe/act/verify loops and exact app/window targeting, then keyboard/mouse fallback and visual checks where app APIs are unavailable. Prevent acting on stale or ambiguous targets. |
| Conversations and voice | Separate threads; silent actions; spoken answers; transcript queue and recording widgets | Hardware test push-to-talk and continuous capture, stop, interruptions, long utterances, noise, and no duplicate execution. Test a chosen custom voice live. |
| Memory | Local SQLite facts, FTS notes, profile/personality, automatic explicit non-sensitive facts | Add entities, event history, corrections and expiration; recall across paraphrases; provenance and deletion tests. Introduce local embeddings only with measurable retrieval improvements. |
| Proactive assistant | Background voice service; local status/notifications | Durable local reminders and event intake, duplicate suppression, urgency and quiet-hour settings, reviewable suggestions and daily briefings. No automatic send or calendar write. |
| Outlook and calendar | Web draft/send adapter with simulated tests | Account setup is deferred. After sign-in: validate live read/draft/send, delegated connection, incremental mail/calendar sync, reminders/suggestions, and briefing accuracy. |
| Models and usage | Codex/Claude selection; manual model override; local exact-command routing | Keep Codex usable independently of Claude, improve local coverage and routing with benchmarks, record available usage metadata, and handle provider limits gracefully. |
| Action dataset | Request/plan/step history and feedback; reviewed-only export | Record observed pre/post state and command exit status; privacy review, versioned export, held-out evaluation, and a separate local-model training experiment. |
| App and delivery | Native control app and user integration | Portable installation and dependency checks, onboarding, actionable error states, tests/CI, and documented upgrade/recovery. |

## Active order

1. Publish a clean, portable source repository with tests and a public-data check.
2. Close desktop reliability gaps and validate a complete multi-terminal workflow using the subscribed Codex agent.
3. Build local proactive reminders and persistent review state without depending on email sign-in.
4. Improve memory retrieval and correction behavior against realistic synthetic recall cases.
5. Extend observed computer control and task recovery.
6. Complete account/hardware-dependent checks when those resources are available.

## Release evidence

Each completed item needs a specific test or observed outcome, not just an implemented function. Document live checks separately from simulations. The release remains unfinished while required acceptance checks are open. Mail/calendar account work is intentionally deferred until the account is set up.
