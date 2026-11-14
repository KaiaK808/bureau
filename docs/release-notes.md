# Bureau v3.8.0 — QA evidence, approval status and runtime fixes

**Release preparation · 2026-11-14 · source tag `v3.8.0`.** v3.8.0 is a minor release on [v3.7.0](release-notes-v3.7.0.md). Tagging and GitHub Release publication remain separate maintainer operations.

The release includes, in first-parent merge order: [#133](https://github.com/KaiaK808/bureau/pull/133), [#134](https://github.com/KaiaK808/bureau/pull/134), [#139](https://github.com/KaiaK808/bureau/pull/139), [#140](https://github.com/KaiaK808/bureau/pull/140), [#136](https://github.com/KaiaK808/bureau/pull/136), [#138](https://github.com/KaiaK808/bureau/pull/138), [#141](https://github.com/KaiaK808/bureau/pull/141), [#142](https://github.com/KaiaK808/bureau/pull/142), [#137](https://github.com/KaiaK808/bureau/pull/137), [#143](https://github.com/KaiaK808/bureau/pull/143), [#144](https://github.com/KaiaK808/bureau/pull/144), [#145](https://github.com/KaiaK808/bureau/pull/145), [#146](https://github.com/KaiaK808/bureau/pull/146), [#147](https://github.com/KaiaK808/bureau/pull/147), [#148](https://github.com/KaiaK808/bureau/pull/148), [#149](https://github.com/KaiaK808/bureau/pull/149), [#150](https://github.com/KaiaK808/bureau/pull/150), [#151](https://github.com/KaiaK808/bureau/pull/151), [#152](https://github.com/KaiaK808/bureau/pull/152), [#153](https://github.com/KaiaK808/bureau/pull/153), [#154](https://github.com/KaiaK808/bureau/pull/154), [#155](https://github.com/KaiaK808/bureau/pull/155), [#156](https://github.com/KaiaK808/bureau/pull/156). All changes are merged. The [changelog](../CHANGELOG.md#380---2026-11-14) collects the 22 release fragments; the CI dependency update in #137 is also included.

## What changed since v3.7.0

### Optional advisory QA evidence

Three new QA checks are **opt-in and advisory and never change QA's result**: fail-before-pass, test integrity and task completion. They read settings from QA's trusted pinned base and add bounded evidence to the acceptance brief. Existing mandatory cancellation, timeout and process-ownership holds still apply independently of their findings. Resync scripts together before enabling any check.

`repo.changed_test_command` enables fail-before-pass measurement after the final QA gate passes. Changed tests run in isolated head/base trees without inherited Git context. The configured command uses one unquoted `{file}` placeholder, for example `python3 -m pytest -q {file}`. Repositories with worktree links are not measured. Cleanup timeouts or errors preserve completed results and report leftovers for the next sweep; retained execution ownership keeps QA held. See [configuration](configuration.md#repo) and [#146](https://github.com/KaiaK808/bureau/pull/146).

`repo.test_integrity_check: true` adds static checks after green QA for removed assertions, new skip/disabled markers and unresolved first-party imports. Verdicts are `pass`, `flagged` or `unverifiable`. Python uses syntax trees; other supported languages use symmetric lexical counts, including tokens in literals and comments, so their findings say "assertion-like sites went down". Relative imports can resolve root packages or JavaScript index files; imports outside the tree and escaped JavaScript specifiers are not checked. These checks cannot establish whether an assertion became weaker in meaning. See [#147](https://github.com/KaiaK808/bureau/pull/147).

`repo.task_completion_check: true` compares completed tasks' named paths with the pinned ticket diff and flags test tasks without added or modified test files. Counts and bounded per-task rules appear in the acceptance brief. This proves only that a named file changed; correctness still requires review. The default is off. See [#152](https://github.com/KaiaK808/bureau/pull/152).

### Exact-commit human approval accident guard

An opt-in `bureau/human-approval` commit status records approval of the exact current PR head. The decision script reads `agents.merge_approvers` from the default branch. An allowed person creates a comment whose first line is `/bureau approve <40-hex-sha>`. Only newly created comments trigger the workflow; edited comments do not count. A new push leaves its new commit without the status, and refused commands leave statuses untouched.

Install the separate `approval` workflow scope as well as the scripts scope, and commit the script, workflow and approval policy to the default branch. Require `bureau/human-approval` in branch protection for it to bind merges. Doctor reports configuration and installation problems and warns about a shared approver login; it does not read or change branch protection. Existing merge checks gain no context-specific approval gate.

This is an **accident guard**. Deliberate bypass remains possible: PR workflows can write the same status, and agents using an allowed person's login can approve themselves. Use a separate account or token when Bureau shares an approver's login. An unforgeable form needs a separate Bureau identity and remains future work. Absent or empty policy leaves the feature off; invalid or unreadable policy refuses approval. See [#151](https://github.com/KaiaK808/bureau/pull/151).

### Secrets file outside the checkout

New installations put their secrets file in `~/.config/bureau/<repository directory name>.env`, with file mode 0600 and directory mode 0700. `repo.env_file` selects the file. Existing installations keep working with their current file and can move it using the [migration steps](migration.md#move-the-secrets-file-outside-the-checkout).

Doctor warns about checkout-local or group/other-readable secrets. Moving the file blocks the conventional checkout path only: a process running with the user's permissions can still discover and read the external file. Update any `BUREAU_ENV_FILE` process override and restart loops and supervisors after moving it. See [#155](https://github.com/KaiaK808/bureau/pull/155).

### Stored Git credentials

Branch-controlled code no longer gets stored Git credential helpers by default: agents, test gates, the post-implement command, the review build check, conflict commands and changed-test measurements all receive the reset in both environment modes. Terminal prompting is disabled. Bureau's own git and gh calls retain their credential behavior.

In default environment mode, the reset preserves valid inherited counted Git configuration and appends an empty `credential.helper` entry; clean mode inherits no counted pairs. Both modes drop inherited `GIT_CONFIG_PARAMETERS` so an included file cannot restore helpers after the reset. Malformed inherited counts discard the inherited counted pairs and start the reset at entry 0. Invalid settings refuse with exit 24 before a gate creates a process hold. The reset switches off helper lookup; code can still supply its own helper or invoke one directly. **Askpass limit:** `GIT_ASKPASS`, `SSH_ASKPASS` and configured `core.askPass` are unchanged and can still supply a password.

Installations whose test gates or post-implement commands fetch private dependencies using a stored credential are affected. Set `repo.untrusted_git_credentials: "keep"` to restore the old behavior, or supply another authentication method before restarting. Resync the scripts together. On rollback, remove this setting for the older runtime; restoring that runtime restores its credential behavior. See [#154](https://github.com/KaiaK808/bureau/pull/154).

### Impeccable pins

Supported Impeccable versions are now skill **4.9.0** and engine **0.1.23**. Installations with `design.impeccable.enabled: true` must separately install those versions and update `design.impeccable.skill_version` to `"4.9.0"` and `design.impeccable.engine_version` to `"0.1.23"` in `.bureau.json`, or remove both version keys to follow the pins, together with the scripts resync. Until tooling and configuration match, Bureau reports Impeccable as unavailable and stages continue without it. With Impeccable disabled, only the resync is needed; old version values are harmless while it stays off.

Follow [the installation guidance](configuration.md#designimpeccable): `--no-hooks` is required, and separately installed agents must be removed or disabled. Bureau installs none of this tooling. Upstream skill **4.9.1** and engine **0.1.24** are unsupported by this release. See [#156](https://github.com/KaiaK808/bureau/pull/156).

### QA retries, review questions and implementation searches

QA's final test gate allows one bounded rerun for complete, named unittest/pytest assertion failures. Crashes, timeouts, zero tests, cleanup failures and environment denials stay on the failure path. Only failure diagnostics are classified; test names and traceback file/function names do not decide eligibility. Simple native commands select failed cases; other commands rerun the gate and must show complete case identities and passing coverage of every original case. Counts alone cannot establish recovery. Phase 1 retains its existing whole-gate retry on any nonzero exit. See [#140](https://github.com/KaiaK808/bureau/pull/140).

A recovered final gate records a bounded `flaky` marker, retains both logs and reports the flake in the QA comment and acceptance packet. Implementation receives no-change guidance only for a QA bounce carrying that recorded flake; ordinary QA and human feedback retain their prompts. Final QA gates default to 900 seconds per attempt through `BUREAU_QA_TEST_TIMEOUT` (1–86400). Older evidence readers must be upgraded together.

Review specialists separate optional human questions from acceptance-criteria holds. Questions never change verdicts; every hold still blocks. Independent and late reports retain their complete question union, rendering shows at most 32 with an omitted count, and fallback receives unchanged reports while validated, escaped questions are appended. The model-written comment may still repeat specialist text. See [#149](https://github.com/KaiaK808/bureau/pull/149).

Implementation's bounded-loop and `/goal` briefs ask for a repository search for the same defect after each review fix. Optional bounded `sibling_searches` reports record the finding, search, other-site count and fixed/out-of-scope paths. Legacy results remain valid; `/goal` retains reports across turns within the same bounds. Receipts filter paths individually and preserve safe out-of-scope sites as advisory open points without expanding accepted scope or overlap claims. Frozen briefs receive no backfill. See [#138](https://github.com/KaiaK808/bureau/pull/138).

### Runtime waits, login checks and path inventories

Evidence lock acquisition has one fixed 10-second wait budget and reports elapsed wait on timeout. Review artifact cleanup shares one 10-second budget and reports skipped artifacts. Exclusive locking and file/directory fsync remain. See [#144](https://github.com/KaiaK808/bureau/pull/144).

Provider login checks retry a timeout once, with limits of 20 seconds then 60 seconds. Two timeouts return existing retryable exit 16 with `login-check-timeout`; doctor warns that login is unconfirmed. See [#145](https://github.com/KaiaK808/bureau/pull/145).

Doctor warns when the latest matching recorded gate duration exceeds the 600-second tool command limit for an enabled Codex implementation or QA stage without delegation. The warning names `agents.providers.codex.gate_outside_sandbox` and [ADR 0013](adr/0013-codex-gate-delegation.md). Non-delegated briefs require the configured command verbatim, without wrappers, prefixes, redirects or environment changes, so execution proof can match. Missing or invalid evidence supplies no estimate. See [#136](https://github.com/KaiaK808/bureau/pull/136).

File-overlap inventories ignore code spans without a slash, backslash, period or known path name. Dotted names with spaces and unresolved paths retain their existing extraction, and an optional count records ignored spans. Task-generation guidance names repository-relative paths explicitly. See [#148](https://github.com/KaiaK808/bureau/pull/148).

### Contributor checks and release records

A hostile-input corpus uses deterministic obedient agents, stage assertions, guard-removal controls and strict expected-failure cases. Sandboxes use resolved system temporary space outside Git checkouts, isolate Git discovery/configuration and stop after failed setup. Review annotates root `LESSONS.md` and installed inline stage prompt changes as advisory `AGENT_CONFIG` findings for human inspection; ordinary spec artifacts remain excluded. The release checklist includes a manual live sample. See [#150](https://github.com/KaiaK808/bureau/pull/150).

Test runners suppress Python bytecode, including isolated imports, while the public-reference guard still scans tracked files and non-ignored new files. Test files and direct cancellation fixtures reset inherited ignored signals without changing the runner's Ctrl-C behavior. Mutation cleanup retries Ctrl-C interruptions before propagating cancellation, records completion after fixture shutdown and pipe closure, and attempts stderr closure even when stdout closure fails. Fixture repositories disable `receive.autogc` as well as automatic maintenance so older Git does not start background collection after pushes. These changes concern contributor tests and need no asset resync. See [#133](https://github.com/KaiaK808/bureau/pull/133), [#134](https://github.com/KaiaK808/bureau/pull/134), [#142](https://github.com/KaiaK808/bureau/pull/142) and [#143](https://github.com/KaiaK808/bureau/pull/143).

The untrusted-environment test removes `SSH_CLIENT`, `SSH2_CLIENT` and `SSH_CONNECTION` before its probes. Debian and Ubuntu Bash can otherwise read bashrc files instead of `BASH_ENV`, causing check D's negative control to fail when a suite starts over SSH. This is a repository-test fix with no adopting-repository action. See [#153](https://github.com/KaiaK808/bureau/pull/153). CI also updates `actions/upload-artifact` to 7.0.2 in [#137](https://github.com/KaiaK808/bureau/pull/137).

Contributors add one validated changelog fragment per change and run `python3 scripts/changelog.py check` or `render`. Maintainers collect fragments with `release VERSION DATE`; originals are staged by rename before atomic changelog replacement. Interrupted recovery or cleanup reports retained staging files; reconcile every `changes/.release-*` directory before retrying. The v3.7.0 publication record preserves its tag, merge, CI and tested-tree facts, and its notes are [archived unchanged](release-notes-v3.7.0.md). See [#141](https://github.com/KaiaK808/bureau/pull/141) and [#139](https://github.com/KaiaK808/bureau/pull/139).

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-changed-tests.py`, `bureau-test-integrity.py` | Complete scripts resync; optional fail-before-pass and static test integrity evidence. |
| `templates/scripts/bureau-task-completion.py` | Complete scripts resync for optional task-path evidence. |
| `templates/scripts/bureau-approval-status.py`, `templates/.github/workflows/bureau-approval.yml` | Decision script in scripts scope, workflow in the separate `approval` scope. |
| `scripts/changelog.py`, `changes/README.md` | Source-only fragment validation, preview and release collection. |
| `tests/fixtures/injection/`, `tests/injection_set_test.py`, QA/retry, approval and task-completion test files | Repository-only regression fixtures. |
| `docs/release-notes-v3.7.0.md` | Unchanged archive of the previous release notes. |

## Configuration and exit-code changes

No configuration schema migration, Spec Kit pin change or new exit code is introduced. The v3 vocabulary, including holds 30/31, remains. Optional QA and approval settings are off until selected. Impeccable pins change to skill 4.9.0 and engine 0.1.23. See [configuration](configuration.md#repo) and [exit codes](exit-codes.md).

| Key | Default / action | Effect |
|---|---|---|
| `repo.changed_test_command` | Absent (off) | Enables advisory isolated fail-before-pass measurement with one unquoted `{file}` placeholder. |
| `repo.test_integrity_check` | `false` | Boolean `true` enables advisory static checks after green QA. |
| `repo.task_completion_check` | `false` | Boolean `true` on the trusted base enables advisory task-path checks. |
| `agents.merge_approvers` | Absent/empty (off) | Default-branch approval policy; install the separate workflow and require its status in branch protection. |
| `repo.env_file` | Existing installations retain their current selection | New setup uses `~/.config/bureau/<repository directory name>.env`; update overrides and restart after moving. |
| `repo.untrusted_git_credentials` | `"off"`; `"keep"` restores old behavior | Private-dependency commands may need `"keep"` or another authentication method; askpass remains available; invalid settings exit 24. |
| `design.impeccable.skill_version`, `design.impeccable.engine_version` | `"4.9.0"`, `"0.1.23"` | Enabled installations install matching tooling and update or remove both keys; 4.9.1 / 0.1.24 are unsupported. |
| `BUREAU_QA_TEST_TIMEOUT` | `900`; range 1–86400 | Seconds per final QA gate attempt and the existing override for changed-test measurement. |
| `agents.providers.codex.gate_outside_sandbox` | Existing opt-in, unchanged | Doctor now points to it when recorded gate duration exceeds the tool command limit. |

Provider login timeouts retain retryable exit 16. Advisory QA findings introduce no verdict, routing or exit changes; existing process-safety holds remain mandatory.

## Upgrade an existing installation

**From v3.7.0, once v3.8.0 is published:** stop every shepherd, queue loop, dispatcher and supervisor. Preserve unfinished work/local commits, custom scripts/interfaces, instructions, Spec Kit assets/constitution and shared ownership, checkpoint, evidence, accounting, witness and process-hold records. Compare installed drift and reconcile incident fixes before replacing assets. Select the published `v3.8.0` tag, refresh discovery, preview and resolve conflicts, then resync the **complete scripts and interfaces scopes together** in every adopting repository:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

Use `$bureau-init` in Codex and retain the installation's existing target selection. `--update` changes configuration only; partial helper/pipeline updates are incompatible, and running shells keep loaded code. Upgrade evidence readers with the pipelines for flake metadata. The three advisory QA checks remain off unless enabled on the trusted base.

Existing secrets files keep working; to move one outside the checkout, follow [the migration steps](migration.md#move-the-secrets-file-outside-the-checkout), set `repo.env_file`, update process overrides and restart. If opting into human approval, install the separate `approval` scope and configure the default branch and required status before relying on it. If private-dependency commands rely on stored Git credentials, set `repo.untrusted_git_credentials: "keep"` or arrange another authentication method. For enabled Impeccable installations, install skill 4.9.0 / engine 0.1.23 and update or remove both version keys. Review the configuration table before restarting. Run `python3 scripts/bureau-doctor.py --mode background`, reconcile holds, restart drivers together and qualify a representative ticket before unattended use. See [From v3.7.0](migration.md#from-v370).

**From earlier versions:** apply the migration guide's cumulative sections through [v3.7](migration.md#upgrade-to-v37) first, including exits 30/31, helper/schema resync, Spec Kit repairs and runtime-record preservation. Contributor-only test tools, changelog fragments and release records need only a source checkout update.

## Compatibility and rollback

Configuration versions 1 and 2, Bash 3.2+, Python 3.9+, Node 18+ for the shared scheduler, Spec Kit 1.2.2 and the current-workspace prepare/finish protocol remain. Legacy implementation results without sibling-search reports remain valid. Older evidence readers must be upgraded together to consume QA flake metadata. The Impeccable integration now requires skill 4.9.0 and engine 0.1.23 when enabled; upstream 4.9.1 / 0.1.24 are unsupported.

For rollback, stop all drivers and reconcile unfinished writers, local commits and holds. Select `v3.7.0` and restore scripts and interfaces together or restore the private backup and compatible configuration. Remove the new advisory QA, approval and Git credential settings for the older runtime. If Impeccable is enabled, restore v3.7.0's skill 4.8.0 / engine 0.1.18 and matching configuration, or disable it. An older runtime does not resolve `repo.env_file`: preserve the external file and explicitly select it with a supported `BUREAU_ENV_FILE` override, or restore the prior secrets-file arrangement without exposing credentials. Reconcile any required approval status and installed workflow separately. Retain runtime records for inspection. Rollback cannot undo commits, pushes, comments, labels or state moves.

## Validation

The maintainer's validation record states that each change was merged with a macOS gate suite and passing PR CI on its own head. The merged content baseline is main `8af867d`. The remote suite on `openclaw-01` became a gate on **2026-11-14**, after a first-pass **191 of 191** on main `6205c6d`. That earlier run does not validate the final release branch.

The merged changes follow first-parent merge order:

| PR | Merge commit | Change |
|---|---|---|
| [#133](https://github.com/KaiaK808/bureau/pull/133) | `b06d407` | Test-runner bytecode suppression and public-reference inventory |
| [#134](https://github.com/KaiaK808/bureau/pull/134) | `499af47` | Interrupt-safe mutation cleanup |
| [#139](https://github.com/KaiaK808/bureau/pull/139) | `9fa1073` | v3.7.0 publication record |
| [#140](https://github.com/KaiaK808/bureau/pull/140) | `4e01763` | Bounded QA assertion rerun and flake evidence |
| [#136](https://github.com/KaiaK808/bureau/pull/136) | `d401b62` | Codex gate-duration warning |
| [#138](https://github.com/KaiaK808/bureau/pull/138) | `9e182d6` | Implementation sibling-defect search |
| [#141](https://github.com/KaiaK808/bureau/pull/141) | `8d6827f` | Changelog fragments and release collection |
| [#142](https://github.com/KaiaK808/bureau/pull/142) | `68f0142` | Disable receive-time Git collection in fixtures |
| [#137](https://github.com/KaiaK808/bureau/pull/137) | `fb6430f` | CI artifact action 7.0.2 |
| [#143](https://github.com/KaiaK808/bureau/pull/143) | `fa91be3` | Inherited signal dispositions in tests |
| [#144](https://github.com/KaiaK808/bureau/pull/144) | `7c6d8c3` | Evidence-lock and artifact-cleanup wait budgets |
| [#145](https://github.com/KaiaK808/bureau/pull/145) | `55a5ad7` | Provider login timeout retry |
| [#146](https://github.com/KaiaK808/bureau/pull/146) | `afee181` | Advisory fail-before-pass evidence |
| [#147](https://github.com/KaiaK808/bureau/pull/147) | `ce0815f` | Advisory test integrity evidence |
| [#148](https://github.com/KaiaK808/bureau/pull/148) | `ecce3e2` | Overlap inventory code-span filtering |
| [#149](https://github.com/KaiaK808/bureau/pull/149) | `9d7dad1` | Nonblocking review questions |
| [#150](https://github.com/KaiaK808/bureau/pull/150) | `bd2be2b` | Hostile-input corpus and agent-configuration annotations |
| [#151](https://github.com/KaiaK808/bureau/pull/151) | `a718287` | Exact-commit approval accident guard |
| [#152](https://github.com/KaiaK808/bureau/pull/152) | `e3d355d` | Advisory task completion evidence |
| [#153](https://github.com/KaiaK808/bureau/pull/153) | `a8e09dc` | Remote-suite check D SSH-startup test fix |
| [#154](https://github.com/KaiaK808/bureau/pull/154) | `6205c6d` | Stored Git credential helpers off by default |
| [#155](https://github.com/KaiaK808/bureau/pull/155) | `92b449e` | Secrets file outside the checkout |
| [#156](https://github.com/KaiaK808/bureau/pull/156) | `8af867d` | Impeccable skill 4.9.0 / engine 0.1.23 pins |

The release branch's own validation remains to be run by the maintainer:

| Release check | Result / reference |
|---|---|
| Independent release review | TBD |
| macOS gate suite and tested ref | TBD |
| Remote gate suite and tested ref | TBD |
| Release CI jobs, runs and results | TBD |
| Final candidate/tag tested-tree comparison | TBD |
| Manual live injection sample and adopter qualification | TBD |

Before publication, record the exact tested refs and results, and run [release tree verification](releases.md#verify-the-tested-release-trees). The hostile-input corpus uses deterministic fakes; its manual live release-checklist sample remains a separate check. This release record creates no tag or release.

## Known limitations

- A stored `gh` login remains available to branch-controlled code through `gh auth token`, `~/.config/gh/hosts.yml` or the keychain. No change in this release covers it. This conclusion was argued from the code and was not tested.
- Moving the secrets file outside the checkout blocks only the conventional path. Processes with the user's permissions can still discover and read it.
- Human approval depends on default-branch installation and branch protection requiring the status. Deliberate status forgery and self-approval with a shared login remain possible; the feature is an accident guard.
- Advisory QA evidence has bounded coverage. Worktree-linked repositories are not measured for fail-before-pass; static integrity checks have lexical/import limits; task-path changes do not prove correct completion.
- Pytest long tracebacks showing a test class instance named after a timeout, sandbox or teardown can suppress the assertion rerun. This fails safe: no rerun, QA stays red. Unfamiliar output also follows the ordinary failure path.
- Several inherited tests remain load-sensitive. The signal-reset change addresses inherited ignored signals; it does not establish that every timing race is resolved.
- Live provider behavior, adopter drift comparisons and representative adoption of the new checks remain unverified. Impeccable adopting-repository pilots have not run. Fragment-level tests and hostile-input fakes cannot establish live resistance to every prompt injection.
- The release branch's own macOS and remote suites, CI, tested refs and final tree comparisons remain `TBD` until the maintainer runs them.
