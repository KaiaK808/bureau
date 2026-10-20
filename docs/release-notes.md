# Bureau v3.4.1 — Herdr pane variables and provider result recovery

**Stable release · 2026-10-20 · source tag `v3.4.1`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.4.1) is marked as latest and records the exact tagged commit. The previous stable release is [v3.4.0](release-notes-v3.4.0.md); its notes retain the earlier release history.

v3.4.1 is a patch release on v3.4.0 with three fixes in one pull request, [#73](https://github.com/KaiaK808/bureau/pull/73). The v3 exit-code contract stays; no exit code or configuration key is new, and no interfaces change. It goes straight to stable without a release candidate.

## What changed since v3.4.0

### Herdr pane variables stay out of child environments

Bureau drops every `HERDR_` variable from the environments it builds for headless agents, their login checks, test commands and other child commands, in both `repo.untrusted_env` modes. Herdr is a terminal multiplexer; its pane variables made headless stages claim the operator's pane through the SessionStart hook, so Herdr later showed or resumed a Bureau stage instead of the operator's session. The clean-mode allowlist and exit codes stay the same. Installations that start shepherd runs from inside Herdr benefit directly.

### Codex can recover from an error notice

A Codex `error` event followed by a later `turn.completed` no longer fails the stage with `22`. A later completion recovers preceding error notices, and the last completion supplies usage. Successful call metadata records the recovered count and first message, truncated to 200 characters. Any `turn.failed` or error without a later completion still fails; final-message requirements, schema validation and failing-process classification are unchanged.

### Claude structured results survive a late wakeup

Claude stages with tools run with `ScheduleWakeup`, `CronCreate`, `CronDelete`, `RemoteTrigger` and `Monitor` disallowed through `--disallowedTools`, including behind Headroom; `upstream_summary` stays tool-free. This prevents a late scheduled wakeup from discarding a completed structured result and turning finished work into exit `22`.

When a successful Claude envelope lacks an object in `structured_output` and the stage expects a schema, the adapter takes the last `StructuredOutput` call's input from that session's stage transcript, validates it as before and records `structured_source: "transcript"` in the call metadata. An object in the envelope keeps precedence. An invalid latest transcript result still fails; a missing transcript or tool call retains the existing handling, including `22` for a prose-only result. Codex arguments, its `final.txt` path and the schemas are unchanged.

## Upgrade an existing installation

**From v3.4.0:** select tag `v3.4.1` in the source skill checkout, then resync the **scripts scope as one set** (`--resync-scripts`) in each adopting repository and restart queue loops and supervisors. No other action is required. See [Upgrade to v3.4.1](migration.md#upgrade-to-v341) and the [source-update instructions](migration.md#select-the-source-release).

**From earlier versions:** follow [Upgrade to v3.4](migration.md#upgrade-to-v34) and its earlier upgrade sections first, then follow the v3.4.1 steps above.

## Compatibility and rollback

The v3 exit-code contract stays; no exit code or configuration key is new, and no interfaces change. No configuration or schema migration is needed.

To roll back, select tag `v3.4.0` in the source skill checkout and resync the scripts scope as one set, or restore the private backup ([rollback](migration.md#rollback)), then restart queue loops and supervisors. Rollback cannot undo commits, pushes or Linear/GitHub changes made in between.

## Validation

The maintainer's release validation record reports new regression tests for each fix, each failing on v3.4.0. The full suite (107 tests) passed on macOS outside any sandbox on v3.4.0, on each fix and on the combined branch. Each fix had one independent Codex review with no findings.

GitHub CI is green on macOS and Ubuntu for [#73](https://github.com/KaiaK808/bureau/pull/73) at head `35c176d`. It was merged as `5b71c1b` with an identical tree. Release preparation changes documentation only; runtime, installer, templates, tests and CI stay at the `5b71c1b` baseline. The final tagged commit is recorded in the GitHub Release body. See [current release status](releases.md#current-release-status).

## Known limitations

Provider calls are faked in tests. No live Herdr session, live Codex 503 recovery or live wakeup was reproduced. No installation has run v3.4.1 before the release, and there is no release candidate; the first installation runs follow the release.
