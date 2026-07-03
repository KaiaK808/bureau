# bureau-init test harness

Shell-based smoke tests for the pipeline scripts under `templates/scripts/`.

## Running

```bash
bash tests/run.sh
```

Required tooling: `bash`, `git`, `jq`, `timeout` (GNU coreutils). On macOS, install with `brew install coreutils` (provides `timeout`; `bash` and `jq` are usually present).

Each test runs in an isolated `mktemp -d` sandbox with a real `git init`. Linear/GitHub helpers are stubbed via `tests/lib/stub-bureau-config.sh` (substituted for the real `bureau-config.sh` when the pipeline sources it). `$CLAUDE` is replaced with `tests/lib/fake_claude.sh`, which emits fixture JSON from `tests/fixtures/` and optionally creates real git commits to exercise the commit-count signal in the stuck detector.

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
