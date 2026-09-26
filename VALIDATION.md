# Validation record

These checks distinguish live local behavior from automated simulations. They are not a claim that the full roadmap is finished.

## Public foundation

- Portable installer tested with checkout paths containing spaces and apostrophes; repeated install is idempotent and replaced files are backed up.
- GitHub Actions installs GTK/AT-SPI Python bindings and runs the unit suite and tracked-file privacy check.
- Only clean public `main` history is published; earlier local development history is not an ancestor of the public branch.

## Desktop workflow

A subscribed Codex agent planned a synthetic request to switch to workspace 3, open a terminal running `pwd` on the left, and a second terminal running `date` on the right. All five steps executed; two distinct floating terminals were verified on workspace 3 with non-overlapping positions. Test windows were closed and the original workspace restored.

This checks planning, terminal launch, window association, workspace and geometry. A later live synthetic Foot command printed a known marker; its private result file recorded exit 0 and the marker. The test terminal was then closed. Unit cases cover terminal success/failure reconciliation, scaled/rotated displays, reserved panel areas, geometry verification failures, cancelled prerequisites, ambiguous launches, and concurrent request exclusion. Exact window focus and named editable field changes have synthetic verification tests; live focus/edit cases still need validation in an unlocked session.

A separate synthetic Foot window received `CTRL+L` through Hyprland's exact-address shortcut dispatcher, which returned success. The test window was closed. This verifies dispatch acceptance and targeting, not the application effect; arbitrary keyboard and mouse control remain open work.

## Local reminders and memory

A synthetic reminder was scheduled in the local database. The running background service delivered it to the real notification daemon and stored the returned notification ID after one attempt. The app exposed its Reminders page and Add reminder control through accessibility.

Automated cases cover restart catch-up, no repeated delivery after completion, active-claim exclusion, retry limits, cancellation, midnight-spanning quiet hours, source-event deduplication, and proposal review. Memory tests include retrieving a matching passage far into a long note. Training export tests verify before/after desktop state and exclusion of unreviewed or incorrect tasks.

Synthetic voice checks now cover a planning failure reaching Activity, an approval window closed before execution, and a completed review releasing the bridge while the app remains open. Live microphone and interruption behavior still needs hardware validation.

## Still awaiting live validation

Microphone capture/continuous listening, the selected ElevenLabs voice, Outlook account access and calendar sync, and general visual/keyboard/mouse control across arbitrary apps. See ROADMAP.md for the full acceptance criteria.
