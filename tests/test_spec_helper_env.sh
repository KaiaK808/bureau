#!/bin/bash
# Observe the actual child at both feature-helper calls in the spec stage.
# Keep the pinned helper's real JSON output and failure handling in the path.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
trap 'if [ -d "${SANDBOX:-}" ]; then teardown; fi' EXIT

fail() {
  echo "FAIL $* (rc=$LAST_RC)" >&2
  printf '%s\n' "$LAST_STDERR" "$LAST_STDOUT" | tail -16 >&2
  exit 1
}

setup() {
  sandbox_init EXP-910 001-test-branch
  git -C "$SANDBOX" checkout -q --detach main
  printf '{"repo":{"untrusted_env":"%s"}}\n' "$1" > "$SANDBOX/.bureau.json"
  export BUREAU_CONFIG="$SANDBOX/.bureau.json" BUREAU_ENV_FILE="$SANDBOX/.env"
  export BUREAU_STUB_ISSUE_STATE=Triage
  export FAKE_CLAUDE_BIN="$LIB_DIR/pr4-fake-specify.sh" FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"
  export PR4_SPECIFY_ACTION=leave
  unset BUREAU_CALLER_STOP FAKE_CLAUDE_COMMIT_ON_ITERS PR4_SPECIFY_DIR PR4_SPECIFY_RAW
  # Dummy credentials only; the stage needs them, while branch code must not.
  export GH_TOKEN=ghp_spec_helper_probe GITHUB_TOKEN=ghs_spec_helper_probe
  export GH_ENTERPRISE_TOKEN=ghe_spec_helper_probe GITHUB_ENTERPRISE_TOKEN=ghec_spec_helper_probe
  export REMOTE_URL="https://x-access-token:$GH_TOKEN@example.invalid/repo.git"
  export API_KEY=test-key OPERATOR_TOOL_VAR=ordinary-helper-setting
  export HERDR_PANE_ID=helper-pane HERDR_FUTURE_VAR=helper-host-setting
  local helpers="$SANDBOX/.specify/scripts/bash"
  mv "$helpers/create-new-feature.sh" "$helpers/create-new-feature.real.sh"
  cat > "$helpers/create-new-feature.sh" <<'HELPER'
#!/bin/bash
set -euo pipefail
helpers=$(cd "$(dirname "$0")" && pwd -P)
phase=create
if [ "${1:-}" = --dry-run ]; then phase=dry-run; fi
/usr/bin/env > "$helpers/$phase.env"
bash "$helpers/create-new-feature.real.sh" "$@"
# Valid JSON on stdout must not hide the helper's nonzero status.
if [ -f "$helpers/fail-$phase" ]; then exit 73; fi
HELPER
}

check_env() {
  local phase="$1" mode="$2" name
  local file="$SANDBOX/.specify/scripts/bash/$phase.env"
  [ -s "$file" ] || fail "$mode $phase helper did not record its environment"
  for name in LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID \
    GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN API_KEY REMOTE_URL \
    BUREAU_CONFIG BUREAU_ENV_FILE HERDR_PANE_ID HERDR_FUTURE_VAR; do
    if grep -q "^$name=" "$file"; then fail "$mode $phase helper inherited $name"; fi
  done
  if grep -qE 'test-key|[a-z]+_spec_helper_probe' "$file"; then
    fail "$mode $phase helper inherited a credential through a carrier"
  fi
  grep -qxF "PATH=$PATH" "$file" || fail "$mode $phase helper PATH changed"
  grep -qxF "HOME=$HOME" "$file" || fail "$mode $phase helper HOME changed"
  if [ "$mode" = clean ]; then
    if grep -q '^OPERATOR_TOOL_VAR=' "$file"; then fail "$mode $phase helper kept an ordinary variable"; fi
  else
    grep -qx 'OPERATOR_TOOL_VAR=ordinary-helper-setting' "$file" || fail "$mode $phase helper lost an ordinary variable"
  fi
}

for mode in clean default; do
  setup "$mode"
  run_pipeline spec-pipeline.sh EXP-910
  [ "$LAST_RC" = 0 ] || fail "$mode helper JSON did not reach the stage unchanged"
  for phase in dry-run create; do
    check_env "$phase" "$mode"
    echo "PASS $mode $phase helper child environment and JSON stdout"
  done
  teardown
done

for phase in dry-run create; do
  setup clean
  touch "$SANDBOX/.specify/scripts/bash/fail-$phase"
  run_pipeline spec-pipeline.sh EXP-910
  [ "$LAST_RC" = 11 ] || fail "$phase helper failure did not map to exit 11"
  check_env "$phase" clean
  grep -q $'^move_issue\tEXP-910\tstate-triage$' "$SANDBOX/calls.log" || fail "$phase helper failure did not recover to Triage"
  [ ! -f "$SANDBOX/fake_claude_counter" ] || fail "$phase helper failure spent model work"
  if grep -q $'^add_issue_label\t' "$SANDBOX/calls.log"; then fail "$phase helper failure added a label"; fi
  echo "PASS clean $phase helper nonzero status after JSON -> 11, Triage, no label or model work"
  teardown
done
echo 'OK test_spec_helper_env (6 scenarios; actual child environments, pinned JSON, exit handling)'
