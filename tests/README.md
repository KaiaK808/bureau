# bureau-init test harness

Shell-based smoke tests for the pipeline scripts under `templates/scripts/`.

## Offline scanner coverage

`bash tests/test_semgrep.sh` checks the shared scanner boundary, synthetic-wheel manifest generation, explicit environment/argv, injected network isolation, bytecode tampering, occurrence-aware newness, base-only policy, durable receipts and projection bounds. Each probe and scan uses its own shadow venv: tests check its exact config, executable directory and linked site-packages, unverified helpers beside the executable and through PATH, duplicate operator system-package settings, manifest verification before the probe, and invalidation of receipts from the old execution boundary. It builds its own pip-free fake venv and manifest under `tempfile`; no downloads or provider credentials are needed.

`bash tests/test_semgrep_pipeline.sh` uses the real review and merge stages with fake providers and GitHub. It checks the frozen disabled prompts/comment/verdict and reuse key, all three late-role receipts, the ordered blocking floor, reuse invalidation and final-head remeasurement/refusal. Merge races cover head/base changes during a scan, a retarget or CI change during the last base fetch, stale blocking/failed results retrying without needs-human, gate marks bound to the scanned head, and the single base-name read followed by a head-pinned merge after the final gates. Disabled and advisory merge call logs must match the frozen disabled baseline. Tests inject the isolation probe only in temporary installed scripts; production has no test override in repository settings or the CLI. The frozen disabled fixture was captured from the unchanged parent runtime, with only fixture paths and Git/run IDs normalized.

Both entrypoints are discovered by `tests/run.sh` and run in every full shard on macOS and Ubuntu; they are not in the focused macOS pull-request manifest, which keeps its runner-minute budget. Their sharding estimates use the runner's fallback until hosted timing records are available. The optional real Semgrep test requires `BUREAU_SEMGREP_VENV` and a GitHub-hosted Ubuntu runner; otherwise unittest reports a skip. It scans every shipped rule fixture through the production invocation and requires exactly the annotated `ruleid` lines to match and no `ok` line, then runs one scan through real network isolation. On macOS Python inserts `__CF_USER_TEXT_ENCODING` after exec even under `-I -S`: tests separately assert the exact environment passed to every process and the fake tool's environment, allowing only that OS-generated, non-inherited addition.

## Running

```bash
bash tests/run.sh
```

Direct calls run once. Hosted CI never retries, and its workflow is unchanged.
Use the local full-suite entrypoint below for a full local validation; on shared
machines follow the maintainer's scheduled-suite limits.

## Local full suite and serial reruns

```bash
python3 tests/run_suite.py --out "$SUITE_OUTPUT" --tag sample --project sample --shards 2
```

Set `SUITE_OUTPUT` to an output directory outside the worktree. Start with a clean
checkout and use a fresh tag for that output directory. The orchestrator refuses
even empty `CI`, `GITHUB_ACTIONS`, `BUREAU_TEST_SHARD` or `BUREAU_TEST_MANIFEST`
variables, with no bypass. It freezes the complete partition from `run.sh --list`,
runs manifests with the shard selector unset, and waits for every shard to end.
Only then can it rerun failed files, once each and serially. A direct `run.sh`
invocation never retries. It does not restart the full suite.

`FAIL` and cleanly terminated `TIMEOUT` files qualify only with complete,
consistent per-file records and successful process-group cleanup. A crashed or
interrupted shard, missing/malformed/duplicate records, a vanished selected file,
failed cleanup or a nonzero exit without a failed file prevents reruns and green.
The entire failed set must fit both limits: five files and 3600 seconds of
expected runtime from the current OS's weights/fallbacks, rather than deadlines.
Otherwise no rerun starts. Deadlines retain the original rule and any explicit
`BUREAU_TEST_FILE_TIMEOUT`. Stop at the first rerun that does not pass; the result
lists the remaining files as not attempted. HEAD and worktree cleanliness are
checked at the start, after shards, after each rerun and before green publication.

Output includes `suite-TAG-s1.log`, `suite-TAG-s2.log` (with default two shards),
`suite-TAG-rerun-<file>.log` and atomic `suite-TAG-result.json`. Schema version 1
records SHA, tag/project, shard selections, attempts/deadlines/cleanup, first-pass
failures, rerun paths, recovered files and files not attempted. States/exits are
`passed` or `passed_after_serial_rerun` (0), `failed` (1), `invalid` or usage/I/O
error (2), and `cancelled` (130). SIGINT, SIGTERM and SIGHUP stop new work, signal
active runners for their own cleanup, and wait up to 50 seconds before killing a
stuck runner, with a further three-second reap deadline per forced kill.
Finalization blocks those signals and checks pending cancellation again after
fsync, immediately before replacing the result. The green replace is the commit
point: a signal held across it cannot change the receipt or its exit code.
Failed publication removes any old result and attempts a non-green receipt;
if that also fails, stderr reports the error and the exit is 2 (130 when
cancelled). A missing result never means green.

A recovered file has reason `failed_in_parallel_passed_serially`: an **observed
file-level flake**, not a proven cause. Detached processes that left their test's
process group and persistent fixtures are not tracked. External wrappers must
keep exclusive suite/worktree admission throughout execution and publication;
these snapshot checks cannot detect transient edits or unrelated concurrent suites.

`--state-dir DIR` overrides `$XDG_STATE_HOME/bureau/suites/KEY`, defaulting to
`~/.local/state/bureau/suites/KEY` when XDG_STATE_HOME is unset. State also stays
outside the worktree. `flakes.jsonl` is append-only, locked with `fcntl.flock`,
flushed and fsynced. Entries identify the file (`test_name` equals the filename,
`identity_scope: file`), SHA, UTC timestamp and run tag. Replaying the same
SHA/tag/file does not append twice. Distinct SHAs count once using their earliest
observation in the episode; repeats cannot refresh the seven-day window. The
third SHA within the inclusive rolling seven days creates one
`proposals/<id>.json`; an open proposal suppresses another for that file.

```bash
python3 tests/run_suite.py --state-dir "$SUITE_STATE" --project sample --resolve-proposal ID
```

Resolution starts a new counting episode using only later observations. Under
the lock, a non-empty ledger without a trailing newline or a malformed proposal
is rejected before append or resolution, without automatic repair. Proposals
require a matching lowercase hex ID, file, open/resolved status, non-empty SHA
and run-tag lists, creation time and a resolution time only when resolved.
Ledger or proposal validation/persistence errors prevent recovered green. The code calls no Linear,
GitHub or network service. A maintainer wrapper reads the proposal outbox and must
validate schema, tag/project, expected SHA, complete attempts and the state before
accepting local green; wrapper/desk adoption is separate from this checkout and
cannot override red hosted checks.

`BUREAU_TEST_RESULTS=path` optionally makes `run_test_files.py` append one JSON
line per selection: `file`, `outcome` (`PASS`, `FAIL`, `TIMEOUT`, `MISSING`),
`cleanup_ok`, `elapsed_seconds` and `deadline`. It must use a separate file for
each runner. Without it, leaf output, exits and missing-file skipping stay unchanged.
`test_run_suite.sh` uses only tiny private temporary Git repositories and real
copies of the selection/runner scripts, with no provider or real state access.

## Leaf runner details

Required tooling: `bash`, `git`, `jq`, `timeout` (GNU coreutils). On macOS, install with `brew install coreutils` (provides `timeout`; `bash` and `jq` are usually present).

The runner also requires Python 3.9+. Each file has its own process group and a
deadline of `max(600, 3 * estimated_seconds)` using the current OS's shard weight
and the same fallback estimates as sharding. `BUREAU_TEST_FILE_TIMEOUT` overrides
all file deadlines with a positive integer in seconds. Expiry prints `TIMEOUT`,
sends TERM then KILL after one second, counts a failure and continues. Background
group members are stopped even after a successful test.

Test files start with default SIGINT, SIGQUIT and SIGHUP dispositions, so starting
the suite in the background or under `nohup` no longer changes results. The
runner's own Ctrl-C handling without `BUREAU_TEST_RESULTS` is unchanged: it exits
130 and stops the active test group when SIGINT is handled, and still ignores
SIGINT if it inherited that setting. The optional results path handles INT, TERM
and HUP explicitly, defers interruption while registering a new child and during
process-group cleanup, and ignores repeated signals while the first
interruption's cleanup finishes.

Both `tests/run.sh` and direct `tests/run_test_files.py` runs set
`PYTHONDONTWRITEBYTECODE=1` for every test, keeping template imports free of
bytecode caches. The internal-reference guard scans Git-tracked and non-ignored
new files; ignored caches are excluded, while tracked files remain covered even
when an ignore rule matches them.

`BUREAU_TEST_MANIFEST=tests/macos-manifest.txt` selects the 56 focused macOS tests;
unset or empty selects every entrypoint. Manifest paths resolve from the invoking
directory. File names may have trailing `#` reason comments; blank/comment lines
are ignored, unknown names and empty manifests fail. Selection precedes weighted
sharding; `--list` shows the resulting files without running them. See
[contributor CI coverage](../CONTRIBUTING.md#ci-coverage) for focused/full events
and the rule for adding platform tests. Current weights estimate 89.98 macOS
runner-minutes, including four minutes of setup across two jobs.

Set `BUREAU_TEST_TIMINGS=timings.tsv` to record monotonic elapsed seconds (including
group cleanup), outcome (`PASS`, `FAIL`, `TIMEOUT`) and OS (`Linux`, `macOS`). The
path is relative to the invoking directory; each run replaces the file. CI uploads
one timing artifact per OS/shard even on failure. After checking that the source
jobs succeeded, merge their files with
`python3 tests/update_shard_weights.py path/to/timings.tsv [more-files.tsv ...]`.
The updater rejects files containing failures/timeouts, rounds the slowest observed
successful duration up to whole seconds and changes only the matching OS column.
Unmeasured files without data and existing comments stay intact; new rows retain
a dash for the other OS. An interrupted run may have only partial timings, so its
artifact must not be used even if all recorded rows passed.

Each test runs in an isolated `mktemp -d` sandbox with a real `git init`. Linear/GitHub helpers are stubbed via `tests/lib/stub-bureau-config.sh` (substituted for the real `bureau-config.sh` when the pipeline sources it). `$CLAUDE` is replaced with `tests/lib/fake_claude.sh`, which emits fixture JSON from `tests/fixtures/` and optionally creates real git commits to exercise the commit-count signal in the stuck detector.

## Mutation controls

`mutation_checks.py` requires the unchanged test to pass at the private mutation
paths before accepting any mutation failure or error. Both executions use the
same command, environment, inventory, resolved cwd and `TMPDIR`. Each pair owns
a random temporary root from the platform temp directory; separate pairs never
share one. Historical controls overlay the requested Git archive onto a complete
current private inventory and restore current bytes there for the baseline.

Every execution has a positive finite deadline (300 seconds by default); `None`,
infinity and nonpositive values are rejected before launch. Harnesses can pass a
different finite `timeout`; conflict controls expose
`BUREAU_CONFLICT_CONTROL_TIMEOUT` and `BUREAU_MODEL_CONFLICT_CONTROL_TIMEOUT`
(seconds, default 300) for slower machines. Timeout cleanup uses a one-second
bounded `ps -axo pid=,command=` on macOS/Linux, plus `/proc/<pid>/cwd` on Linux.
Only paths within that unique root match. Cleanup sends TERM, then KILL after a
one-second grace, allows three seconds for reaping and bounds the final output
drain to one second. Linux adopts orphans for reaping. Process inspection must
work before a fixture can start; unavailable inspection fails the harness.

The review-base/review-convergence lifecycles, shepherd outcome and judged
overlap-readiness integration tests use a
135-second pipeline deadline: three 45-second command budgets for setup/re-entry,
the judged stage and routing/cleanup. On macOS CI the previous single 45-second
budget expired after the review reported BLOCK/25. A readiness subcase timeout
also skipped its fixture cleanup, leaving reservations that correctly blocked
later subcases. These tests assert behavior, not performance;
exit codes, outcome counts, paid-call counts and routing remain exact. Direct
overlap commands retain their 45-second budget. After auditing every recorded
transport PID, survivor assertions allow five additional seconds total for
teardown; live survivors and unavailable process inspection still fail.
The two review fixtures also use the shared fixture supervisor: it drains both
pipes and joins descendants before assertions return or fixture directories are
removed. Process inspection must be available, including for detached children.

Baseline inventory audit (all locations below are private, resolved temp paths):

| Harness | Baseline location before mutation |
| --- | --- |
| `acceptance_insights_negative_controls.py` | Same `templates/scripts/` copy, with the copied shell tests and companion fixtures unchanged |
| `test_bounded_lessons_negative_control.sh` | Same `scripts/` inventory |
| `conflicts_negative_controls.py` | Same `scripts/` copy; historical/withdrawal/worker controls restore current bytes in the same `templates/scripts/` overlay |
| `dispatcher_negative_control.py` | Same fixture instance, repo, service inventory and worker paths; joined/audited baseline state is restored before mutation |
| `evidence_negative_controls.py` | Same selected scripts at the private root, including companion modules |
| `failure_insights_negative_controls.py` | Same `templates/scripts/` inventory |
| `file_overlap_negative_control.py` | Same `scripts/` inventory |
| `model_conflict_negative_controls.py` | Same `scripts/` copy; historical cases restore current bytes in the same `templates/scripts/` overlay |
| `review_convergence_negative_controls.py` | Same per-control `.sh` file and environment |
| `silence_watchdog_negative_controls.py` | Same `scripts/` inventory |
| `silence_watchdog_fix1_negative_controls.py` | Same per-control `scripts/` inventory |
| `spec_resume_negative_controls.py` | Same `scripts/` inventory |
| `ticket_kill_switch_test.py` (embedded control) | Same fixture, script, worker path and environment; joined/audited baseline state is restored before mutation |

## Adding a test

1. Drop a fixture under `tests/fixtures/` if you need a new Claude response shape.
2. Add `tests/test_<name>.sh` that:
   ```bash
   source "$(dirname "$0")/lib/harness.sh"
   sandbox_init "EXP-XXX" "test-branch"
   export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/your_fixture.txt"
   run_implement_pipeline
   assert_eq 0 "$LAST_RC" "exit code"
   assert_calls_include 'pattern' "label"
   ```
3. `bash tests/run.sh` to verify.

## When to run

Before committing changes to `templates/scripts/` — especially anything touching the retry loop, status parsing, or escalation log. CI runs the same suite on push (see `.github/workflows/test.yml`).

`bash tests/test_trivy.sh` checks verified private binary bytes (including digest/size mismatch, symlinks and special files), exact allowlist/argv and per-call network wrapping, external-cache copying/digests and UpdatedAt freshness, explicit networked refresh, embedded checks, inventory/lockfile consistency, pnpm sibling metadata and nested-table exclusions, TOML fallback table scope, package-lock/Yarn metadata controls, base-only expiring exceptions, occurrence-aware identities with LF-only cause lines, durable receipts and the projection bound. Trivy v1 scans dependencies and configuration only; image scanning is not part of this release, as every projection states. Tests reject the removed `qa.trivy.image_archive_command` key in runtime and doctor validation, even while disabled, before any command can run. Fixtures and fake pins are private temporary files; no installed scanner or download is needed. `BUREAU_TRIVY_REAL_TEST=1`, `BUREAU_TRIVY_BIN` and `BUREAU_TRIVY_CACHE` enable the separate real-binary qualification, which verifies the shipped pin and exercises the central flag table on lockfile and Dockerfile filesystem fixtures under real network denial.

`bash tests/test_trivy_pipeline.sh` runs the installed QA/merge scripts with fake providers/GitHub, real local Git and a pinned fake scanner. It checks the frozen disabled QA prompts/comments/routing from the unchanged parent runtime, QA commit/push placement and receipt-hash archival, no-harness scans, blocking holds, absent QA-state merge coverage, final-head reuse/invalidation, stale passing/blocking/failed scan retries and scanned-head marks. Merge tests also cover retarget/base/CI races around the last fetch and final gates, the single post-gate base-name read, every merge strategy's last-step ticket checkpoint, and disabled/advisory GitHub/Git logs against the existing frozen baseline. Guard-removal controls demonstrate the unsafe merge/QA handoff that the receipt requirements prevent. Both Trivy suites are discovered by the ordinary local and full runner and sharded like every other entrypoint. The focused macOS pull-request manifest and shard budget are unchanged.
