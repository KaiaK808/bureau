# Bureau v3.9.0 — Semgrep and Trivy evidence, Python bytecode fix

**Release preparation · 2026-11-20 · source tag `v3.9.0`.** v3.9.0 is a minor release on [v3.8.0](release-notes-v3.8.0.md). Tagging and GitHub Release publication remain separate maintainer operations.

The release includes [#158](https://github.com/KaiaK808/bureau/pull/158), [#159](https://github.com/KaiaK808/bureau/pull/159), [#160](https://github.com/KaiaK808/bureau/pull/160) and [#161](https://github.com/KaiaK808/bureau/pull/161) in first-parent merge order. All changes are merged. The [changelog](../CHANGELOG.md#390---2026-11-20) collects the four release fragments.

## What changed since v3.8.0

### Optional Semgrep CE evidence in code review

Independent specialists finish their first pass before all review roles receive the bounded Semgrep projection in the late step. Semgrep scans offline under network denial. Bureau loads the bundled `bureau-semgrep-rules.yml` from the installed runtime and reads any configured extra rules and `.bureau/semgrep-ignore.json` suppressions from the pinned PR base. Bureau verifies a private copy of the Semgrep packages against a shipped manifest before probing the interpreter and re-hashes the copy before each scan side. Once Bureau has loaded the rules and validated the base suppressions, an empty set of eligible files on both sides passes as `not_applicable` without checking scanner prerequisites. A configured `extra_rules` file missing from the pinned base yields `not_measured` even with an empty eligible set.

In advisory mode, Bureau reports the evidence and skips the scanner's verdict floor. On a fresh review, any Semgrep projection, including `not_applicable`, triggers one late provider call per specialist after the independent passes. Malformed supplemental output ends the stage with exit 22. Blocking mode holds approval on new findings at or above the configured severity and on unavailable or incomplete measurements. Existing BLOCK verdicts and review caps still apply. With blocking enabled, Bureau's automatic merge pipeline rechecks the receipt against the head and base.

### Optional Trivy evidence in QA

Trivy scans dependencies and configuration on the final QA head, including repositories without a test harness. This release excludes image scanning. The stage runs a verified private copy of the binary offline under network denial, checks the external database cache for integrity and freshness, and uses Trivy's embedded configuration checks. Database downloads occur in a separate operator refresh. Expiring exceptions come from the pinned base and the scan ignores head-native suppressions.

With Trivy enabled, live QA without a test harness runs `git push origin HEAD` even when there is no QA merge to publish. A rejected push exits 18 before Trivy evidence or ticket routing, including in advisory mode. A failed PR lookup or base fetch while pinning the scan base also exits 18 before scanning. Once the scan step runs, advisory mode reports the result and QA continues. Blocking mode holds for a human on qualifying new findings, measurement gaps or coverage gaps without an active exception. After a failed base scan, unclassified head findings still count toward the severity floor, even if every coverage gap has a file exception. With Trivy enabled in blocking mode, Bureau's automatic merge pipeline requires a valid final-head receipt even without a QA state. With `agents.merge_mode: "manual"` and no QA state, a human can merge without a Trivy receipt.

### Scanner receipts and merge freshness

Both scanners write immutable evidence attempts and add a retention reference for each PR that produces or reuses one. Open consuming PRs keep referenced attempts beyond the newest 20. An unknown PR inventory keeps all referenced attempts.

For scanners enabled in blocking mode, Bureau's automatic merge pipeline checks the receipts and fetches the base again before the existing just-in-time merge gates. A final read checks the PR base name and live branch tip before the head-pinned merge. If either changes, the merge retries with exit 2 even when the final gates pass. The ticket checkpoint remains the last step before merge.

### Python bytecode in stage commits

Stages suppress installed Bureau helper bytecode and exclude the Bureau cache from shell stage commits. The installer adds the literal `scripts/__pycache__/` ignore rule and preserves existing rules. Staging and doctor match the exact path components and existing aliases of the same directory. A separate application's `Scripts/__pycache__/` remains eligible for ordinary staging on a case-sensitive filesystem.

### v3.8.0 publication record

The source-only record captures the published tag and merge commit, GitHub Release, CI results including one tag rerun, tested-tree comparison and decision to skip the manual live injection sample. See the [v3.8.0 release notes](release-notes-v3.8.0.md).

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-semgrep.py`, `bureau-semgrep.sh`, `bureau-semgrep-rules.yml`, `bureau-semgrep-lock.txt`, `bureau-semgrep-manifest-*.json` | Complete scripts resync for code review, the blocking merge recheck, and the locked scanner packages and platform manifests. |
| `templates/scripts/bureau-trivy.py`, `bureau-trivy.sh`, `bureau-trivy-pins.json` | Complete scripts resync for QA, the blocking merge recheck and binary pins. |
| `templates/scripts/bureau-scan.py`, `bureau-scan-nonet.sb` | Shared scan and receipt support and the macOS network-denial profile. |
| `scripts/semgrep_pin.py` | Source-only generator for Semgrep wheel manifests. |
| `tests/test_semgrep*.sh`, `tests/test_trivy*.sh`, `tests/test_python_bytecode.sh` | Repository-only regression entry points. |
| `docs/release-notes-v3.8.0.md` | Unchanged archive of the previous release notes. |

## Configuration and exit-code changes

Semgrep runs in the background review pipeline and Trivy runs in the background QA pipeline. The app's `prepare`/`finish` flow runs neither scanner, so enabling either in advisory or blocking mode has no effect there. Semgrep and Trivy are off until enabled. Set the appropriate `enabled` key to boolean `true` and select `advisory` for the first adoption. Blocking is a separate opt-in through `mode`. See [Semgrep configuration](configuration.md#reviewsemgrep) and [Trivy configuration](configuration.md#qatrivy).

| Key | Default / action | Effect |
|---|---|---|
| `review.semgrep.enabled`, `review.semgrep.mode` | `false`, `advisory` | Enables offline Semgrep CE in background review. Blocking also rechecks before merge. |
| `BUREAU_SEMGREP_VENV` | Operator-supplied venv or shared-state fallback | Selects the pinned Semgrep environment for the matching platform and interpreter. |
| `qa.trivy.enabled`, `qa.trivy.mode` | `false`, `advisory` | Enables offline Trivy in background QA. Blocking also rechecks before merge. |
| `BUREAU_TRIVY_BIN`, `qa.trivy.cache_dir` | Operator-supplied binary or shared-state fallback, plus an external cache directory | Supplies the pinned executable and its separately refreshed databases. |
| `qa.trivy.image_archive_command` | Remove from existing configurations | Runtime and doctor reject this key, including with `null` or with Trivy disabled. |

A shell stage that reaches its commit step with a staged Bureau cache change, including the cleanup deletion, stops with exit 24 and leaves the index untouched. See [exit codes](exit-codes.md).

## Upgrade an existing installation

**From v3.8.0, once v3.9.0 is published.** Stop every shepherd, queue loop, dispatcher and supervisor. Preserve unfinished work, local commits, customized assets, instructions, the Spec Kit constitution and shared runtime records. Compare installed script drift and reconcile local incident fixes before replacing assets. Select the published `v3.9.0` tag and refresh discovery. Save the [pre-upgrade inventory and private backup](migration.md#before-changing-an-adopting-repository), including `.bureau-install.json` and `.gitignore` in the backup. Use the [v3.9.0 scripts-only preview and apply commands](migration.md#from-v380) in every adopting repository with its existing target selection. Resolve conflicts before applying. `--update` edits configuration only.

To set up Semgrep for background review, create a standard-library venv with the supported CPython and install Semgrep CE **1.183.0** from the shipped wheel-only hash lock. Set `BUREAU_SEMGREP_VENV` or use the shared-state fallback. Qualify network isolation on the deployment host, then enable `review.semgrep.enabled` with `review.semgrep.mode` set to `advisory`. See [Semgrep installation](configuration.md#reviewsemgrep).

To set up Trivy for background QA, provide the verified **0.76.1** binary. Use the shared-state fallback or export the absolute binary path as `BUREAU_TRIVY_BIN` in the refresh shell and the stage launcher's environment before restarting drivers. Bureau's `.env` loader ignores this key. Select an absolute `qa.trivy.cache_dir` outside the repository and refresh it separately while it is empty or already Bureau-managed. Qualify host network isolation, then enable `qa.trivy.enabled` with `qa.trivy.mode` set to `advisory`. See [Trivy installation and refresh](configuration.md#qatrivy).

In the resynced repository, run `python3 scripts/bureau-doctor.py --mode background` for a background installation or `python3 scripts/bureau-doctor.py --mode app` for an app-only installation. Reconcile holds. If doctor reports a tracked `scripts/__pycache__/`, run its printed cleanup command for the matched literal directories once and commit the removal. The ignore entry takes effect for untracked files. After any scanner setup, restart all drivers together so they load the new scripts. If you paused dispatch for the upgrade, run `python3 scripts/bureau-runtime.py unpause` after the restart. See [From v3.8.0](migration.md#from-v380) and [tracked Python bytecode](troubleshooting.md#tracked-python-bytecode).

**From earlier versions.** Apply the migration guide's cumulative steps through [v3.8](migration.md#upgrade-to-v38) first. The v3.8.0 publication record itself needs only a source checkout update.

## Compatibility and rollback

Configuration schema versions 1 and 2, the v3 exit vocabulary, Bash 3.2+, Python 3.9+, Node 18+ for the shared scheduler, Spec Kit 1.2.2 and the current-workspace prepare/finish protocol apply. The scanners have their own platform prerequisites when enabled.

| Scanner | Shipped platform pins |
|---|---|
| Semgrep CE 1.183.0 | macOS arm64 with CPython 3.14 and Linux x86_64 with CPython 3.12. An eligible scan with another interpreter reports unavailable. |
| Trivy 0.76.1 | Linux x86_64 and macOS arm64 binary pins. |

For rollback, stop drivers and reconcile unfinished writers, local commits and holds. Follow the [restore procedure](migration.md#restore-the-pre-upgrade-installation) to restore the complete v3.8.0 asset set, compatible configuration and installer records, and remove confirmed scanner additions as described there. Retain runtime and scanner records for inspection. Prior commits, pushes, comments, labels and state moves persist through rollback.

## Validation

The merged content baseline is main `b65f8cf`. The merged changes follow first-parent merge order.

| PR | Merge commit | Change |
|---|---|---|
| [#158](https://github.com/KaiaK808/bureau/pull/158) | `917214e` | v3.8.0 publication record |
| [#159](https://github.com/KaiaK808/bureau/pull/159) | `0e1dce2` | Optional Semgrep CE review evidence and blocking merge recheck |
| [#160](https://github.com/KaiaK808/bureau/pull/160) | `10d4c60` | Optional Trivy QA evidence from a verified pinned binary |
| [#161](https://github.com/KaiaK808/bureau/pull/161) | `b65f8cf` | Keep installed Bureau bytecode out of shell stage commits |

The fragments record offline Semgrep fixtures and negative controls for the private runtime, review, receipt reuse and merge freshness. An earlier maintainer run of Semgrep 1.183.0 on macOS arm64 under network denial covered every rule fixture, a hostile `.semgrepignore` and inline `nosemgrep`.

Offline Trivy tests used a fake binary to cover the scanner boundary, cache, coverage and parser controls, QA placement, the disabled baseline and merge races. The bytecode fix used local fixtures for stage entry points, review evidence, provider gates, filesystem-specific directory cases and a tracked Latin-1 shell POST, without a model or network. See the [fragment validation in the changelog](../CHANGELOG.md#390---2026-11-20).

The release branch's own validation remains to be run by the maintainer:

| Release check | Result / reference |
|---|---|
| Independent release review | TBD |
| macOS gate suite and tested ref | TBD |
| Remote gate suite and tested ref | TBD |
| Release CI jobs, runs and results | TBD |
| Final candidate/tag tested-tree comparison | TBD |
| Manual live injection sample and adopter qualification | TBD |

Before publication, record the exact tested refs and results and run [release tree verification](releases.md#verify-the-tested-release-trees). The manual live injection sample remains a separate release-checklist item.

## Known limitations

- Real-engine validation of Semgrep's private-copy boundary, Linux isolation on deployment hosts and adopting-repository qualification remain pending.
- The separate GitHub-hosted Ubuntu Trivy job remains pending after offline implementation. It needs network access to download the pinned archive and refresh a cache. It verifies the archive and extracted-binary hashes before refresh and qualifies filesystem flags under `unshare`. Pinned-engine flag behavior, platform isolation and adopter compatibility still need a networked CI or maintainer run.
- Trivy reports reduced coverage for lockfile forms its consistency parsers reject or for non-canonical manifest and lockfile names. In blocking mode, each affected file needs an active base coverage exception to waive its gap.
- Inline-ignore stripping can truncate an IaC line at `//` inside a quoted string before a trailing `trivy:ignore:` comment, so Trivy can receive altered configuration text.
- Bureau's Yarn consistency parser rejects indented comments and skips other indented body lines, so unsupported syntax in those lines can pass without a Bureau coverage warning.
- The writer of the historical adopter cache remains unknown.
- The release branch's own macOS and remote suites, CI, tested refs and final tree comparisons remain `TBD` until the maintainer runs them.
