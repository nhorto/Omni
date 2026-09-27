# Contributing

Omni is in active development. Check PLAN.md for scope, build status, and acceptance criteria. Describe user-visible behavior and validation in pull requests. Prefer supported app APIs and exact targets for desktop actions. Preserve consequential-action approval and stop a sequence when a prerequisite fails.

Run the unit suite and public-file check before contributing. Use synthetic fixtures and temporary directories. Never include real mail, recordings, profiles, secrets, browser state, action exports, or personal home-directory paths. Use a GitHub noreply commit address if you do not want your email in public history.

GitHub issues should describe reproducible behavior with sanitized examples. Do not attach full debug archives or databases. Tests should cover real behavioral risks rather than repeat implementation details.
