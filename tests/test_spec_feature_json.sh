#!/bin/bash
# The shell allocates and prepares the feature before specify. A provider may
# populate it, but cannot redirect plan/tasks to an absent, stale or other feature.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP
trap 'teardown' EXIT
setup() {
  sandbox_init EXP-910 001-test-branch
  mkdir -p "$SANDBOX/specs/003-older-feature"
  echo '# older spec' > "$SANDBOX/specs/003-older-feature/spec.md"
  git -C "$SANDBOX" add specs; git -C "$SANDBOX" commit -qm 'older spec'
  git -C "$SANDBOX" push -q origin HEAD
  git -C "$SANDBOX" checkout -q --detach main
  mkdir -p "$SANDBOX/.specify"
  printf '{"feature_directory":"specs/003-older-feature"}\n' > "$SANDBOX/.specify/feature.json"
  export BUREAU_STUB_ISSUE_STATE=Triage
  export FAKE_CLAUDE_BIN="$LIB_DIR/pr4-fake-specify.sh" FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"
  export FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/prompts.log"
  unset FAKE_CLAUDE_COMMIT_ON_ITERS PR4_SPECIFY_DIR PR4_SPECIFY_RAW
}
fail() { echo "FAIL $* (rc=$LAST_RC)" >&2; printf '%s\n' "$LAST_STDERR" "$LAST_STDOUT" | tail -16 >&2; exit 1; }
for action in leave remove raw write restore; do
  setup
  export PR4_SPECIFY_ACTION="$action"
  case "$action" in
    raw) export PR4_SPECIFY_RAW='{"feature_directory": ' ;;
    write|restore) export PR4_SPECIFY_DIR=specs/003-older-feature ;;
  esac
  # restore uses a tracked stale pointer, just as main used to carry.
  if [ "$action" = restore ]; then git -C "$SANDBOX" add .specify/feature.json; git -C "$SANDBOX" commit -qm 'stale pointer'; fi
  run_pipeline spec-pipeline.sh EXP-910
  if [ "$action" = leave ]; then
    [ "$LAST_RC" = 0 ] || fail 'prepared pointer should proceed'
    grep -q 'Work on the spec in .*specs/004-exp-910/' "$SANDBOX/prompts.log" || fail 'plan/tasks lack allocated directory'
    grep -q 'already run with --number 4' "$SANDBOX/prompts.log" || fail 'specify lacks reservation instruction'
    grep -q 'Do not run create-new-feature again' "$SANDBOX/prompts.log" || fail 'specify could reallocate'
    [ "$(git -C "$SANDBOX" branch --show-current)" = 004-exp-910 ] || fail 'branch not derived from feature directory'
    if grep -q 'specs/003-older-feature' "$SANDBOX/prompts.log"; then fail 'old pointer reached a prompt'; fi
  else
    [ "$LAST_RC" = 11 ] || fail "$action did not stop"
    [ "$(cat "$SANDBOX/fake_claude_counter")" = 1 ] || fail 'provider called after pointer corruption'
    grep -q $'^move_issue\tEXP-910\tstate-triage$' "$SANDBOX/calls.log" || fail 'no recovery to Triage'
    ! grep -q 'speckit-plan/SKILL' "$SANDBOX/prompts.log" || fail 'plan saw another feature'
  fi
  echo "PASS feature pointer $action: prepared by shell, corruption stops before plan/tasks"
  teardown
 done

setup
export PR4_SPECIFY_ACTION=leave
# Retry after a failed specify, preserving a real user's work and reservation.
export PR4_SPECIFY_ACTION=remove
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 11 ] || fail 'retry setup'
echo '# preserve on retry' > "$SANDBOX/specs/004-exp-910/spec.md"
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 0 ] || fail 'retry failed'
[ "$(cat "$SANDBOX/specs/004-exp-910/spec.md")" = '# preserve on retry' ] || fail 'helper overwrote spec on retry'
[ "$(find "$SANDBOX/specs" -type d -name '*-exp-910' | wc -l | tr -d ' ')" = 1 ] || fail 'retry minted a second feature'
echo 'PASS retry reuses 004 and preserves spec content'
teardown

setup
rm "$SANDBOX/.specify/scripts/bash/create-new-feature.sh"
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 24 ] || fail 'missing pinned helper should block environment'
[ ! -f "$SANDBOX/fake_claude_counter" ] || fail 'missing helper spent a model call'
echo 'PASS missing helper -> 24 before model work'
teardown

setup
# A reservation becomes occupied outside this allocator. 1.2.2 auto-corrects
# --number as a preference; Bureau must stop before trusting that different number.
python3 -I "$SCRIPTS_DIR/bureau-supervision.py" --repo "$SANDBOX" reserve-spec-number EXP-910 >/dev/null
mkdir -p "$SANDBOX/specs/004-conflict"
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 11 ] || fail 'helper number drift was trusted'
[ ! -f "$SANDBOX/fake_claude_counter" ] || fail 'number drift spent a model call'
echo 'PASS real 1.2.2 --number auto-correction is rejected before specify'
teardown

setup
# Custom Bureau specs directory: helper has no directory flag, so the shell
# relocates its output and every later phase consumes the resulting pointer.
printf '\nBUREAU_SPECS_DIR="$SANDBOX/features"\n' >> "$SCRIPTS_DIR/bureau-config.sh"
mkdir -p "$SANDBOX/features/010-previous"
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 0 ] || fail 'configured specs directory'
grep -q 'Work on the spec in .*features/011-exp-910/' "$SANDBOX/prompts.log" || fail 'custom path lost'
echo '# custom retry content' > "$SANDBOX/features/011-exp-910/spec.md"
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 0 ] || fail 'custom retry'
[ "$(cat "$SANDBOX/features/011-exp-910/spec.md")" = '# custom retry content' ] || fail 'custom retry lost work'
[ ! -d "$SANDBOX/specs/011-exp-910" ] || fail 'custom retry created an extra directory'
echo 'PASS configured specs_dir and retry preserve branch, pointer, prompts and work'
teardown
setup
git -C "$SANDBOX" checkout -q main
mkdir -p "$SANDBOX/specs/050-main"
echo main > "$SANDBOX/specs/050-main/spec.md"
git -C "$SANDBOX" add specs; git -C "$SANDBOX" commit -qm '050 main'; git -C "$SANDBOX" push -q origin main
git -C "$SANDBOX" checkout -qb feat/KEY-51-unmerged
mkdir -p "$SANDBOX/specs/051-remote"
echo remote > "$SANDBOX/specs/051-remote/spec.md"
git -C "$SANDBOX" add specs; git -C "$SANDBOX" commit -qm '051 remote'; git -C "$SANDBOX" push -q origin HEAD
git -C "$SANDBOX" checkout -q --detach main
git -C "$SANDBOX" branch -D feat/KEY-51-unmerged >/dev/null
# Simulate a narrow fetch configuration: the stage must still fetch all heads.
git -C "$SANDBOX" config remote.origin.fetch '+refs/heads/main:refs/remotes/origin/main'
git -C "$SANDBOX" update-ref -d refs/remotes/origin/feat/KEY-51-unmerged
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 0 ] || fail 'stage origin-tree inspection'
[ "$(git -C "$SANDBOX" branch --show-current)" = 052-exp-910 ] || fail 'stage duplicated 051'
echo 'PASS stage fetches every origin head and passes --number 52 to the real helper'
echo 'OK test_spec_feature_json (10 scenarios; real pinned 1.2.2 helper)'
