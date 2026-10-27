#!/bin/bash
# Real disposable-worker resets, leases, branches and pinned helper; only
# provider/Linear calls are faked. The provider records the content it receives.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP
SEED=""; HOLDER_PID=""
cleanup() {
  if [ -n "$HOLDER_PID" ]; then kill "$HOLDER_PID" 2>/dev/null || true; wait "$HOLDER_PID" 2>/dev/null || true; fi
  [ -z "$SEED" ] || rm -rf "$SEED"
}
trap cleanup EXIT
fail() { echo "FAIL $* (rc=${LAST_RC:-})" >&2; printf '%s\n' "${LAST_STDERR:-}" "${LAST_STDOUT:-}" | tail -20 >&2; exit 1; }
setup_worker() {
  # These PR3 regressions exercise fresh rework and reserved-branch restoration.
  # Committed checkpoint reuse is covered by test_spec_resume.sh; opt out here
  # so the existing origin/base reset and provider-input assertions stay real.
  export BUREAU_SPEC_CHECKPOINTS_DISABLED=1
  sandbox_init EXP-910 001-test-branch
  SEED="$SANDBOX"; WORKER="$SEED/.worktrees/spec"
  git -C "$SEED" checkout -q main
  mkdir -p "$SEED/specs/003-older-feature"
  echo older > "$SEED/specs/003-older-feature/spec.md"
  printf 'calls.log\nfake_claude_counter\nprompts.log\nstderr.log\nspecify-input\nspecify-branch\n' > "$SEED/.gitignore"
  git -C "$SEED" add specs .specify/scripts .gitignore
  git -C "$SEED" commit -qm 'worker fixture with pinned helper'
  git -C "$SEED" push -q origin main
  printf '{"repo":{"base_branch":"main"}}\n' > "$SEED/.bureau.json"
  # Exercise production branch ownership and reset functions, including the
  # real runtime assert-owner; only optional worktree links are absent here.
  sed -n -e '/^bureau_get() {.*}$/p' \
    -e '/^free_branch_from_other_worktrees() {/,/^}/p' \
    -e '/^reset_worktree() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$SEED/reset-helpers.sh"
  sed -n '/^free_branch_from_other_worktrees() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  export SANDBOX="$WORKER" BUREAU_STUB_ISSUE_STATE=Triage
  export FAKE_CLAUDE_BIN="$LIB_DIR/pr4-fake-specify.sh" FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"
  export FAKE_CLAUDE_PROMPT_LOG="$WORKER/prompts.log" PR4_SPECIFY_ACTION=populate
  unset FAKE_CLAUDE_COMMIT_ON_ITERS PR4_SPECIFY_DIR PR4_SPECIFY_RAW BUREAU_SPEC_LOCK_TIMEOUT
}
reset_worker() {
  local run
  run=$(printf '%032d' "${1##*-}")
  python3 -B - "$SCRIPTS_DIR/bureau-runtime.py" "$SEED" "$WORKER" "$run" <<'PY'
from pathlib import Path
import runpy, sys
runtime, seed, worker, run = sys.argv[1:]
store = runpy.run_path(runtime)['Store'](Path(seed))
# Each completed fixture attempt releases its claim; the next run claims anew.
for prior in range(1, 4):
    store.release(f'{prior:032d}')
store.claim('EXP-910', Path(worker), run, 'background')
PY
  (
    cd "$SEED"
    source "$SEED/reset-helpers.sh"
    REPO_DIR="$SEED"; BUREAU_RUNTIME="$SCRIPTS_DIR/bureau-runtime.py"; BUREAU_CONFIG="$SEED/.bureau.json"
    BUREAU_RUN_ID="$run"; BUREAU_CURRENT_ISSUE=EXP-910; BUREAU_WORKSPACE_MODE=disposable
    bureau_link_worktree_paths() { :; }
    reset_worktree "$WORKER" spec-pipeline.sh
  ) || fail 'production disposable reset'
  [ -z "$(git -C "$WORKER" branch --show-current)" ] || fail 'reset did not detach worker'
  [ "$(git -C "$WORKER" rev-parse HEAD)" = "$(git -C "$SEED" rev-parse origin/main)" ] || fail 'reset did not return to origin/main'
  [ ! -d "$WORKER/specs/004-exp-910" ] || fail 'reset retained unmerged spec content'
}

for mode in local local-only local-ahead remote remote-rework fresh-worker diverged; do
  setup_worker
  reset_worker attempt-1
  run_pipeline spec-pipeline.sh EXP-910
  [ "$LAST_RC" = 0 ] || fail "$mode first spec attempt"
  git -C "$SEED" show-ref --verify --quiet refs/remotes/origin/004-exp-910 || fail 'first spec was not pushed'
  reset_worker attempt-2
  if [ "$mode" = fresh-worker ]; then
    WORKER="$SEED/.worktrees/spec-retry"
    export SANDBOX="$WORKER" FAKE_CLAUDE_PROMPT_LOG="$WORKER/prompts.log"
    reset_worker attempt-3
  fi
  case "$mode" in
    local-only) git --git-dir="$SEED/.fake-origin.git" update-ref -d refs/heads/004-exp-910 ;;
    remote) git -C "$SEED" branch -D 004-exp-910 >/dev/null ;;
    remote-rework|diverged)
      git clone -q --branch 004-exp-910 "$SEED/.fake-origin.git" "$SEED/reviewer"
      git -C "$SEED/reviewer" config user.email test@bureau
      git -C "$SEED/reviewer" config user.name Reviewer
      echo '# pushed review revisions' > "$SEED/reviewer/specs/004-exp-910/spec.md"
      git -C "$SEED/reviewer" add specs
      git -C "$SEED/reviewer" commit -qm 'review revisions returned for rework'
      git -C "$SEED/reviewer" push -q origin HEAD ;;
  esac
  case "$mode" in
    local-ahead|diverged)
      git -C "$SEED" checkout -q 004-exp-910
      echo '# unpublished local revisions' > "$SEED/specs/004-exp-910/spec.md"
      git -C "$SEED" add specs
      git -C "$SEED" commit -qm 'unpublished local revisions'
      git -C "$SEED" checkout -q main ;;
  esac
  expected='# ticket spec'
  [ "$mode" != remote-rework ] || expected='# pushed review revisions'
  [ "$mode" != local-ahead ] || expected='# unpublished local revisions'
  export PR4_SPECIFY_ACTION=observe
  if [ "$mode" = local ]; then
    # Undo branch restoration in a sandbox copy, reinstating the old late
    # checkout -b. The real worker reset exposes both lost content and exit 128.
    cp "$SCRIPTS_DIR/spec-pipeline.sh" "$SEED/spec-pipeline.fixed"
    python3 - "$SCRIPTS_DIR/spec-pipeline.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text()
start = text.index('# Resolve branch ownership BEFORE')
end = text.index('if [ ! -d "$WANTED_DIR" ]; then', start)
text = text[:start] + 'FEATURE_BRANCH="$ALLOCATED_NAME"\n' + text[end:]
text = text.replace('CUR_BRANCH=$(git branch --show-current)', 'git checkout -b "$FEATURE_BRANCH"\nCUR_BRANCH=$(git branch --show-current)')
p.write_text(text)
PY
    run_pipeline spec-pipeline.sh EXP-910
    [ "$LAST_RC" = 128 ] || fail 'late-checkout control did not reproduce exit 128'
    [ "$(cat "$WORKER/specify-input")" != "$expected" ] || fail 'late-checkout control unexpectedly restored content'
    echo 'PASS NEGATIVE CONTROL late checkout after reset -> specify reads fresh template, existing branch fails with 128'
    mv "$SEED/spec-pipeline.fixed" "$SCRIPTS_DIR/spec-pipeline.sh"
    reset_worker attempt-3
  fi
  run_pipeline spec-pipeline.sh EXP-910
  if [ "$mode" = diverged ]; then
    [ "$LAST_RC" = 25 ] || fail 'diverged reserved branch did not stop for human reconciliation'
    [ ! -f "$WORKER/fake_claude_counter" ] || fail 'diverged branch spent a provider call'
    [ "$(git -C "$WORKER" show HEAD:specs/004-exp-910/spec.md)" = '# unpublished local revisions' ] || fail 'divergence discarded local work'
    [ "$(git -C "$WORKER" show origin/004-exp-910:specs/004-exp-910/spec.md)" = '# pushed review revisions' ] || fail 'divergence discarded remote work'
    echo 'PASS diverged reserved branch -> 25 before specify, local and remote revisions both retained'
    cleanup; SEED=""
    continue
  fi
  [ "$LAST_RC" = 0 ] || fail "$mode reserved-branch retry"
  [ "$(cat "$WORKER/specify-input")" = "$expected" ] || fail "$mode specify did not receive saved revisions"
  [ "$(cat "$WORKER/specify-branch")" = 004-exp-910 ] || fail "$mode specify ran on main"
  [ "$(cat "$WORKER/specs/004-exp-910/spec.md")" = "$expected" ] || fail "$mode retry replaced saved spec"
  [ "$(git -C "$WORKER" branch --show-current)" = 004-exp-910 ] || fail "$mode retry lost branch"
  [ "$(jq -r .feature_directory "$WORKER/.specify/feature.json")" = "$(cd "$WORKER/specs/004-exp-910" && pwd -P)" ] || fail "$mode pointer still names another checkout"
  [ "$(cat "$SEED/.git/bureau/spec-numbers.json" | jq -r '.["EXP-910"]')" = 4 ] || fail "$mode reservation changed"
  echo "PASS $mode pushed spec returned for rework survives real reset/clean and new ownership claim; specify sees reserved content"
  cleanup; SEED=""
done

setup_worker
reset_worker attempt-1
# Another checkout holding this issue's reserved branch must stay untouched.
python3 -I "$SCRIPTS_DIR/bureau-supervision.py" --repo "$WORKER" reserve-spec-number EXP-910 >/dev/null
git -C "$SEED" worktree add -qb 004-exp-910 "$SEED/held" main
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 21 ] || fail 'held branch was not refused'
[ "$(git -C "$SEED/held" branch --show-current)" = 004-exp-910 ] || fail 'holder was detached'
[ ! -f "$WORKER/fake_claude_counter" ] || fail 'held branch spent a provider call'
echo 'PASS held reserved branch -> 21 before specify; other checkout remains attached'
cleanup; SEED=""

setup_worker
reset_worker attempt-1
python3 - "$SEED/.git/bureau/spec-numbers.guard" "$SEED/locked" <<'PY' &
import fcntl, pathlib, sys, time
with open(sys.argv[1], 'a') as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    pathlib.Path(sys.argv[2]).touch()
    time.sleep(30)
PY
HOLDER_PID=$!
for ((i=0; i<100; i++)); do [ ! -f "$SEED/locked" ] || break; sleep 0.01; done
[ -f "$SEED/locked" ] || fail 'lock holder never became ready'
export BUREAU_SPEC_LOCK_TIMEOUT=1
run_pipeline spec-pipeline.sh EXP-910
[ "$LAST_RC" = 124 ] || fail 'allocator timeout was not propagated by the stage'
grep -q 'timed out after 1s waiting for spec allocator lock' "$WORKER/stderr.log" || fail 'allocator timeout lacks diagnostic'
grep -q $'^move_issue\tEXP-910\tstate-triage$' "$WORKER/calls.log" || fail 'timeout stranded issue in Spec'
[ ! -f "$WORKER/fake_claude_counter" ] || fail 'allocator timeout spent a provider call'
[ ! -f "$SEED/.git/bureau/spec-numbers.json" ] || fail 'timeout changed reservations'
echo 'PASS real held lock -> stage exit 124, explicit message, Triage recovery, no reservation or specify call'
echo 'OK test_spec_worker_retry (9 worker scenarios; late-checkout negative control)'
