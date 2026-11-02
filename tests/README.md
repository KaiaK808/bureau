# bureau-init test harness

Shell-based smoke tests for the pipeline scripts under `templates/scripts/`.

## Running

```bash
bash tests/run.sh
```

Required tooling: `bash`, `git`, `jq`, `timeout` (GNU coreutils). On macOS, install with `brew install coreutils` (provides `timeout`; `bash` and `jq` are usually present).

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
