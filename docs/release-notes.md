# Bureau v3.3.0-rc.1 — a shell gate for Codex implement turns in the sandbox

**Release candidate · 2026-10-07 · source tag `v3.3.0-rc.1`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.3.0-rc.1) is published as a prerelease and records the exact tagged commit. [v3.2.1](release-notes-v3.2.1.md) stays the stable release, marked as latest, until v3.3.0 is published. Earlier releases: [v3.2.0](release-notes-v3.2.0.md) and its candidate [v3.2.0-rc.1](release-notes-v3.2.0-rc.1.md), [v3.1.0](release-notes-v3.1.0.md) with its candidates [v3.1.0-rc.2](release-notes-v3.1.0-rc.2.md) and [v3.1.0-rc.1](release-notes-v3.1.0-rc.1.md), [v3.0.2](release-notes-v3.0.2.md), [v3.0.1](release-notes-v3.0.1.md), [v3.0.0](release-notes-v3.0.0.md) with its candidates [v3.0.0-rc.2](release-notes-v3.0.0-rc.2.md) and [v3.0.0-rc.1](release-notes-v3.0.0-rc.1.md), and the previous major release [v2.0.0](release-notes-v2.0.0.md).

v3.3 is a minor release on v3.2.1 with one pull request, [#60](https://github.com/KaiaK808/bureau/pull/60). A Codex implement run blocked only by tests the sandbox denies can finish when the full project test command passes in the shell outside the sandbox. Codex calls without `TMPDIR` also get a private temporary directory under `/tmp`. The v3 exit-code contract stays; no exit code is new. Claude runs are unchanged.

## What changed since v3.2.1

- **The Codex turn names sandbox-only blockers.** Its implement prompt says to finish every other task and report `NEEDS_HUMAN` with one `notes.needs_human` object per affected task. Each string `reason` must begin with `SANDBOX_GATE:` and name the failing tests and the denied operation, such as socket bind/listen, network access or access to the per-user temporary directory. Never change, skip or deselect tests to bypass the sandbox; code failures and other blockers must not use the prefix.
- **Part 1 admits the run to the existing completion path.** Only a non-empty needs-human array with every reason prefixed, a configured `repo.test_command` and commits beyond `origin/main` qualify. The shell treats that run as `COMPLETE` before the post-implement hook and squash-range check. A missing command, no commits, mixed reasons or malformed reasons leave `NEEDS_HUMAN` in place; hook and squash-range failures keep their existing handling.
- **Part 2 runs the project gate over the pushed state.** If the intervening checks still allow completion, the shell runs `repo.test_command` once after the final push, outside the Codex sandbox through `bureau_untrusted_env bash --noprofile --norc -c`, without Bureau secrets. Green continues as `COMPLETE` and hands off to QA or Build Review; red, or a command emptied in between, restores `NEEDS_HUMAN` and keeps the ticket labelled `needs-human`. The ordinary Codex completion check does not run the suite a second time. The implement summary comment records the gate decisions.
- **The shell reader is strict.** It accepts one JSON object or a cost envelope whose `result` string holds one object, reads the whole input and preserves parsing failure. Fenced JSON, trailing garbage, multiple values and partial reads cannot qualify. Every needs-human entry must be an object with a string reason beginning with the prefix.
- **The provider lets Codex reach the shell gate.** A Codex `NEEDS_HUMAN` result with only `SANDBOX_GATE:` reasons is passed through even when it contains “Operation not permitted”, “Permission denied” or “sandbox denied”; the provider no longer ends that call early with `24`. Other denied-operation results keep the existing environment-blocked handling. Claude result handling is unchanged.
- **Codex gets a temporary directory it can use.** When its child environment has no `TMPDIR`, the provider allocates a private `bureau-codex-*` directory directly under resolved `/tmp`, outside the repository regardless of `TEMP` or `TMP`. Allocation follows the signal handlers, and cleanup follows the child on completion, timeout, interruption and exceptions. Removal failures are reported without failing the call. An existing `TMPDIR` passes through unchanged; the provider's own environment is unchanged.

Claude prompts, child environments, result handling and exit codes retain their previous behaviour. See the [sandbox gate contract](provider-runtime.md#codex-implementation-sandbox-gate) for the runtime details.

## Upgrade an existing installation

**From v3.2.1:** select tag `v3.3.0-rc.1` in the source skill checkout, then resync the **scripts scope as one set** (`--resync-scripts`); `implement-pipeline.sh`, `bureau-config.sh` and `bureau-provider.py` changed together. Pause dispatch, preserve unfinished work and customized files, and use [preview and resolve](migration.md#preview-and-resolve-asset-conflicts). No configuration migration is needed.

For Codex implement, `repo.test_command` must hold the full project gate, including the tests the sandbox can deny. Restart queue loops and supervisors after the resync so they read the new scripts; a stage already running keeps the old scripts because the installer replaces files atomically. Run doctor (`python3 scripts/bureau-doctor.py --mode background`) and follow [Upgrade to v3.3](migration.md#upgrade-to-v33).

**From v3.1.0 or older:** go through [Upgrade to v3.2](migration.md#upgrade-to-v32) and its earlier upgrade steps first, including the scripts and interfaces scopes and the applicable configuration checks, then follow the v3.3 steps above.

## Compatibility and rollback

Dependencies, supported platforms, configuration schemas and the manifest version are unchanged. The v3 exit-code contract stays; no configuration or data migration is needed.

To roll back, select tag `v3.2.1` again in the source skill checkout and resync the scripts scope as one set, or restore the private backup ([rollback](migration.md#rollback)). Restart queue loops and supervisors after resync. Rollback restores the previous Codex handling of sandbox-denied tests and temporary directories; it cannot undo commits, pushes or Linear/GitHub changes made in between.

## Validation

CI on `main` at `0c2f286` (the merge of [#60](https://github.com/KaiaK808/bureau/pull/60)) is green on macOS and Ubuntu. All 104 harness tests passed, and an independent review found no blocker. Release preparation changes documentation only; runtime, installer, templates, tests and CI stay at that baseline. The final tagged commit is recorded in the GitHub Release body.

**Acceptance is still pending at tagging.** The acceptance run is an installation's next real Codex implement run. Automated checks and the independent review do not replace that run; v3.2.1 stays the stable release marked as latest.

## Known limitations

- The `SANDBOX_GATE:` prefix is the Codex turn's own diagnosis. The shell verifies the result's shape and the project test command, not why the sandboxed test failed.
- A green test command protects only what it covers. Keep `repo.test_command` configured with the full project gate; QA and review follow as usual.
- Loopback or network access for the Codex sandbox is not part of v3.3.0-rc.1. The gate runs the configured tests outside that sandbox; it does not grant the turn those permissions.
- On `SIGKILL`, the provider cannot run its cleanup and a `bureau-codex-*` temporary directory can stay under `/tmp`.
