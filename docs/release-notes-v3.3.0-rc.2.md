# Bureau v3.3.0-rc.2 — shell gates for Codex implement and QA turns in the sandbox

**Release candidate · 2026-10-07 · source tag `v3.3.0-rc.2`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.3.0-rc.2) is published as a prerelease and records the exact tagged commit. This candidate is superseded by the stable [v3.3.0](release-notes.md), which has the same runtime, installer, templates and tests. The previous stable release is [v3.2.1](release-notes-v3.2.1.md). The previous candidate is [v3.3.0-rc.1](release-notes-v3.3.0-rc.1.md), which stays published as a prerelease. Earlier releases: [v3.2.0](release-notes-v3.2.0.md) and its candidate [v3.2.0-rc.1](release-notes-v3.2.0-rc.1.md), [v3.1.0](release-notes-v3.1.0.md) with its candidates [v3.1.0-rc.2](release-notes-v3.1.0-rc.2.md) and [v3.1.0-rc.1](release-notes-v3.1.0-rc.1.md), [v3.0.2](release-notes-v3.0.2.md), [v3.0.1](release-notes-v3.0.1.md), [v3.0.0](release-notes-v3.0.0.md) with its candidates [v3.0.0-rc.2](release-notes-v3.0.0-rc.2.md) and [v3.0.0-rc.1](release-notes-v3.0.0-rc.1.md), and the previous major release [v2.0.0](release-notes-v2.0.0.md).

Second release candidate for v3.3.0. rc.2 adds one pull request, [#62](https://github.com/KaiaK808/bureau/pull/62), on top of rc.1's [#60](https://github.com/KaiaK808/bureau/pull/60): Codex QA turns blocked only by tests the sandbox denies now reach a shell gate too. v3.3 is a minor release on v3.2.1; it carries the implement gate, the QA gate and a private temporary directory for Codex calls without `TMPDIR`. The v3 exit-code contract stays; no exit code is new. Claude runs are unchanged.

## What changed since v3.3.0-rc.1

All in [#62](https://github.com/KaiaK808/bureau/pull/62); this addition applies to Codex QA runs only.

- **The QA turn names sandbox-only blockers.** It reports `NEEDS_HUMAN` with string `coverage_notes` beginning with `SANDBOX_GATE:` and naming the tests and denied operation. QA keeps its existing schema, with no needs-human array. The strict reader accepts exactly one JSON object or a cost envelope whose `result` string holds exactly one object; fenced JSON, trailing garbage, multiple values, missing or non-string notes and unprefixed blockers never qualify.
- **The final outside run decides the status.** The existing Phase 3 run of the project test command outside the sandbox decides GREEN (Build Review) or RED (Build), without a `needs-human` label or an extra test run. The decision leads the QA summary comment. The squash-range check still holds the ticket for a human if it finds a CI suppressor or cannot read the range; its report then leads the comment.
- **The provider lets Codex QA reach its gate.** A qualifying QA result is passed through even when it names “Operation not permitted”, “Permission denied” or “sandbox denied”, instead of ending early with `24`. The implement exception remains scoped to its `notes.needs_human` array; QA uses `coverage_notes`. Claude prompts and behaviour, other stages and exit codes are unchanged.

## What changed since v3.2.1

- **The Codex turn names sandbox-only blockers.** Its implement prompt says to finish every other task and report `NEEDS_HUMAN` with one `notes.needs_human` object per affected task. Each string `reason` must begin with `SANDBOX_GATE:` and name the failing tests and the denied operation, such as socket bind/listen, network access or access to the per-user temporary directory. Never change, skip or deselect tests to bypass the sandbox; code failures and other blockers must not use the prefix.
- **Part 1 admits the run to the existing completion path.** Only a non-empty needs-human array with every reason prefixed, a configured `repo.test_command` and commits beyond `origin/main` qualify. The shell treats that run as `COMPLETE` before the post-implement hook and squash-range check. A missing command, no commits, mixed reasons or malformed reasons leave `NEEDS_HUMAN` in place; hook and squash-range failures keep their existing handling.
- **Part 2 runs the project gate over the pushed state.** If the intervening checks still allow completion, the shell runs `repo.test_command` once after the final push, outside the Codex sandbox through `bureau_untrusted_env bash --noprofile --norc -c`, without Bureau secrets. Green continues as `COMPLETE` and hands off to QA or Build Review; red, or a command emptied in between, restores `NEEDS_HUMAN` and keeps the ticket labelled `needs-human`. The ordinary Codex completion check does not run the suite a second time. The implement summary comment records the gate decisions.
- **The shell reader is strict.** It accepts one JSON object or a cost envelope whose `result` string holds one object, reads the whole input and preserves parsing failure. Fenced JSON, trailing garbage, multiple values and partial reads cannot qualify. Every needs-human entry must be an object with a string reason beginning with the prefix.
- **The provider lets Codex reach the shell gate.** A Codex `NEEDS_HUMAN` result with only `SANDBOX_GATE:` reasons is passed through even when it contains “Operation not permitted”, “Permission denied” or “sandbox denied”; the provider no longer ends that call early with `24`. Other denied-operation results keep the existing environment-blocked handling. Claude result handling is unchanged.
- **Codex gets a temporary directory it can use.** When its child environment has no `TMPDIR`, the provider allocates a private `bureau-codex-*` directory directly under resolved `/tmp`, outside the repository regardless of `TEMP` or `TMP`. Allocation follows the signal handlers, and cleanup follows the child on completion, timeout, interruption and exceptions. Removal failures are reported without failing the call. An existing `TMPDIR` passes through unchanged; the provider's own environment is unchanged.
- **QA uses its final outside run for a sandbox-only blocker.** A Codex QA `NEEDS_HUMAN` result with string `coverage_notes` beginning with `SANDBOX_GATE:` passes through the provider and strict reader to the shell. The final test run decides GREEN or RED, with no extra run; the summary comment records the decision, and the squash-range check still applies as described above. Never change, skip or deselect a test to bypass the sandbox; a correctness bug or a harness broken outside it stays `NEEDS_HUMAN` without the prefix.

Claude prompts, child environments, result handling and exit codes retain their previous behaviour. See the [implement sandbox gate contract](provider-runtime.md#codex-implementation-sandbox-gate) and the [QA sandbox gate contract](provider-runtime.md#codex-qa-sandbox-gate) for the runtime details.

## Upgrade an existing installation

**From v3.3.0-rc.1:** select tag `v3.3.0-rc.2` in the source skill checkout and resync the **scripts scope as one set** (`--resync-scripts`); `qa-pipeline.sh`, `bureau-config.sh` and `bureau-provider.py` changed together. No configuration, interface or schema migration is needed.

**From v3.2.1:** select tag `v3.3.0-rc.2` in the source skill checkout, then resync the **scripts scope as one set** (`--resync-scripts`); `implement-pipeline.sh`, `qa-pipeline.sh`, `bureau-config.sh` and `bureau-provider.py` changed together. The rc.1 upgrade steps apply with this tag; no configuration migration is needed.

In both cases, pause dispatch, preserve unfinished work and customized files, and use [preview and resolve](migration.md#preview-and-resolve-asset-conflicts).

For Codex implement and QA, `repo.test_command` must hold the full project gate, including the tests the sandbox can deny. Restart queue loops and supervisors after the resync so they read the new scripts; a stage already running keeps the old scripts because the installer replaces files atomically. Run doctor (`python3 scripts/bureau-doctor.py --mode background`) and follow [Upgrade to v3.3](migration.md#upgrade-to-v33).

**From v3.1.0 or older:** go through [Upgrade to v3.2](migration.md#upgrade-to-v32) and its earlier upgrade steps first, including the scripts and interfaces scopes and the applicable configuration checks, then follow the v3.3 steps above.

## Compatibility and rollback

Dependencies, supported platforms, configuration schemas and the manifest version are unchanged. The v3 exit-code contract stays; no configuration or data migration is needed.

To roll back to rc.1, select tag `v3.3.0-rc.1` in the source skill checkout and resync the scripts scope as one set, or restore the private backup ([rollback](migration.md#rollback)). That removes the QA gate while retaining the implement gate and private temporary directories. To return to the stable release, select tag `v3.2.1` and resync the scripts scope as one set. Restart queue loops and supervisors after either resync. Returning to v3.2.1 restores the previous Codex handling of sandbox-denied tests and temporary directories; rollback cannot undo commits, pushes or Linear/GitHub changes made in between.

## Validation

CI on `main` at `51f713e` (the merge of [#62](https://github.com/KaiaK808/bureau/pull/62)) is green on macOS and Ubuntu. All 105 harness tests passed, and an independent review found no blocker. Release preparation changes documentation only; runtime, installer, templates, tests and CI stay at that baseline. The final tagged commit is recorded in the GitHub Release body.

**Implement acceptance is still pending at tagging.** The acceptance run is an installation's next real Codex implement run. Automated checks and the independent review do not replace that run; v3.2.1 stays the stable release marked as latest.

**The QA gate has no live run yet.** No installation runs QA on Codex. The full-stage harness tests and mutation controls cover its shell and provider paths; live Codex QA acceptance remains pending.

## Known limitations

- The `SANDBOX_GATE:` prefix is the Codex turn's own diagnosis. The shell verifies the result's shape and the project test command, not why the sandboxed test failed.
- A green test command protects only what it covers. Keep `repo.test_command` configured with the full project gate; QA and review follow as usual.
- A green QA sandbox gate does not bypass the squash-range check; a CI suppressor or an unreadable range still holds the ticket for a human.
- Loopback or network access for the Codex sandbox is not part of v3.3.0-rc.2. The gates run the configured tests outside that sandbox; they do not grant the turn those permissions.
- On `SIGKILL`, the provider cannot run its cleanup and a `bureau-codex-*` temporary directory can stay under `/tmp`.
