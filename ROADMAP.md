# Full-release roadmap

The definition of done is the complete personal-assistant vision, including proactive notifications and broader computer control. The current app is a working foundation. A passing unit suite is not proof that every desktop or live integration works.

| Area | Current state | Remaining acceptance checks |
| --- | --- | --- |
| Desktop workflows | Workspace changes, exact window focus, moves/placement, app launches and approved visible terminal commands | A complete two-terminal workflow is verified. Placement accounts for scale/rotation and panels; requests cannot execute concurrently. Terminal commands now record exit codes asynchronously; live restart/failure checks remain. Remaining: broader cancellation, recovery, and reused-window targeting after launch. |
| Broader computer control | Browser snapshots/clicks/input and AT-SPI controls including named text fields | Add observe/act/verify loops and keyboard/mouse fallback with visual checks where app APIs are unavailable. Prevent acting on stale or ambiguous targets. |
| Conversations and voice | Separate threads; silent actions; spoken answers; transcript queue and recording widgets; reviewed tasks release the bridge when done | Hardware test push-to-talk and continuous capture, stop, interruptions, long utterances, noise, and no duplicate execution. Test a chosen custom voice live. |
| Memory | Local SQLite facts, FTS notes, profile/personality, automatic explicit non-sensitive facts | Add entities, event history, corrections and expiration; recall across paraphrases; provenance and deletion tests. Introduce local embeddings only with measurable retrieval improvements. |
| Proactive assistant | Durable local reminders, quiet hours, delivery retry/recovery, and a review queue | Connect real event sources, add urgency/notification triage and daily briefings, and validate reviewable suggestions from those sources. No automatic send or calendar write. |
| Outlook and calendar | Web draft/send adapter with simulated tests | Account setup is deferred. After sign-in: validate live read/draft/send, delegated connection, incremental mail/calendar sync, reminders/suggestions, and briefing accuracy. |
| Models and usage | Codex/Claude selection; manual model override; local exact-command routing for reminders, memories, workspaces, URLs, and folder listings | Keep Codex usable independently of Claude, improve local coverage and routing with benchmarks, record available usage metadata, and handle provider limits gracefully. |
| Action dataset | Request/plan/step history including failed voice planning, observed before/after windows, terminal exit results, feedback, and versioned reviewed export | Privacy review, held-out evaluation, and a separate local-model training experiment. Pending or failed terminal jobs cannot enter training export. |
| App and delivery | Native control app, portable user installer, read-only setup diagnostics in CLI and Settings, public repository, public-file check, and CI | Guided onboarding, clearer errors, broader integration coverage, and documented upgrade/recovery. |

## Active order

1. Completed: publish a clean, portable source repository with tests and a public-data check.
2. In progress: close desktop reliability gaps. A complete multi-terminal workflow through Codex passed.
3. Local reminder delivery and persistent review state are implemented and tested; connect additional notification sources next.
4. Improve memory retrieval and correction behavior against realistic synthetic recall cases.
5. Extend observed computer control and task recovery.
6. Complete account/hardware-dependent checks when those resources are available.

## Release evidence

Each completed item needs a specific test or observed outcome, not just an implemented function. Document live checks separately from simulations. The release remains unfinished while required acceptance checks are open. Mail/calendar account work is intentionally deferred until the account is set up.
